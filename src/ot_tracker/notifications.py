from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Callable

from .config import TrackerConfig
from .db import TrackerDB


DISCORD_CHANNEL = "discord"
_WEBHOOK_PATH = re.compile(
    r"^/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+/?$"
)
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
_SEVERITY_COLOR = {
    "low": 0x95A5A6,
    "medium": 0xF1C40F,
    "high": 0xE74C3C,
}
_KST = timezone(timedelta(hours=9), name="KST")
_CATEGORY_LABELS = {
    "official_ot.registered": ("🆕", "신규 공개 OT 등록"),
    "official_ot.reappeared_in_feed": ("🔁", "공개 OT 피드 재등장"),
    "official_ot.removed_from_feed": ("🚨", "공개 OT 피드에서 사라짐"),
    "official_ot.status_changed": ("🚦", "OT 상태 변경"),
    "official_ot.milestone_changed": ("📅", "OT 마일스톤 변경"),
    "official_ot.metadata_changed": ("📝", "OT 기본정보 변경"),
    "official_ot.code_changed": ("🔑", "공개 OT 코드 변경"),
    "chromestatus.feature_tracking_started": ("🔎", "Chrome Status 추적 시작"),
    "chromestatus.feature_tracking_resumed": ("🔁", "Chrome Status 추적 재개"),
    "chromestatus.ot_code_changed": ("🔑", "Chrome Status OT 코드 변경"),
    "chromestatus.ot_stage_milestone_changed": ("📅", "OT stage 마일스톤 변경"),
    "chromestatus.feature_metadata_changed": ("📝", "Chrome Status 기본정보 변경"),
    "chromium.ot_declaration_added": ("🧩", "Chromium OT 선언 추가"),
    "chromium.ot_declaration_restored": ("🔁", "Chromium OT 선언 복원"),
    "chromium.ot_declaration_removed": ("🚨", "Chromium OT 선언 제거"),
    "chromium.ot_code_changed": ("🛠️", "Chromium OT 계약 변경"),
    "chromium.ot_source_file_changed": ("🛠️", "Chromium OT 소스 변경"),
    "chromium.release_ot_declaration_added": ("📦", "배포 채널 OT 선언 추가"),
    "chromium.release_ot_declaration_restored": ("🔁", "배포 채널 OT 선언 복원"),
    "chromium.release_ot_declaration_removed": ("🚨", "배포 채널 OT 선언 제거"),
    "chromium.release_ot_code_changed": ("🛠️", "배포 채널 OT 계약 변경"),
    "chromium.implementation_changed": ("💻", "OT 구현 코드 변경"),
    "chromium.ot_framework_changed": ("🏗️", "공통 OT 프레임워크 변경"),
    "chromium.premerge_change_detected": ("👀", "병합 전 OT 구현 CL 탐지"),
    "chromium.premerge_patchset_updated": ("🔄", "OT CL patchset 변경"),
    "chromium.premerge_framework_change_detected": ("🧪", "병합 전 OT 프레임워크 CL"),
    "chromium.premerge_change_merged": ("✅", "추적 중 OT CL 병합"),
    "chromium.premerge_change_abandoned": ("🗑️", "추적 중 OT CL 폐기"),
    "coverage.implementation_path_missing": ("🗺️", "OT 구현 경로 매핑 필요"),
    "coverage.implementation_path_added": ("✅", "OT 구현 경로 매핑 완료"),
    "coverage.runtime_declaration_missing": ("🚨", "활성 OT의 main 선언 누락"),
    "coverage.runtime_declaration_restored": ("✅", "활성 OT의 main 선언 복원"),
    "coverage.implementation_path_candidate_detected": ("🧭", "OT 구현 경로 후보 탐지"),
    "coverage.cross_source_contract_mismatch_detected": (
        "⚠️",
        "Chrome Status ↔ Chromium 계약 불일치",
    ),
    "coverage.cross_source_contract_mismatch_resolved": (
        "✅",
        "Chrome Status ↔ Chromium 계약 불일치 해소",
    ),
    "candidate.pre_registration_detected": ("🔭", "공식 등록 전 OT 후보"),
    "candidate.code_signal_detected": ("🔭", "Chromium 신규 OT 코드 후보"),
    "candidate.premerge_code_signal_detected": ("👀", "병합 전 신규 OT 코드 후보"),
    "candidate.premerge_patchset_updated": ("🔄", "신규 OT 후보 patchset 변경"),
    "candidate.signal_restored": ("🔁", "OT 후보 신호 복원"),
    "candidate.evidence_updated": ("🧪", "OT 후보 근거 변경"),
    "candidate.resolved_by_official_registration": ("✅", "후보가 공식 OT로 등록됨"),
    "candidate.signal_removed": ("🧹", "OT 후보 신호 제거"),
    "candidate.reclassified_as_noise": ("🧹", "OT 후보를 노이즈로 재분류"),
    "source.health_degraded": ("🚧", "추적 수집원 이상"),
    "source.health_recovered": ("💚", "추적 수집원 복구"),
}
_SOURCE_LABELS = {
    "chromestatus": "Chrome Status",
    "chromium": "Chromium main",
    "chromium_release": "Stable·Beta",
    "gerrit": "Chromium Gerrit",
    "tracker": "트래커",
}
_IMPLEMENTATION_CHANGE_CATEGORY = "chromium.implementation_changed"
_RELEASE_EVENT_CATEGORIES = {
    "chromium.release_ot_declaration_added",
    "chromium.release_ot_declaration_restored",
    "chromium.release_ot_declaration_removed",
    "chromium.release_ot_code_changed",
}
_RELEASE_LOCATION_KEYS = {"source_line", "source_excerpt"}
_MAX_DIGEST_CL_LINES = 6
_MAX_DIGEST_GROUPS_PER_MESSAGE = 6
_DIGEST_FIELD_VALUE_LIMIT = 700


