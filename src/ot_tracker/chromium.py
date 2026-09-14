from __future__ import annotations

import ast
import base64
import fnmatch
import hashlib
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from .config import TargetRule, TrackerConfig
from .http import FetchError, HttpClient


RUNTIME_FEATURES_PATH = (
    "third_party/blink/renderer/platform/runtime_enabled_features.json5"
)
MANUAL_COMPLETION_PATH = (
    "third_party/blink/common/origin_trials/"
    "manual_completion_origin_trial_features.cc"
)
NAVIGATION_FEATURES_PATH = (
    "third_party/blink/common/origin_trials/navigation_origin_trial_features.cc"
)
PERSISTENT_TRIALS_PATH = (
    "third_party/blink/common/origin_trials/persistent_origin_trials.cc"
)

DECLARATION_KEYS = (
    "name",
    "origin_trial_feature_name",
    "origin_trial_type",
    "origin_trial_allows_third_party",
    "origin_trial_allows_insecure",
    "origin_trial_os",
    "status",
    "base_feature",
    "base_feature_status",
    "copied_from_base_feature_if",
    "public",
    "browser_process_read_access",
    "browser_process_read_write_access",
    "implied_by",
    "depends_on",
)


def _strip_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    state = "normal"
    quote = ""
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if state == "string":
            output.append(char)
            if char == "\\" and index + 1 < len(text):
                output.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                state = "normal"
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                output.append("\n")
                state = "normal"
            else:
                output.append(" ")
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                output.extend((" ", " "))
                index += 2
                state = "normal"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if char in ('"', "'"):
            state = "string"
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            output.extend((" ", " "))
            index += 2
            state = "line_comment"
            continue
        if char == "/" and next_char == "*":
            output.extend((" ", " "))
            index += 2
            state = "block_comment"
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _iter_object_blocks(text: str) -> Iterable[tuple[int, str]]:
    stack: list[int] = []
    index = 0
    state = "normal"
    quote = ""
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if state == "string":
            if char == "\\":
                index += 2
                continue
            if char == quote:
                state = "normal"
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                state = "normal"
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "normal"
                index += 2
            else:
                index += 1
            continue
        if char in ('"', "'"):
            state = "string"
            quote = char
        elif char == "/" and next_char == "/":
            state = "line_comment"
            index += 2
            continue
        elif char == "/" and next_char == "*":
            state = "block_comment"
            index += 2
            continue
        elif char == "{":
            stack.append(index)
        elif char == "}" and stack:
            start = stack.pop()
            yield start, text[start : index + 1]
        index += 1


