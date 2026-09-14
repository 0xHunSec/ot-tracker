from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class DiffEntry:
    path: str
    old: Any
    new: Any

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _keyed_list(value: list[Any]) -> dict[str, Any] | None:
    if not value or not all(isinstance(item, dict) and "id" in item for item in value):
        return None
    keys = [str(item["id"]) for item in value]
    if len(set(keys)) != len(keys):
        return None
    return dict(zip(keys, value))


def deep_diff(old: Any, new: Any, path: str = "") -> list[DiffEntry]:
    if type(old) is not type(new):
        return [DiffEntry(path or "/", old, new)]
    if isinstance(old, dict):
        differences: list[DiffEntry] = []
        for key in sorted(set(old) | set(new)):
            child_path = f"{path}/{key}"
            if key not in old:
                differences.append(DiffEntry(child_path, None, new[key]))
            elif key not in new:
                differences.append(DiffEntry(child_path, old[key], None))
            else:
                differences.extend(deep_diff(old[key], new[key], child_path))
        return differences
    if isinstance(old, list):
        old_keyed = _keyed_list(old)
        new_keyed = _keyed_list(new)
        if old_keyed is not None and new_keyed is not None:
            return deep_diff(old_keyed, new_keyed, path)
        return [] if old == new else [DiffEntry(path or "/", old, new)]
    return [] if old == new else [DiffEntry(path or "/", old, new)]


TRIAL_MILESTONE_ROOTS = {
    "/start_milestone",
    "/end_milestone",
    "/original_end_milestone",
    "/end_time",
    "/trial_extensions",
}

STAGE_MILESTONE_FIELDS = {
    "desktop_first",
    "desktop_last",
    "android_first",
    "android_last",
    "ios_first",
    "ios_last",
    "webview_first",
    "webview_last",
    "extensions",
}

STATUS_ROOTS = {"/status", "/enabled", "/type"}

OT_CODE_FIELDS = {"trial_name", "ot_chromium_trial_name", "origin_trial_id"}

CODE_CONTRACT_FIELDS = {
    "origin_trial_feature_name",
    "origin_trial_type",
    "origin_trial_allows_third_party",
    "origin_trial_allows_insecure",
    "origin_trial_os",
    "base_feature",
    "base_feature_status",
    "copied_from_base_feature_if",
    "browser_process_read_access",
    "browser_process_read_write_access",
    "implied_by",
    "depends_on",
}


def is_milestone_diff(diff: DiffEntry) -> bool:
    if diff.path in TRIAL_MILESTONE_ROOTS:
        return True
    return diff.path.startswith("/stages/") and diff.path.rsplit("/", 1)[-1] in STAGE_MILESTONE_FIELDS


def is_status_diff(diff: DiffEntry) -> bool:
    return diff.path in STATUS_ROOTS


def is_ot_code_diff(diff: DiffEntry) -> bool:
    parts = [part for part in diff.path.split("/") if part]
    return bool(parts and parts[-1] in OT_CODE_FIELDS)


def is_contract_diff(diff: DiffEntry) -> bool:
    parts = [part for part in diff.path.split("/") if part]
    return bool(parts and parts[-1] in CODE_CONTRACT_FIELDS)


def partition_diffs(
    differences: Iterable[DiffEntry],
) -> tuple[list[DiffEntry], list[DiffEntry], list[DiffEntry]]:
    milestones: list[DiffEntry] = []
    statuses: list[DiffEntry] = []
    metadata: list[DiffEntry] = []
    for diff in differences:
        if is_milestone_diff(diff):
            milestones.append(diff)
        elif is_status_diff(diff):
            statuses.append(diff)
        else:
            metadata.append(diff)
    return milestones, statuses, metadata


def serialize_diffs(differences: Iterable[DiffEntry]) -> list[dict[str, Any]]:
    return [diff.as_dict() for diff in differences]