class DiscordDeliveryError(RuntimeError):
    """A sanitized Discord error that never contains webhook or bot secrets."""


class DiscordNotConfigured(DiscordDeliveryError):
    """Discord delivery credentials or destination have not been configured."""


# Backward-compatible public name used by existing callers and tests.
DiscordWebhookError = DiscordDeliveryError


@dataclass(frozen=True)
class NotificationResult:
    channel: str
    status: str
    pending_before: int
    sent: int
    failed: int
    batches: int
    pending_after: int
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_discord_webhook_url(value: str) -> str:
    """Validate a Discord webhook without returning or logging it on failure."""
    cleaned = value.strip()
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise DiscordWebhookError("Discord webhook URL 형식이 올바르지 않습니다") from exc

    hostname = (parsed.hostname or "").casefold()
    valid_host = hostname in {
        "discord.com",
        "canary.discord.com",
        "ptb.discord.com",
        "discordapp.com",
        "canary.discordapp.com",
        "ptb.discordapp.com",
    }
    if (
        parsed.scheme.casefold() != "https"
        or not valid_host
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not _WEBHOOK_PATH.fullmatch(parsed.path)
    ):
        raise DiscordWebhookError("Discord 공식 Webhook URL이 아닙니다")
    return cleaned


def _strip_optional_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _validate_bot_token(value: str) -> str:
    token = _strip_optional_quotes(value)
    if len(token) < 30 or len(token) > 512 or any(character.isspace() for character in token):
        raise DiscordDeliveryError("Discord bot token 형식이 올바르지 않습니다")
    return token


def _bot_token(config: TrackerConfig) -> tuple[str, str]:
    environment_value = os.environ.get(config.discord.bot_token_env, "")
    if environment_value.strip():
        return _validate_bot_token(environment_value), "environment"

    token_file = config.discord.bot_token_file
    if token_file is None:
        raise DiscordNotConfigured(
            f"{config.discord.bot_token_env} 환경변수 또는 bot_token_file이 없습니다"
        )
    try:
        mode = token_file.stat().st_mode & 0o777
        content = token_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DiscordNotConfigured("Discord bot token 파일이 없습니다") from exc
    except OSError as exc:
        raise DiscordDeliveryError("Discord bot token 파일을 읽을 수 없습니다") from exc
    if mode & 0o077:
        raise DiscordDeliveryError("Discord bot token 파일 권한은 600이어야 합니다")

    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) == 1 and "=" not in lines[0]:
        return _validate_bot_token(lines[0]), "file"
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() in {
            config.discord.bot_token_env,
            "DISCORD_BOT_TOKEN",
        }:
            return _validate_bot_token(value), "file"
    raise DiscordNotConfigured("Discord bot token 파일에 토큰이 없습니다")


def _bot_channel_id(config: TrackerConfig) -> tuple[str, str]:
    environment_value = os.environ.get(config.discord.channel_id_env, "").strip()
    channel_id = environment_value or (config.discord.channel_id or "")
    if not channel_id:
        raise DiscordNotConfigured(
            f"{config.discord.channel_id_env} 환경변수 또는 channel_id가 없습니다"
        )
    if not re.fullmatch(r"\d{15,25}", channel_id):
        raise DiscordDeliveryError("Discord channel ID 형식이 올바르지 않습니다")
    return channel_id, "environment" if environment_value else "config"


def discord_configuration_status(config: TrackerConfig) -> dict[str, Any]:
    result: dict[str, Any] = {
        "enabled": config.discord.enabled,
        "transport": config.discord.transport,
        "configured": False,
        "valid": False,
        "min_severity": config.discord.min_severity,
        "implementation_digest_hours": (
            config.discord.implementation_digest_hours
        ),
    }
    if not config.discord.enabled:
        result["valid"] = True
        return result

    if config.discord.transport == "webhook":
        result["webhook_env"] = config.discord.webhook_env
        raw = os.environ.get(config.discord.webhook_env, "")
        result["configured"] = bool(raw.strip())
        if not raw.strip():
            result["error"] = f"{config.discord.webhook_env} 환경변수가 없습니다"
            return result
        try:
            validate_discord_webhook_url(raw)
        except DiscordDeliveryError as exc:
            result["error"] = str(exc)
            return result
    else:
        result.update(
            {
                "bot_token_env": config.discord.bot_token_env,
                "bot_token_file": config.discord.bot_token_file is not None,
                "channel_id_env": config.discord.channel_id_env,
            }
        )
        try:
            _, token_source = _bot_token(config)
            _, channel_source = _bot_channel_id(config)
        except DiscordDeliveryError as exc:
            result["error"] = str(exc)
            return result
        result["credential_source"] = token_source
        result["channel_source"] = channel_source
        result["configured"] = True
    result["valid"] = True
    return result


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _compact_value(value: Any, limit: int = 120) -> str:
    if value is None:
        return "∅"
    if isinstance(value, str):
        rendered = value.replace("\n", " ").strip()
    else:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _truncate(rendered or "∅", limit)


def _without_release_location_noise(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_release_location_noise(item)
            for key, item in value.items()
            if key not in _RELEASE_LOCATION_KEYS
        }
    if isinstance(value, list):
        return [_without_release_location_noise(item) for item in value]
    return value