def _split_top_level(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote = ""
    escaped = False
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ('"', "'"):
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _parse_value(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if value[0] in ('"', "'") and value[-1:] == value[0]:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        items = _split_top_level(value[1:-1], ",")
        return [_parse_value(item) for item in items if item.strip()]
    return re.sub(r"\s+", " ", value).strip()


def _parse_object_members(block: str) -> dict[str, Any]:
    cleaned = _strip_comments(block).strip()
    if not (cleaned.startswith("{") and cleaned.endswith("}")):
        return {}
    members: dict[str, Any] = {}
    for item in _split_top_level(cleaned[1:-1], ","):
        key_value = _split_top_level(item, ":")
        if len(key_value) < 2:
            continue
        key = key_value[0].strip().strip('"\'')
        value = ":".join(key_value[1:])
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            members[key] = _parse_value(value)
    return members


def parse_runtime_declarations(
    text: str,
    *,
    source_path: str = RUNTIME_FEATURES_PATH,
) -> list[dict[str, Any]]:
    candidates: list[tuple[int, int, dict[str, Any], str]] = []
    for start, block in _iter_object_blocks(text):
        if "origin_trial_feature_name" not in block or "name" not in block:
            continue
        members = _parse_object_members(block)
        runtime_name = members.get("name")
        trial_name = members.get("origin_trial_feature_name")
        if not isinstance(runtime_name, str) or not isinstance(trial_name, str):
            continue
        candidates.append((len(block), start, members, block.strip()))

    # Parent objects also contain the same strings.  Keeping the smallest object for
    # each RuntimeEnabledFeature selects the actual feature declaration.
    selected: dict[str, tuple[int, dict[str, Any], str]] = {}
    for _length, start, members, block in sorted(candidates, key=lambda item: item[0]):
        runtime_name = str(members["name"])
        if runtime_name not in selected:
            selected[runtime_name] = (start, members, block)

    declarations: list[dict[str, Any]] = []
    for runtime_name, (start, members, block) in selected.items():
        fields = {key: members.get(key) for key in DECLARATION_KEYS if key in members}
        declarations.append(
            {
                "runtime_feature_name": runtime_name,
                "trial_name": members["origin_trial_feature_name"],
                "fields": fields,
                "classifications": [],
                "source_path": source_path,
                "source_line": text.count("\n", 0, start) + 1,
                "source_excerpt": block,
            }
        )
    declarations.sort(key=lambda item: item["runtime_feature_name"])
    return declarations


def _production_tail(text: str) -> str:
    marker = re.search(r"Production[^\n]*", text, flags=re.IGNORECASE)
    return text[marker.end() :] if marker else text


def parse_special_classifications(files: dict[str, str]) -> dict[str, set[str]]:
    classifications: dict[str, set[str]] = {}

    manual = files.get(MANUAL_COMPLETION_PATH)
    if manual:
        for name in re.findall(r"OriginTrialFeature::k([A-Za-z0-9_]+)", _production_tail(manual)):
            classifications.setdefault(name, set()).add("expiry_grace_period")

    navigation = files.get(NAVIGATION_FEATURES_PATH)
    if navigation:
        for name in re.findall(
            r"OriginTrialFeature::k([A-Za-z0-9_]+)", _production_tail(navigation)
        ):
            classifications.setdefault(name, set()).add("navigation")

    persistent = files.get(PERSISTENT_TRIALS_PATH)
    if persistent:
        for name in re.findall(r'"([A-Za-z][A-Za-z0-9_]+)"', _production_tail(persistent)):
            classifications.setdefault(name, set()).add("persistent_to_next_response")
    return classifications


def attach_classifications(
    declarations: list[dict[str, Any]], classifications: dict[str, set[str]]
) -> None:
    for declaration in declarations:
        values = set()
        values.update(classifications.get(declaration["runtime_feature_name"], set()))
        values.update(classifications.get(declaration["trial_name"], set()))
        declaration["classifications"] = sorted(values)


@dataclass(frozen=True)
class ChromiumSnapshot:
    revision: str
    files: dict[str, str]
    file_errors: dict[str, str]
    declarations: list[dict[str, Any]]
    classifications: dict[str, set[str]]


@dataclass(frozen=True)
class ChromiumReleaseSnapshot:
    channel: str
    platform: str
    milestone: int
    version: str
    revision: str
    chromium: ChromiumSnapshot


@dataclass(frozen=True)
class ChromiumReleaseCollection:
    snapshots: dict[str, ChromiumReleaseSnapshot]
    errors: dict[str, str]


class ChromiumSourceClient:
    def __init__(self, config: TrackerConfig, http: HttpClient):
        self.config = config
        self.http = http

    def fetch_revision(self, ref_name: str | None = None) -> str:
        ref = urllib.parse.quote(ref_name or self.config.chromium_ref, safe="/")
        response = self.http.get_json(
            f"{self.config.gitiles_base_url}/+/{ref}", params={"format": "JSON"}
        )
        revision = response.get("commit") if isinstance(response, dict) else None
        if not isinstance(revision, str) or not revision:
            raise FetchError("Chromium Gitiles revision response has no commit")
        return revision

    def fetch_file(self, revision: str, path: str) -> str:
        safe_path = urllib.parse.quote(path, safe="/")
        encoded = self.http.get_bytes(
            f"{self.config.gitiles_base_url}/+/{revision}/{safe_path}",
            params={"format": "TEXT"},
        )
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise FetchError(f"invalid Gitiles TEXT response for {path}: {exc}") from exc

    def fetch_snapshot_at(
        self,
        revision: str,
        *,
        source_files: Iterable[str] | None = None,
    ) -> ChromiumSnapshot:
        files: dict[str, str] = {}
        errors: dict[str, str] = {}
        requested_files = tuple(source_files or self.config.chromium_source_files)
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="gitiles") as executor:
            futures = {
                executor.submit(self.fetch_file, revision, path): path
                for path in requested_files
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    files[path] = future.result()
                except Exception as exc:
                    errors[path] = str(exc)
        declarations = parse_runtime_declarations(files.get(RUNTIME_FEATURES_PATH, ""))
        classifications = parse_special_classifications(files)
        attach_classifications(declarations, classifications)
        return ChromiumSnapshot(
            revision=revision,
            files=files,
            file_errors=errors,
            declarations=declarations,
            classifications=classifications,
        )

    def fetch_snapshot(self) -> ChromiumSnapshot:
        return self.fetch_snapshot_at(self.fetch_revision())


class ChromiumReleaseClient:
    def __init__(
        self,
        config: TrackerConfig,
        http: HttpClient,
        *,
        source_client: ChromiumSourceClient | None = None,
    ):
        self.config = config
        self.http = http
        self.source_client = source_client or ChromiumSourceClient(config, http)

    def fetch_snapshot(self, channel: str) -> ChromiumReleaseSnapshot:
        normalized_channel = channel.strip().title()
        response = self.http.get_json(
            f"{self.config.chromiumdash_base_url}/fetch_releases",
            params={
                "channel": normalized_channel,
                "platform": self.config.chromium_release_platform,
                "num": 1,
            },
        )
        if not isinstance(response, list) or not response:
            raise FetchError(
                f"ChromiumDash returned no {normalized_channel} release"
            )
        release = response[0] if isinstance(response[0], dict) else {}
        hashes = release.get("hashes")
        revision = hashes.get("chromium") if isinstance(hashes, dict) else None
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise FetchError(
                f"ChromiumDash {normalized_channel} release has no Chromium revision"
            )
        try:
            milestone = int(release["milestone"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FetchError(
                f"ChromiumDash {normalized_channel} release has no milestone"
            ) from exc
        version = str(release.get("version") or "")
        if not version:
            raise FetchError(
                f"ChromiumDash {normalized_channel} release has no version"
            )
        snapshot = self.source_client.fetch_snapshot_at(
            revision,
            source_files=(RUNTIME_FEATURES_PATH,),
        )
        if snapshot.file_errors:
            error = snapshot.file_errors.get(RUNTIME_FEATURES_PATH) or next(
                iter(snapshot.file_errors.values())
            )
            raise FetchError(
                f"{normalized_channel} runtime declaration fetch failed: {error}"
            )
        return ChromiumReleaseSnapshot(
            channel=normalized_channel,
            platform=self.config.chromium_release_platform,
            milestone=milestone,
            version=version,
            revision=revision,
            chromium=snapshot,
        )

    def fetch_snapshots(self) -> ChromiumReleaseCollection:
        channels = tuple(
            dict.fromkeys(
                channel.strip().title()
                for channel in self.config.chromium_release_channels
                if channel.strip()
            )
        )
        snapshots: dict[str, ChromiumReleaseSnapshot] = {}
        errors: dict[str, str] = {}
        if not channels:
            return ChromiumReleaseCollection(snapshots=snapshots, errors=errors)
        with ThreadPoolExecutor(
            max_workers=len(channels), thread_name_prefix="chromium-release"
        ) as executor:
            futures = {
                executor.submit(self.fetch_snapshot, channel): channel
                for channel in channels
            }
            for future in as_completed(futures):
                channel = futures[future]
                try:
                    snapshot = future.result()
                    snapshots[snapshot.channel] = snapshot
                except Exception as exc:
                    errors[channel] = str(exc)
        return ChromiumReleaseCollection(snapshots=snapshots, errors=errors)


def canonical_source_file(path: str, content: str, revision: str) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "size": len(content.encode("utf-8")),
    }


def _parse_gerrit_change(raw: dict[str, Any], base_url: str) -> dict[str, Any]:
    revision_id = raw.get("current_revision")
    revisions = raw.get("revisions") or {}
    revision = revisions.get(revision_id) if revision_id else None
    revision = revision if isinstance(revision, dict) else {}
    commit = revision.get("commit") if isinstance(revision.get("commit"), dict) else {}
    files = revision.get("files") if isinstance(revision.get("files"), dict) else {}
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    number = raw.get("_number")
    file_stats = []
    for path, details in sorted(files.items()):
        details = details if isinstance(details, dict) else {}
        file_stats.append(
            {
                "path": path,
                "status": details.get("status"),
                "lines_inserted": int(details.get("lines_inserted") or 0),
                "lines_deleted": int(details.get("lines_deleted") or 0),
            }
        )
    return {
        "change_number": int(number) if number is not None else None,
        "change_id": raw.get("change_id"),
        "project_id": raw.get("id"),
        "status": raw.get("status"),
        "work_in_progress": bool(raw.get("work_in_progress")),
        "subject": raw.get("subject"),
        "created": raw.get("created"),
        "submitted": raw.get("submitted"),
        "updated": raw.get("updated"),
        "revision": revision_id,
        "patchset": raw.get("current_revision_number") or revision.get("_number"),
        "commit_message": commit.get("message"),
        "author": {
            "name": author.get("name"),
            "email": author.get("email"),
        },
        "files": sorted(files.keys()),
        "file_stats": file_stats,
        "url": f"{base_url}/c/chromium/src/+/{number}" if number else None,
    }


_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_HEADER = re.compile(r"^@@ .+? @@(?: (.*))?$")
_SIGNATURE = re.compile(
    r"^[+-]\s*(?:[A-Za-z_][A-Za-z0-9_:<>,*&\s]+\s+)?"
    r"([A-Za-z_~][A-Za-z0-9_:~]*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:\{|;|$)"
)
_ORIGIN_TRIAL_DECLARATION = re.compile(
    r'''["']?origin_trial_feature_name["']?\s*:\s*["']([^"']+)["']'''
)


def parse_unified_diff_summary(
    patch: str,
    *,
    max_files: int = 8,
    max_contexts: int = 8,
) -> dict[str, Any]:
    """Extract compact file, line, and function context from a unified diff."""
    current_file: str | None = None
    file_counts: dict[str, dict[str, int]] = {}
    contexts: list[str] = []
    symbols: list[str] = []
    added_origin_trial_feature_names: set[str] = set()
    for line in patch.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            current_file = header.group(2)
            file_counts.setdefault(current_file, {"insertions": 0, "deletions": 0})
            continue
        hunk = _HUNK_HEADER.match(line)
        if hunk:
            context = re.sub(r"\s+", " ", (hunk.group(1) or "").strip())
            if context and context not in contexts and len(contexts) < max_contexts:
                contexts.append(context[:180])
            continue
        if current_file is None or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            file_counts[current_file]["insertions"] += 1
            if current_file == RUNTIME_FEATURES_PATH:
                declaration = _ORIGIN_TRIAL_DECLARATION.search(line[1:])
                if declaration:
                    added_origin_trial_feature_names.add(declaration.group(1).strip())
        elif line.startswith("-"):
            file_counts[current_file]["deletions"] += 1
        else:
            continue
        signature = _SIGNATURE.match(line)
        if signature:
            symbol = signature.group(1)
            if symbol not in symbols and len(symbols) < max_contexts:
                symbols.append(symbol[:120])

    ranked = sorted(
        (
            {"path": path, **counts}
            for path, counts in file_counts.items()
        ),
        key=lambda item: (
            -(item["insertions"] + item["deletions"]),
            item["path"],
        ),
    )
    return {
        "files_changed": len(file_counts),
        "insertions": sum(item["insertions"] for item in ranked),
        "deletions": sum(item["deletions"] for item in ranked),
        "top_files": ranked[:max_files],
        "hunk_contexts": contexts,
        "changed_symbols": symbols,
        "added_origin_trial_feature_names": sorted(added_origin_trial_feature_names),
    }


def parse_gerrit_file_diff_summary(
    path: str,
    diff: dict[str, Any],
    *,
    max_contexts: int = 8,
) -> dict[str, Any]:
    """Extract the same compact summary from Gerrit's single-file DiffInfo."""
    content = diff.get("content")
    if not isinstance(content, list):
        raise ValueError("Gerrit file diff has no content list")

    insertions = 0
    deletions = 0
    symbols: list[str] = []
    added_origin_trial_feature_names: set[str] = set()
    for block in content:
        if not isinstance(block, dict):
            continue
        added = block.get("b") if isinstance(block.get("b"), list) else []
        removed = block.get("a") if isinstance(block.get("a"), list) else []
        insertions += len(added)
        deletions += len(removed)

        # A move is not a newly introduced OT declaration even though Gerrit
        # represents its destination as side-B content.
        declaration_lines = [] if block.get("due_to_move") else added
        if path == RUNTIME_FEATURES_PATH:
            for line in declaration_lines:
                declaration = _ORIGIN_TRIAL_DECLARATION.search(str(line))
                if declaration:
                    added_origin_trial_feature_names.add(
                        declaration.group(1).strip()
                    )

        for prefix, lines in (("+", added), ("-", removed)):
            for line in lines:
                signature = _SIGNATURE.match(prefix + str(line))
                if signature:
                    symbol = signature.group(1)
                    if symbol not in symbols and len(symbols) < max_contexts:
                        symbols.append(symbol[:120])

    return {
        "files_changed": 1,
        "insertions": insertions,
        "deletions": deletions,
        "top_files": [
            {
                "path": path,
                "insertions": insertions,
                "deletions": deletions,
            }
        ],
        "hunk_contexts": [],
        "changed_symbols": symbols,
        "added_origin_trial_feature_names": sorted(
            added_origin_trial_feature_names
        ),
    }


def summarize_change_file_stats(change: dict[str, Any]) -> dict[str, Any]:
    stats = [item for item in change.get("file_stats") or [] if isinstance(item, dict)]
    ranked = sorted(
        (
            {
                "path": str(item.get("path") or ""),
                "insertions": int(item.get("lines_inserted") or 0),
                "deletions": int(item.get("lines_deleted") or 0),
            }
            for item in stats
        ),
        key=lambda item: (-(item["insertions"] + item["deletions"]), item["path"]),
    )
    return {
        "files_changed": len(change.get("files") or []),
        "insertions": sum(item["insertions"] for item in ranked),
        "deletions": sum(item["deletions"] for item in ranked),
        "top_files": ranked[:8],
        "hunk_contexts": [],
        "changed_symbols": [],
        "added_origin_trial_feature_names": [],
    }


class GerritClient:
    def __init__(self, config: TrackerConfig, http: HttpClient):
        self.config = config
        self.http = http

    def fetch_changes(self, after: datetime) -> list[dict[str, Any]]:
        after = after.astimezone(UTC)
        timestamp = after.strftime("%Y-%m-%d %H:%M:%S")
        status_filter = "" if self.config.gerrit_open_changes_enabled else "status:merged "
        query = f'project:chromium/src {status_filter}branch:main after:"{timestamp}"'
        changes: list[dict[str, Any]] = []
        offset = 0
        more_changes = False
        while len(changes) < self.config.gerrit_max_changes:
            params = [
                ("q", query),
                ("o", "CURRENT_REVISION"),
                ("o", "CURRENT_COMMIT"),
                ("o", "CURRENT_FILES"),
                ("n", str(self.config.gerrit_page_size)),
                ("S", str(offset)),
            ]
            url = f"{self.config.gerrit_base_url}/changes/?{urllib.parse.urlencode(params)}"
            response = self.http.get_json(url)
            if not isinstance(response, list):
                raise FetchError("Gerrit changes response is not a list")
            if not response:
                break
            changes.extend(
                _parse_gerrit_change(item, self.config.gerrit_base_url)
                for item in response
                if isinstance(item, dict)
            )
            has_more = bool(response[-1].get("_more_changes"))
            more_changes = has_more
            offset += len(response)
            if not has_more:
                break
        if more_changes and len(changes) >= self.config.gerrit_max_changes:
            raise FetchError(
                "Gerrit result exceeded max_changes; watermark was not advanced. "
                "Increase gerrit.max_changes or reduce the lookback window."
            )
        changes = changes[: self.config.gerrit_max_changes]
        changes.sort(
            key=lambda item: (item.get("updated") or "", item.get("change_number") or 0)
        )
        return changes

    def fetch_file_diff_summary(
        self, change: dict[str, Any], path: str
    ) -> dict[str, Any]:
        number = change.get("change_number")
        if number is None:
            raise FetchError("Gerrit file diff requires a change number")
        revision = str(change.get("revision") or "current")
        encoded_revision = urllib.parse.quote(revision, safe="")
        encoded_path = urllib.parse.quote(path, safe="")
        response = self.http.get_json(
            f"{self.config.gerrit_base_url}/changes/{number}/revisions/"
            f"{encoded_revision}/files/{encoded_path}/diff"
        )
        if not isinstance(response, dict):
            raise FetchError(f"invalid Gerrit file diff for change {number}: {path}")
        try:
            return parse_gerrit_file_diff_summary(path, response)
        except ValueError as exc:
            raise FetchError(
                f"invalid Gerrit file diff for change {number}: {path}"
            ) from exc

    @staticmethod
    def _merge_targeted_file_summary(
        summary: dict[str, Any],
        targeted: dict[str, Any],
        path: str,
    ) -> None:
        for key in ("hunk_contexts", "changed_symbols"):
            summary[key] = list(
                dict.fromkeys(
                    [
                        *[str(value) for value in summary.get(key) or []],
                        *[str(value) for value in targeted.get(key) or []],
                    ]
                )
            )[:8]
        summary["added_origin_trial_feature_names"] = sorted(
            {
                *[
                    str(value)
                    for value in summary.get("added_origin_trial_feature_names") or []
                ],
                *[
                    str(value)
                    for value in targeted.get("added_origin_trial_feature_names") or []
                ],
            }
        )
        summary["targeted_files"] = sorted(
            {*[str(value) for value in summary.get("targeted_files") or []], path}
        )
        summary["targeted_file_stats"] = targeted.get("top_files") or []

    def fetch_patch_summary(self, change: dict[str, Any]) -> dict[str, Any]:
        summary = summarize_change_file_stats(change)
        number = change.get("change_number")
        if not self.config.gerrit_patch_details_enabled or number is None:
            summary["detail"] = "file_stats"
            return summary
        files = [str(path) for path in change.get("files") or []]
        runtime_file_changed = RUNTIME_FEATURES_PATH in files
        if len(files) > 80:
            summary.update(
                {
                    "detail": "file_stats",
                    "truncated": True,
                    "full_patch_skipped_reason": "file_count_limit",
                }
            )
            if runtime_file_changed:
                targeted = self.fetch_file_diff_summary(
                    change, RUNTIME_FEATURES_PATH
                )
                self._merge_targeted_file_summary(
                    summary, targeted, RUNTIME_FEATURES_PATH
                )
                summary["detail"] = "targeted_file_diff"
            return summary
        revision = urllib.parse.quote(str(change.get("revision") or "current"), safe="")
        encoded = self.http.get_bytes(
            f"{self.config.gerrit_base_url}/changes/{number}/revisions/"
            f"{revision}/patch?download"
        )
        try:
            decoded = base64.b64decode(encoded)
        except ValueError as exc:
            raise FetchError(f"invalid Gerrit patch response for change {number}") from exc
        truncated = len(decoded) > self.config.gerrit_max_patch_bytes
        decoded = decoded[: self.config.gerrit_max_patch_bytes]
        detailed = parse_unified_diff_summary(decoded.decode("utf-8", errors="replace"))
        # Gerrit's CURRENT_FILES counts remain authoritative if a truncated patch
        # does not include every file.
        if truncated:
            detailed["files_changed"] = summary["files_changed"]
            detailed["insertions"] = summary["insertions"]
            detailed["deletions"] = summary["deletions"]
            if runtime_file_changed:
                targeted = self.fetch_file_diff_summary(
                    change, RUNTIME_FEATURES_PATH
                )
                self._merge_targeted_file_summary(
                    detailed, targeted, RUNTIME_FEATURES_PATH
                )
        detailed.update({"detail": "unified_diff", "truncated": truncated})
        if detailed.get("targeted_files"):
            detailed["detail"] = "unified_diff_with_targeted_file"
        return detailed


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


_PATH_NOISE_PARTS = {
    "test",
    "tests",
    "testing",
    "web_tests",
    "wpt",
    "resources",
    "testdata",
}

# These directories are specific enough to pass the depth check but still host
# many unrelated features.  A directory this broad is only safe when the OT or
# RuntimeEnabledFeature alias is present in the path itself.
_GENERIC_IMPLEMENTATION_DIRS = {
    "chrome/browser/",
    "content/browser/",
    "content/browser/renderer_host/",
    "third_party/blink/renderer/core/dom/",
    "third_party/blink/renderer/core/frame/",
    "third_party/blink/renderer/core/html/",
    "third_party/blink/renderer/core/loader/",
    "third_party/blink/renderer/core/page/",
    "third_party/blink/renderer/core/timing/",
}


def _production_implementation_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1].casefold() if parts else ""
    if any(part.casefold() in _PATH_NOISE_PARTS for part in parts):
        return False
    if re.search(r"_(?:test|unittest|browsertest|uitest)s?(?:[_.]|$)", name):
        return False
    if name in {"owners", "readme.md", "dir_metadata", "presubmit.py", "build.gn"}:
        return False
    if path in {
        RUNTIME_FEATURES_PATH,
        MANUAL_COMPLETION_PATH,
        NAVIGATION_FEATURES_PATH,
        PERSISTENT_TRIALS_PATH,
    }:
        return False
    if path.startswith("tools/metrics/") or path in {"DEPS", "WATCHLISTS"}:
        return False
    return True


def infer_implementation_path_candidates(
    change: dict[str, Any],
    aliases: Iterable[str],
) -> list[dict[str, Any]]:
    """Infer conservative source path prefixes from an alias-matched Gerrit CL."""
    files = [
        str(path)
        for path in change.get("files") or []
        if _production_implementation_path(str(path))
    ]
    if not files:
        return []

    alias_tokens = {
        _normalized(alias)
        for alias in aliases
        if len(_normalized(alias)) >= 8
    }
    by_parent: dict[str, list[str]] = {}
    for path in files:
        parent, _, _name = path.rpartition("/")
        if parent:
            by_parent.setdefault(parent + "/", []).append(path)

    candidates: dict[str, dict[str, Any]] = {}
    for parent, parent_files in by_parent.items():
        depth = parent.rstrip("/").count("/") + 1
        normalized_parent = _normalized(parent)
        alias_in_path = any(token in normalized_parent for token in alias_tokens)
        sufficiently_specific = (
            depth >= 5
            if parent.startswith("third_party/blink/renderer/")
            else depth >= 3
        )
        if not sufficiently_specific:
            continue
        if parent in _GENERIC_IMPLEMENTATION_DIRS and not alias_in_path:
            continue
        if len(parent_files) >= 2 or alias_in_path:
            score = 92 if alias_in_path else min(88, 78 + len(parent_files) * 2)
            candidates[parent] = {
                "path": parent,
                "score": score,
                "kind": "directory_prefix",
                "files": sorted(parent_files)[:12],
                "reasons": [
                    "trial/runtime alias matched the Gerrit change",
                    (
                        "alias token appears in the implementation directory"
                        if alias_in_path
                        else f"{len(parent_files)} production files share a specific directory"
                    ),
                ],
            }

    if not candidates and len(files) <= 4:
        for path in files:
            normalized_path = _normalized(path)
            alias_in_path = any(token in normalized_path for token in alias_tokens)
            score = 85 if alias_in_path else 68
            candidates[path] = {
                "path": path,
                "score": score,
                "kind": "exact_file",
                "files": [path],
                "reasons": [
                    "trial/runtime alias matched the Gerrit change",
                    (
                        "alias token appears in the implementation path"
                        if alias_in_path
                        else "single production implementation file; requires confirmation"
                    ),
                ],
            }

    return sorted(
        candidates.values(), key=lambda item: (-int(item["score"]), str(item["path"]))
    )[:8]


def build_target_index(
    active_trials: list[dict[str, Any]],
    declarations: list[dict[str, Any]],
    target_rules: dict[str, TargetRule],
) -> dict[str, dict[str, list[str]]]:
    runtime_names: dict[str, set[str]] = {}
    for declaration in declarations:
        runtime_names.setdefault(str(declaration["trial_name"]), set()).add(
            str(declaration["runtime_feature_name"])
        )

    targets: dict[str, dict[str, list[str]]] = {}
    for trial in active_trials:
        name = trial.get("trial_name")
        if not isinstance(name, str) or not name:
            continue
        rule = target_rules.get(name)
        aliases = {name, *runtime_names.get(name, set())}
        paths: set[str] = set()
        if rule:
            aliases.update(rule.aliases)
            paths.update(rule.paths)
        targets[name] = {
            "aliases": sorted(alias for alias in aliases if len(_normalized(alias)) >= 6),
            "paths": sorted(paths),
        }
    return targets


def match_change_to_targets(
    change: dict[str, Any],
    targets: dict[str, dict[str, list[str]]],
) -> dict[str, dict[str, list[str]]]:
    files = [str(path) for path in change.get("files") or []]
    matchable_files = [
        path
        for path in files
        if _production_implementation_path(path)
        or path
        in {
            RUNTIME_FEATURES_PATH,
            MANUAL_COMPLETION_PATH,
            NAVIGATION_FEATURES_PATH,
            PERSISTENT_TRIALS_PATH,
        }
    ]
    # Bulk WPT/test imports often contain many OT names in filenames and commit
    # messages. They are useful test churn, not implementation changes.
    if files and not matchable_files:
        return {}

    diff_summary = change.get("diff_summary")
    declared_trial_names = (
        diff_summary.get("added_origin_trial_feature_names") or []
        if isinstance(diff_summary, dict)
        else []
    )
    message = "\n".join(
        str(value or "")
        for value in (
            change.get("subject"),
            change.get("commit_message"),
            "\n".join(matchable_files),
            "\n".join(str(name) for name in declared_trial_names),
        )
    )
    folded = message.casefold()
    normalized = _normalized(message)
    matches: dict[str, dict[str, list[str]]] = {}
    for trial_name, rule in targets.items():
        alias_matches = []
        for alias in rule["aliases"]:
            alias_folded = alias.casefold()
            alias_normalized = _normalized(alias)
            if alias_folded in folded or (
                len(alias_normalized) >= 8 and alias_normalized in normalized
            ):
                alias_matches.append(alias)
        path_matches = []
        for pattern in rule["paths"]:
            for path in matchable_files:
                if (
                    fnmatch.fnmatch(path, pattern)
                    if any(char in pattern for char in "*?[")
                    else path.startswith(pattern)
                ):
                    path_matches.append(path)
        if alias_matches or path_matches:
            matches[trial_name] = {
                "aliases": sorted(set(alias_matches)),
                "paths": sorted(set(path_matches)),
            }
    return matches


def gerrit_ot_signal(change: dict[str, Any]) -> dict[str, Any] | None:
    subject = str(change.get("subject") or "")
    message = str(change.get("commit_message") or "")
    files = [str(path) for path in change.get("files") or []]
    language_text = f"{subject}\n{message}".casefold()
    text = f"{language_text}\n{' '.join(files).casefold()}"
    terms = (
        "origin trial",
        "origin_trial",
        "origintrial",
        "trialtokenvalidator",
        "trial token validator",
    )
    matched_terms = sorted(term for term in terms if term in language_text)
    special_paths = [
        path
        for path in files
        if path == RUNTIME_FEATURES_PATH
        or path in {MANUAL_COMPLETION_PATH, NAVIGATION_FEATURES_PATH, PERSISTENT_TRIALS_PATH}
    ]
    diff_summary = change.get("diff_summary")
    declared_trial_names = sorted(
        {
            str(name).strip()
            for name in (
                diff_summary.get("added_origin_trial_feature_names") or []
                if isinstance(diff_summary, dict)
                else []
            )
            if str(name).strip()
        }
    )
    if not matched_terms and not special_paths and not declared_trial_names:
        return None

    # A generic edit to runtime_enabled_features.json5 is not itself an OT
    # candidate.  Exact declaration additions are detected by the source parser.
    # Gerrit becomes a strong early signal only when the CL also uses OT language.
    score = 40 if matched_terms else 10
    reasons: list[str] = []
    if any(term in subject.casefold() for term in terms):
        score += 15
        reasons.append("origin-trial term in subject")
    if RUNTIME_FEATURES_PATH in special_paths:
        score += 30 if matched_terms else 5
        reasons.append(
            "runtime OT declaration file changed"
            if matched_terms
            else "generic runtime feature file change; no OT-specific term"
        )
    if any(path != RUNTIME_FEATURES_PATH for path in special_paths):
        score += 20 if matched_terms else 25
        reasons.append("special OT classification file changed")
    if declared_trial_names:
        score = max(score, 95)
        reasons.append(
            "origin_trial_feature_name added in patch: "
            + ", ".join(declared_trial_names)
        )
    if "test" in subject.casefold() or "frobulate" in text:
        score -= 35
        reasons.append("test/scaffold language lowers confidence")
    return {
        "score": max(0, min(100, score)),
        "matched_terms": matched_terms,
        "special_paths": special_paths,
        "declared_trial_names": declared_trial_names,
        "reasons": reasons or ["origin-trial implementation term observed"],
    }


def matches_ignore_pattern(name: str, patterns: Iterable[str]) -> str | None:
    for pattern in patterns:
        if fnmatch.fnmatch(name.casefold(), pattern.casefold()):
            return pattern
    return None