def _release_runtime_feature(event: dict[str, Any]) -> str | None:
    evidence = event.get("evidence") or {}
    runtime_feature = evidence.get("runtime_feature_name")
    if runtime_feature:
        return str(runtime_feature)
    for payload_name in ("new", "old"):
        payload = event.get(payload_name)
        if not isinstance(payload, dict):
            continue
        runtime_feature = payload.get("runtime_feature_name")
        if runtime_feature:
            return str(runtime_feature)
        declaration = payload.get("declaration")
        if isinstance(declaration, dict) and declaration.get("runtime_feature_name"):
            return str(declaration["runtime_feature_name"])
    entity_key = str(event.get("entity_key") or "")
    _, separator, runtime_feature = entity_key.partition(":")
    return runtime_feature if separator and runtime_feature else None


def _release_trial_name(event: dict[str, Any]) -> str | None:
    evidence = event.get("evidence") or {}
    if evidence.get("trial_name"):
        return str(evidence["trial_name"])
    for payload_name in ("new", "old"):
        payload = event.get(payload_name)
        if not isinstance(payload, dict):
            continue
        if payload.get("trial_name"):
            return str(payload["trial_name"])
        declaration = payload.get("declaration")
        if isinstance(declaration, dict) and declaration.get("trial_name"):
            return str(declaration["trial_name"])
    return None


def _release_changes(event: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = event.get("evidence") or {}
    changes = evidence.get("changes")
    if not isinstance(changes, list):
        return []
    normalized: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "value")
        if path.rsplit("/", 1)[-1] in _RELEASE_LOCATION_KEYS:
            continue
        normalized.append(
            {
                "path": path,
                "old": _without_release_location_noise(change.get("old")),
                "new": _without_release_location_noise(change.get("new")),
            }
        )
    return sorted(
        normalized,
        key=lambda change: (
            change["path"],
            json.dumps(change, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )


def _release_declaration_contract(event: dict[str, Any]) -> dict[str, Any] | None:
    category = str(event.get("category") or "")
    payload_name = "old" if category == "chromium.release_ot_declaration_removed" else "new"
    payload = event.get(payload_name)
    if not isinstance(payload, dict):
        return None
    declaration = payload.get("declaration")
    if not isinstance(declaration, dict):
        return None
    normalized = _without_release_location_noise(declaration)
    if not isinstance(normalized, dict):
        return None
    normalized.pop("runtime_feature_name", None)
    fields = normalized.get("fields")
    if isinstance(fields, dict):
        normalized["fields"] = {
            key: value for key, value in fields.items() if key != "name"
        }
    return normalized


def _release_group_signature(event: dict[str, Any]) -> str | None:
    category = str(event.get("category") or "")
    if category not in _RELEASE_EVENT_CATEGORIES:
        return None
    trial_name = _release_trial_name(event)
    evidence = event.get("evidence") or {}
    if not trial_name or not evidence.get("channel"):
        return None
    semantic: Any
    if category == "chromium.release_ot_code_changed":
        semantic = _release_changes(event)
    else:
        semantic = _release_declaration_contract(event)
    if semantic is None:
        return None
    material = {
        "category": category,
        "trial_name": trial_name.casefold(),
        "channel": str(evidence.get("channel") or "").casefold(),
        "platform": evidence.get("platform"),
        "milestone": evidence.get("milestone"),
        "version": evidence.get("version"),
        "revision": evidence.get("revision"),
        "semantic": semantic,
    }
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _collapse_release_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine equivalent per-runtime release events for Discord presentation."""
    collapsed: list[dict[str, Any]] = []
    group_indexes: dict[str, int] = {}
    for original in events:
        category = str(original.get("category") or "")
        if category not in _RELEASE_EVENT_CATEGORIES:
            collapsed.append(original)
            continue

        event = dict(original)
        evidence = dict(original.get("evidence") or {})
        event["evidence"] = evidence
        if isinstance(evidence.get("changes"), list):
            evidence["changes"] = _release_changes(event)
        event_id = event.get("id")
        evidence["grouped_event_ids"] = [event_id] if event_id is not None else []
        runtime_feature = _release_runtime_feature(event)
        evidence["release_runtime_features"] = (
            [runtime_feature] if runtime_feature else []
        )

        signature = _release_group_signature(event)
        if signature is None or signature not in group_indexes:
            if signature is not None:
                group_indexes[signature] = len(collapsed)
            collapsed.append(event)
            continue

        grouped = collapsed[group_indexes[signature]]
        grouped_evidence = grouped["evidence"]
        if event_id is not None and event_id not in grouped_evidence["grouped_event_ids"]:
            grouped_evidence["grouped_event_ids"].append(event_id)
        if (
            runtime_feature
            and runtime_feature not in grouped_evidence["release_runtime_features"]
        ):
            grouped_evidence["release_runtime_features"].append(runtime_feature)
        if _SEVERITY_RANK.get(str(event.get("severity") or "low"), 0) > _SEVERITY_RANK.get(
            str(grouped.get("severity") or "low"), 0
        ):
            grouped["severity"] = event.get("severity")
    return collapsed


def _event_subject(event: dict[str, Any]) -> str:
    evidence = event.get("evidence") or {}
    candidates: list[Any] = [
        evidence.get("display_name"),
        evidence.get("source_name"),
        evidence.get("feature_name"),
        evidence.get("trial_name"),
    ]
    declared_trial_names = evidence.get("declared_trial_names")
    if isinstance(declared_trial_names, list) and declared_trial_names:
        candidates.append(", ".join(str(name) for name in declared_trial_names))
    for payload_name in ("new", "old"):
        payload = event.get(payload_name)
        if not isinstance(payload, dict):
            continue
        candidates.extend(
            (
                payload.get("display_name"),
                payload.get("trial_name"),
                (payload.get("change") or {}).get("subject")
                if isinstance(payload.get("change"), dict)
                else None,
            )
        )
    candidates.append(event.get("entity_key"))
    return _truncate(next((str(item) for item in candidates if item), "unknown"), 160)


def _event_url(event: dict[str, Any]) -> str | None:
    evidence = event.get("evidence") or {}
    for key in ("chromestatus_url", "change_url", "url"):
        value = evidence.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    for payload_name in ("new", "old"):
        payload = event.get(payload_name)
        if not isinstance(payload, dict):
            continue
        for key in ("chromestatus_url", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("https://"):
                return value
        change = payload.get("change")
        if isinstance(change, dict):
            value = change.get("url")
            if isinstance(value, str) and value.startswith("https://"):
                return value
    return None


def _stage_extension_labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for extension in value:
        if not isinstance(extension, dict):
            continue
        milestones: list[str] = []
        for platform in ("desktop", "android", "ios", "webview"):
            first = extension.get(f"{platform}_first")
            last = extension.get(f"{platform}_last")
            if first is not None and last is not None:
                milestone = (
                    f"{platform} M{first}"
                    if first == last
                    else f"{platform} M{first}–{last}"
                )
            elif first is not None:
                milestone = f"{platform} 시작 M{first}"
            elif last is not None:
                milestone = f"{platform} 종료 M{last}"
            else:
                continue
            milestones.append(milestone)
        if milestones:
            labels.append(", ".join(milestones))
        elif extension.get("id") is not None:
            labels.append(f"stage #{extension['id']}")
    return labels


def _stage_extension_reason(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    for extension in value:
        if not isinstance(extension, dict):
            continue
        reason = extension.get("experiment_extension_reason")
        if reason:
            return str(reason)
    return None


def _event_change_payload(event: dict[str, Any]) -> dict[str, Any]:
    for payload_name in ("new", "old"):
        payload = event.get(payload_name)
        if not isinstance(payload, dict):
            continue
        change = payload.get("change")
        if isinstance(change, dict):
            return change
    return {}


def _event_diff_summary(event: dict[str, Any]) -> dict[str, Any] | None:
    evidence = event.get("evidence") or {}
    summary = evidence.get("diff_summary")
    if isinstance(summary, dict):
        return summary
    change = _event_change_payload(event)
    summary = change.get("diff_summary")
    return summary if isinstance(summary, dict) else None


def _change_lines(event: dict[str, Any]) -> list[str]:
    evidence = event.get("evidence") or {}
    changes = evidence.get("changes")
    lines: list[str] = []
    channel = evidence.get("channel")
    if channel:
        release = f"{channel}"
        if evidence.get("milestone") is not None:
            release += f" M{evidence['milestone']}"
        if evidence.get("version"):
            release += f" · {evidence['version']}"
        if evidence.get("platform"):
            release += f" · {evidence['platform']}"
        lines.append(f"배포 기준: `{_truncate(release, 120)}`")
    release_runtime_features = evidence.get("release_runtime_features")
    if isinstance(release_runtime_features, list) and len(release_runtime_features) > 1:
        shown_features = ", ".join(
            f"`{_truncate(str(name), 65)}`"
            for name in release_runtime_features[:4]
        )
        if len(release_runtime_features) > 4:
            shown_features += f" 외 {len(release_runtime_features) - 4}개"
        lines.append(
            f"같은 OT에 연결된 Runtime feature {len(release_runtime_features)}개: "
            f"{shown_features}"
        )
    error = evidence.get("error")
    if error:
        lines.append(f"원인: `{_truncate(str(error), 150)}`")
    if "fetched_total" in evidence or "preserved_total" in evidence:
        lines.append(
            "OT 레코드: "
            f"수집 `{evidence.get('fetched_total', '—')}` / "
            f"기존 보존 `{evidence.get('preserved_total', '—')}`"
        )
    if "fetched_count" in evidence or "preserved_count" in evidence:
        lines.append(
            "OT 선언: "
            f"수집 `{evidence.get('fetched_count', '—')}` / "
            f"기존 보존 `{evidence.get('preserved_count', '—')}`"
        )
    if evidence.get("failed_count"):
        examples = evidence.get("examples") or []
        line = f"실패 `{evidence['failed_count']}`건"
        if examples:
            line += " · " + ", ".join(
                f"`{_truncate(str(item), 50)}`" for item in examples[:3]
            )
        lines.append(line)
    release_presence = evidence.get("release_presence")
    if isinstance(release_presence, list):
        lines.append(
            "Stable/Beta: "
            + (
                ", ".join(f"`{_truncate(str(item), 80)}`" for item in release_presence)
                if release_presence
                else "선언 없음"
            )
        )
    if isinstance(changes, list):
        for change in changes[:4]:
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "value")
            if path.endswith("/extensions"):
                old_extensions = _stage_extension_labels(change.get("old"))
                new_extensions = _stage_extension_labels(change.get("new"))
                if old_extensions or new_extensions:
                    lines.append(
                        "OT 연장: "
                        f"`{_truncate(', '.join(old_extensions) or '없음', 85)}` → "
                        f"`{_truncate(', '.join(new_extensions) or '없음', 85)}`"
                    )
                    reason = _stage_extension_reason(change.get("new"))
                    if reason:
                        lines.append(f"연장 사유: {_truncate(reason, 130)}")
                    continue
            lines.append(
                f"`{_truncate(path, 90)}`: "
                f"{_compact_value(change.get('old'), 85)} → "
                f"{_compact_value(change.get('new'), 85)}"
            )
        if len(changes) > 4:
            lines.append(f"외 {len(changes) - 4}개 필드")
    elif event.get("field_path"):
        lines.append(
            f"`{_truncate(str(event['field_path']), 90)}`: "
            f"{_compact_value(event.get('old'), 85)} → "
            f"{_compact_value(event.get('new'), 85)}"
        )
    if (
        evidence.get("field")
        and "console" in evidence
        and "chromium" in evidence
    ):
        relationship = "=" if evidence["console"] == evidence["chromium"] else "≠"
        lines.append(
            f"`{_truncate(str(evidence['field']), 90)}`: "
            f"Chrome Status `{evidence['console']}` {relationship} "
            f"Chromium `{evidence['chromium']}`"
        )
        runtime_features = evidence.get("runtime_features")
        if isinstance(runtime_features, list) and runtime_features:
            lines.append(
                "Runtime feature: "
                + ", ".join(
                    f"`{_truncate(str(name), 70)}`"
                    for name in runtime_features[:3]
                )
            )
    patchset = evidence.get("new_patchset", evidence.get("patchset"))
    old_patchset = evidence.get("old_patchset")
    if patchset is not None:
        if old_patchset is not None and old_patchset != patchset:
            lines.append(f"patchset `{old_patchset}` → `{patchset}`")
        else:
            lines.append(f"patchset `{patchset}`")
    candidate_path = evidence.get("candidate_path")
    if candidate_path:
        applied = "자동 적용" if evidence.get("auto_applied") else "검토 필요"
        lines.append(
            f"경로 `{_truncate(str(candidate_path), 105)}` · "
            f"score {evidence.get('score', '—')} · {applied}"
        )
    matched_paths = [
        str(path)
        for path in evidence.get("matched_paths") or []
        if path
    ]
    if matched_paths:
        lines.append(
            "매칭 파일: "
            + ", ".join(
                f"`{_truncate(path, 65)}`" for path in matched_paths[:2]
            )
        )
    summary = _event_diff_summary(event)
    if isinstance(summary, dict):
        declared_trial_names = summary.get("added_origin_trial_feature_names")
        if isinstance(declared_trial_names, list) and declared_trial_names:
            lines.append(
                "추가 OT 선언: "
                + ", ".join(
                    f"`{_truncate(str(name), 70)}`"
                    for name in declared_trial_names[:3]
                )
            )
        targeted_files = summary.get("targeted_files")
        if isinstance(targeted_files, list) and targeted_files:
            lines.append(
                "대형 CL 정밀 검사: "
                + ", ".join(
                    f"`{_truncate(str(path).rsplit('/', 1)[-1], 70)}`"
                    for path in targeted_files[:2]
                )
            )
        lines.append(
            f"파일 {summary.get('files_changed', 0)}개 · "
            f"+{summary.get('insertions', 0)}/-{summary.get('deletions', 0)}"
        )
        top_files = [
            str(item.get("path") or "")
            for item in summary.get("top_files") or []
            if isinstance(item, dict) and item.get("path")
        ]
        if top_files and not matched_paths:
            lines.append(
                "주요 파일: "
                + ", ".join(
                    f"`{_truncate(path, 65)}`" for path in top_files[:2]
                )
            )
        contexts = [
            *[str(value) for value in summary.get("changed_symbols") or []],
            *[str(value) for value in summary.get("hunk_contexts") or []],
        ]
        if contexts and not matched_paths:
            lines.append(
                "함수/구간: "
                + ", ".join(
                    f"`{_truncate(value, 70)}`" for value in contexts[:2]
                )
            )
    return lines


def _event_field(event: dict[str, Any]) -> dict[str, Any]:
    category = str(event.get("category") or "unknown")
    emoji, label = _CATEGORY_LABELS.get(category, ("🔔", category))
    subject = _event_subject(event).replace("`", "ʼ")
    evidence = event.get("evidence") or {}
    grouped_event_ids = evidence.get("grouped_event_ids")
    event_reference = f"event #{event.get('id')}"
    if isinstance(grouped_event_ids, list) and len(grouped_event_ids) > 1:
        numeric_ids = sorted(
            int(event_id)
            for event_id in grouped_event_ids
            if isinstance(event_id, int) or str(event_id).isdigit()
        )
        if len(numeric_ids) == len(grouped_event_ids):
            contiguous = numeric_ids == list(range(numeric_ids[0], numeric_ids[-1] + 1))
            event_reference = (
                f"events #{numeric_ids[0]}–#{numeric_ids[-1]}"
                if contiguous
                else "events " + ", ".join(f"#{event_id}" for event_id in numeric_ids)
            )
    value_lines = [
        f"**{subject}**",
        f"`{event.get('severity', 'unknown')}` · "
        f"`{event.get('source', 'unknown')}` · {event_reference}",
    ]
    value_lines.extend(_change_lines(event))
    url = _event_url(event)
    if url:
        value_lines.append(f"[근거 보기]({url})")
    observed_at = _format_kst(event.get("observed_at"))
    if observed_at:
        value_lines.append(observed_at)
    return {
        "name": _truncate(f"{emoji} {label}", 256),
        "value": _truncate("\n".join(value_lines), 640),
        "inline": False,
    }


def _format_kst(value: Any) -> str:
    """Render stored UTC event times explicitly in Korea Standard Time."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(_KST).strftime("%Y-%m-%d %H:%M KST")


def _event_batch_description(events: list[dict[str, Any]]) -> str:
    labels: list[str] = []
    sources: list[str] = []
    for event in events:
        category = str(event.get("category") or "unknown")
        label = _CATEGORY_LABELS.get(category, ("🔔", category))[1]
        if label not in labels:
            labels.append(label)
        source = str(event.get("source") or "unknown")
        if source not in sources:
            sources.append(source)

    if len(sources) == 1:
        source_label = _SOURCE_LABELS.get(sources[0], sources[0])
        prefix = f"{source_label} "
        compact_labels = [
            label[len(prefix) :] if label.startswith(prefix) else label
            for label in labels
        ]
        return (
            f"{source_label}에서 다음 변화를 감지했습니다: "
            + " · ".join(compact_labels)
        )
    return "다음 변화를 감지했습니다: " + " · ".join(labels)


def _parse_event_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _implementation_digest_cutoff(now: datetime, window_hours: int) -> datetime:
    local_now = now.astimezone(_KST)
    bucket_hour = (local_now.hour // window_hours) * window_hours
    return local_now.replace(
        hour=bucket_hour,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(UTC)


def _ready_implementation_digest_groups(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    window_hours: int,
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    cutoff = (
        _implementation_digest_cutoff(now, window_hours)
        if window_hours > 0
        else now
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    deferred: list[dict[str, Any]] = []
    for event in events:
        observed_at = _parse_event_time(event.get("observed_at"))
        retry = int(event.get("delivery_attempts") or 0) > 0
        ready = (
            window_hours == 0
            or retry
            or observed_at is None
            or observed_at < cutoff
        )
        if not ready:
            deferred.append(event)
            continue
        groups.setdefault(_event_subject(event), []).append(event)
    return list(groups.values()), deferred


def _implementation_digest_line(event: dict[str, Any]) -> str:
    change = _event_change_payload(event)
    number = change.get("change_number")
    if number is None:
        number = str(event.get("entity_key") or "").split(":", 1)[0]
    label = f"CL {number}" if number else f"event #{event.get('id')}"
    url = _event_url(event)
    rendered_label = f"[{label}]({url})" if url else f"`{label}`"

    details: list[str] = []
    subject = str(change.get("subject") or "").strip()
    if subject:
        details.append(_truncate(subject, 40))
    summary = _event_diff_summary(event)
    if summary is not None:
        details.append(
            f"파일 {summary.get('files_changed', 0)}개 "
            f"(+{summary.get('insertions', 0)}/-{summary.get('deletions', 0)})"
        )
    suffix = " · " + " · ".join(details) if details else ""
    return f"• {rendered_label}{suffix}"


def _implementation_digest_field(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    subject = _event_subject(events[0]).replace("`", "ʼ")
    change_lines = [
        _implementation_digest_line(event)
        for event in events[:_MAX_DIGEST_CL_LINES]
    ]
    first_seen = _format_kst(events[0].get("observed_at"))
    last_seen = _format_kst(events[-1].get("observed_at"))
    tail: list[str] = []
    if first_seen and last_seen and first_seen != last_seen:
        tail.append(f"감지 구간: `{first_seen}` → `{last_seen}`")
    elif first_seen:
        tail.append(f"감지 시각: `{first_seen}`")
    tail.append(
        f"event #{events[0].get('id')}–#{events[-1].get('id')} · "
        "전송 실패 시 자동 재시도"
    )

    while True:
        hidden_count = len(events) - len(change_lines)
        lines = list(change_lines)
        if hidden_count:
            lines.append(f"• 외 {hidden_count}개 CL")
        lines.extend(tail)
        rendered = "\n".join(lines)
        if len(rendered) <= _DIGEST_FIELD_VALUE_LIMIT or len(change_lines) <= 1:
            break
        change_lines.pop()
    return {
        "name": _truncate(f"💻 {subject} · 병합 CL {len(events)}건", 160),
        "value": _truncate(rendered, _DIGEST_FIELD_VALUE_LIMIT),
        "inline": False,
    }


def build_discord_implementation_digest_payload(
    config: TrackerConfig,
    groups: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    if not groups or any(not group for group in groups):
        raise ValueError("at least one non-empty implementation group is required")
    if len(groups) > _MAX_DIGEST_GROUPS_PER_MESSAGE:
        raise ValueError("too many implementation groups for one Discord message")
    events = [event for group in groups for event in group]
    highest = max(
        (str(event.get("severity") or "low") for event in events),
        key=lambda severity: _SEVERITY_RANK.get(severity, 0),
    )
    window_hours = config.discord.implementation_digest_hours
    cadence = (
        f"KST 기준 {window_hours}시간 단위"
        if window_hours > 0
        else "현재 실행 단위"
    )
    payload: dict[str, Any] = {
        "username": config.discord.username,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": (
                    f"Chromium OT 구현 변경 요약 · "
                    f"{len(groups)}개 OT / {len(events)}건"
                ),
                "description": (
                    f"{cadence}로 병합된 구현 CL을 OT별로 묶었습니다. "
                    "OT 등록·마일스톤·계약 변경은 별도로 즉시 알립니다."
                ),
                "color": _SEVERITY_COLOR.get(highest, _SEVERITY_COLOR["low"]),
                "fields": [_implementation_digest_field(group) for group in groups],
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "footer": {
                    "text": "병합된 구현 코드만 포함 · 전송 실패 이벤트는 자동 재시도"
                },
            }
        ],
    }
    if config.discord.avatar_url:
        payload["avatar_url"] = config.discord.avatar_url
    return payload


def build_discord_event_payload(
    config: TrackerConfig, events: list[dict[str, Any]]
) -> dict[str, Any]:
    if not events:
        raise ValueError("at least one event is required")
    highest = max(
        (str(event.get("severity") or "low") for event in events),
        key=lambda severity: _SEVERITY_RANK.get(severity, 0),
    )
    display_events = _collapse_release_events(events)
    payload: dict[str, Any] = {
        "username": config.discord.username,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"Chrome Origin Trial 변경 {len(events)}건",
                "description": _event_batch_description(events),
                "color": _SEVERITY_COLOR.get(highest, _SEVERITY_COLOR["low"]),
                "fields": [_event_field(event) for event in display_events],
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "footer": {
                    "text": (
                        f"event #{events[0]['id']}–#{events[-1]['id']} · "
                        "전송 실패 이벤트는 자동 재시도"
                    )
                },
            }
        ],
    }
    if config.discord.avatar_url:
        payload["avatar_url"] = config.discord.avatar_url
    return payload


def build_discord_test_payload(config: TrackerConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": config.discord.username,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "✅ Chrome OT Tracker 연결 완료",
                "description": (
                    "이 채널로 신규 OT, 기본정보·코드·마일스톤 변경, "
                    "공식 등록 전 Chromium 후보를 계속 알립니다."
                ),
                "color": 0x2ECC71,
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "footer": {"text": f"알림 기준: {config.discord.min_severity} 이상"},
            }
        ],
    }
    if config.discord.avatar_url:
        payload["avatar_url"] = config.discord.avatar_url
    return payload


def build_discord_heartbeat_payload(
    config: TrackerConfig,
    *,
    run_id: int,
    stats: dict[str, Any],
) -> dict[str, Any]:
    sources = stats.get("sources") or {}
    chromestatus = sources.get("chromestatus") or {}
    chromium = sources.get("chromium") or {}
    gerrit = sources.get("gerrit") or {}
    revision = str(chromium.get("revision") or "unknown")
    payload: dict[str, Any] = {
        "username": config.discord.username,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "💚 Chrome OT Tracker 정상 작동 중",
                "description": "최근 수집·상태 저장·Discord 연결이 정상입니다.",
                "color": 0x2ECC71,
                "fields": [
                    {
                        "name": "추적 상태",
                        "value": (
                            f"활성 OT **{chromestatus.get('active_trials', '—')}개** · "
                            f"전체 **{chromestatus.get('total_trials', '—')}개**\n"
                            f"Chromium `{revision[:12]}` · "
                            f"Gerrit 최근 변경 {gerrit.get('changes_scanned', '—')}개"
                        ),
                        "inline": False,
                    }
                ],
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "footer": {
                    "text": (
                        f"run #{run_id} · 다음 heartbeat는 약 "
                        f"{config.health.heartbeat_interval_hours}시간 후"
                    )
                },
            }
        ],
    }
    if config.discord.avatar_url:
        payload["avatar_url"] = config.discord.avatar_url
    return payload


def _with_wait_query(webhook_url: str) -> str:
    parsed = urllib.parse.urlsplit(webhook_url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() != "wait"
    ]
    query.append(("wait", "true"))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
    )


def _retry_after(error: urllib.error.HTTPError) -> float:
    header = error.headers.get("Retry-After") if error.headers else None
    body_value: Any = None
    try:
        body = json.loads(error.read().decode("utf-8"))
        if isinstance(body, dict):
            body_value = body.get("retry_after")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    for value in (body_value, header):
        try:
            return min(60.0, max(0.1, float(value)))
        except (TypeError, ValueError):
            continue
    return 1.0


def _execute_discord_request(
    request: urllib.request.Request,
    *,
    timeout_seconds: float,
    retries: int,
    operation: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            with opener(request, timeout=timeout_seconds) as response:
                status_value = getattr(response, "status", None)
                status = int(
                    status_value if status_value is not None else response.getcode()
                )
                response.read()
            if 200 <= status < 300:
                return
            raise DiscordDeliveryError(f"Discord {operation} 응답 오류 (HTTP {status})")
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt + 1 < attempts:
                sleep(_retry_after(exc) if exc.code == 429 else min(2**attempt, 10))
                continue
            if exc.code == 429:
                raise DiscordDeliveryError(
                    f"Discord {operation} 요청 제한 (HTTP 429)"
                ) from exc
            raise DiscordDeliveryError(
                f"Discord {operation} 전송 실패 (HTTP {exc.code})"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt + 1 < attempts:
                sleep(min(2**attempt, 10))
                continue
            raise DiscordDeliveryError(
                f"Discord {operation} 네트워크 전송 실패"
            ) from exc


def execute_discord_webhook(
    webhook_url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    retries: int,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    validated = validate_discord_webhook_url(webhook_url)
    request = urllib.request.Request(
        _with_wait_query(validated),
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "chrome-ot-tracker/0.1",
        },
        method="POST",
    )
    _execute_discord_request(
        request,
        timeout_seconds=timeout_seconds,
        retries=retries,
        operation="webhook",
        opener=opener,
        sleep=sleep,
    )


def execute_discord_bot(
    bot_token: str,
    channel_id: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    retries: int,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    token = _validate_bot_token(bot_token)
    if not re.fullmatch(r"\d{15,25}", channel_id):
        raise DiscordDeliveryError("Discord channel ID 형식이 올바르지 않습니다")
    bot_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"username", "avatar_url"}
    }
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=json.dumps(
            bot_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "chrome-ot-tracker/0.1",
        },
        method="POST",
    )
    _execute_discord_request(
        request,
        timeout_seconds=timeout_seconds,
        retries=retries,
        operation="bot message",
        opener=opener,
        sleep=sleep,
    )


def _discord_sender(
    config: TrackerConfig,
    *,
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
) -> Callable[[dict[str, Any]], None]:
    if config.discord.transport == "webhook":
        webhook_url = os.environ.get(config.discord.webhook_env, "").strip()
        if not webhook_url:
            raise DiscordNotConfigured(
                f"{config.discord.webhook_env} 환경변수가 없습니다"
            )
        validated = validate_discord_webhook_url(webhook_url)

        def send_webhook(payload: dict[str, Any]) -> None:
            execute_discord_webhook(
                validated,
                payload,
                timeout_seconds=config.http_timeout_seconds,
                retries=config.discord.retries,
                opener=opener,
                sleep=sleep,
            )

        return send_webhook

    bot_token, _ = _bot_token(config)
    channel_id, _ = _bot_channel_id(config)

    def send_bot(payload: dict[str, Any]) -> None:
        execute_discord_bot(
            bot_token,
            channel_id,
            payload,
            timeout_seconds=config.http_timeout_seconds,
            retries=config.discord.retries,
            opener=opener,
            sleep=sleep,
        )

    return send_bot


def deliver_discord_pending(
    config: TrackerConfig,
    db: TrackerDB,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> NotificationResult:
    before = db.notification_summary(
        channel=DISCORD_CHANNEL, min_severity=config.discord.min_severity
    )
    pending_before = before["pending"]
    if not config.discord.enabled:
        return NotificationResult(
            DISCORD_CHANNEL, "disabled", pending_before, 0, 0, 0, pending_before
        )
    if pending_before == 0:
        return NotificationResult(DISCORD_CHANNEL, "idle", 0, 0, 0, 0, 0)

    now = (now or datetime.now(UTC)).astimezone(UTC)
    events = db.pending_notification_events(
        channel=DISCORD_CHANNEL,
        min_severity=config.discord.min_severity,
        limit=max(1, pending_before),
    )
    implementation_events = [
        event
        for event in events
        if event.get("category") == _IMPLEMENTATION_CHANGE_CATEGORY
    ]
    immediate_events = [
        event
        for event in events
        if event.get("category") != _IMPLEMENTATION_CHANGE_CATEGORY
    ]
    digest_groups, deferred_events = _ready_implementation_digest_groups(
        implementation_events,
        now=now,
        window_hours=config.discord.implementation_digest_hours,
    )

    plans: list[tuple[list[int], dict[str, Any]]] = []
    for offset in range(0, len(immediate_events), config.discord.batch_size):
        batch = immediate_events[offset : offset + config.discord.batch_size]
        plans.append(
            (
                [int(event["id"]) for event in batch],
                build_discord_event_payload(config, batch),
            )
        )
    digest_batch_size = min(
        config.discord.batch_size,
        _MAX_DIGEST_GROUPS_PER_MESSAGE,
    )
    for offset in range(0, len(digest_groups), digest_batch_size):
        group_batch = digest_groups[offset : offset + digest_batch_size]
        plans.append(
            (
                [
                    int(event["id"])
                    for group in group_batch
                    for event in group
                ],
                build_discord_implementation_digest_payload(config, group_batch),
            )
        )

    if not plans:
        return NotificationResult(
            DISCORD_CHANNEL,
            "deferred",
            pending_before,
            0,
            0,
            0,
            pending_before,
        )

    try:
        sender = _discord_sender(config, opener=opener, sleep=sleep)
    except DiscordNotConfigured as exc:
        return NotificationResult(
            DISCORD_CHANNEL,
            "not_configured",
            pending_before,
            0,
            0,
            0,
            pending_before,
            str(exc),
        )
    except DiscordDeliveryError as exc:
        return NotificationResult(
            DISCORD_CHANNEL,
            "invalid_configuration",
            pending_before,
            0,
            0,
            0,
            pending_before,
            str(exc),
        )

    sent = 0
    failed = 0
    batches = 0
    error: str | None = None
    for event_ids, payload in plans[: config.discord.max_batches_per_run]:
        batches += 1
        try:
            sender(payload)
        except DiscordDeliveryError as exc:
            error = str(exc)
            failed += len(event_ids)
            db.record_notification_delivery(
                channel=DISCORD_CHANNEL,
                event_ids=event_ids,
                status="failed",
                error=error,
            )
            break
        db.record_notification_delivery(
            channel=DISCORD_CHANNEL,
            event_ids=event_ids,
            status="sent",
        )
        sent += len(event_ids)

    after = db.notification_summary(
        channel=DISCORD_CHANNEL, min_severity=config.discord.min_severity
    )
    if failed:
        status = "partial" if sent else "failed"
    elif len(plans) > batches:
        status = "partial"
        error = "한 실행의 Discord 배치 한도에 도달했습니다"
    elif deferred_events:
        status = "deferred"
    elif after["pending"]:
        status = "partial"
        error = "Discord 미전송 이벤트가 남아 있습니다"
    else:
        status = "sent"
    return NotificationResult(
        DISCORD_CHANNEL,
        status,
        pending_before,
        sent,
        failed,
        batches,
        after["pending"],
        error,
    )


def send_discord_test(
    config: TrackerConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if not config.discord.enabled:
        raise DiscordDeliveryError("config.toml에서 Discord 알림이 비활성화되어 있습니다")
    sender = _discord_sender(config, opener=opener, sleep=sleep)
    sender(build_discord_test_payload(config))


def maybe_send_discord_heartbeat(
    config: TrackerConfig,
    db: TrackerDB,
    *,
    run_id: int,
    run_status: str,
    stats: dict[str, Any],
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not config.health.heartbeat_enabled:
        return {"status": "disabled", "sent": False}
    if not config.discord.enabled:
        return {"status": "discord_disabled", "sent": False}
    if run_status != "complete":
        return {"status": "run_not_complete", "sent": False}

    now = (now or datetime.now(UTC)).astimezone(UTC)
    last_raw = db.get_meta("notification.discord.heartbeat_sent_at")
    last_sent: datetime | None = None
    if last_raw:
        try:
            last_sent = datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=UTC)
            last_sent = last_sent.astimezone(UTC)
        except ValueError:
            last_sent = None
    interval = timedelta(hours=config.health.heartbeat_interval_hours)
    if last_sent is not None and now - last_sent < interval:
        return {
            "status": "not_due",
            "sent": False,
            "last_sent_at": last_sent.isoformat(timespec="seconds"),
        }

    try:
        sender = _discord_sender(config, opener=opener, sleep=sleep)
        sender(build_discord_heartbeat_payload(config, run_id=run_id, stats=stats))
    except DiscordDeliveryError as exc:
        return {"status": "failed", "sent": False, "error": str(exc)}
    sent_at = now.isoformat(timespec="seconds")
    db.set_meta("notification.discord.heartbeat_sent_at", sent_at)
    db.commit_changes()
    return {"status": "sent", "sent": True, "sent_at": sent_at}
