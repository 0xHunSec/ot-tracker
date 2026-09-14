from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_FILES = (
    "third_party/blink/renderer/platform/runtime_enabled_features.json5",
    "third_party/blink/common/origin_trials/manual_completion_origin_trial_features.cc",
    "third_party/blink/common/origin_trials/navigation_origin_trial_features.cc",
    "third_party/blink/common/origin_trials/persistent_origin_trials.cc",
)

DEFAULT_IGNORE_PATTERNS = (
    "Frobulate*",
    "OriginTrialsSampleAPI*",
    "TestFeature*OriginTrial*",
    "ForceTouchEventFeatureDetectionForInspector",
)


@dataclass(frozen=True)
class TargetRule:
    name: str
    aliases: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscordConfig:
    enabled: bool = True
    transport: str = "webhook"
    webhook_env: str = "OT_TRACKER_DISCORD_WEBHOOK_URL"
    bot_token_env: str = "DISCORD_BOT_TOKEN"
    bot_token_file: Path | None = None
    channel_id_env: str = "DISCORD_CHANNEL_ID"
    channel_id: str | None = None
    username: str = "Chrome OT Tracker"
    avatar_url: str | None = None
    min_severity: str = "medium"
    implementation_digest_hours: int = 6
    batch_size: int = 8
    max_batches_per_run: int = 10
    retries: int = 3


@dataclass(frozen=True)
class HealthConfig:
    heartbeat_enabled: bool = True
    heartbeat_interval_hours: int = 24


@dataclass(frozen=True)
class TrackerConfig:
    config_path: Path
    database_path: Path
    reports_dir: Path
    recent_event_days: int = 14
    http_timeout_seconds: float = 30.0
    http_retries: int = 3
    user_agent: str = "chrome-ot-tracker/0.1"
    chromestatus_base_url: str = "https://chromestatus.com/api/v0"
    feature_fetch_workers: int = 8
    gitiles_base_url: str = "https://chromium.googlesource.com/chromium/src"
    chromium_ref: str = "refs/heads/main"
    chromium_source_files: tuple[str, ...] = DEFAULT_SOURCE_FILES
    chromiumdash_base_url: str = "https://chromiumdash.appspot.com"
    # Programmatic callers opt in explicitly. load_config enables Stable/Beta by
    # default, and the shipped config also declares them.
    chromium_release_channels: tuple[str, ...] = ()
    chromium_release_platform: str = "Linux"
    gerrit_enabled: bool = True
    gerrit_base_url: str = "https://chromium-review.googlesource.com"
    gerrit_lookback_hours: int = 24
    gerrit_overlap_minutes: int = 15
    gerrit_page_size: int = 200
    gerrit_max_changes: int = 5000
    gerrit_open_changes_enabled: bool = True
    gerrit_patch_details_enabled: bool = True
    gerrit_max_patch_bytes: int = 2_000_000
    gerrit_max_detailed_changes: int = 20
    gerrit_auto_path_min_score: int = 80
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    candidate_ignore_patterns: tuple[str, ...] = DEFAULT_IGNORE_PATTERNS
    target_rules: dict[str, TargetRule] = field(default_factory=dict)


def _expand_path(value: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve()


def _as_tuple(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return fallback
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected an array of strings")
    return tuple(value)


def _environment_name(value: Any, *, setting: str, fallback: str) -> str:
    name = str(value if value is not None else fallback)
    if not name or not name.replace("_", "A").isalnum() or name[0].isdigit():
        raise ValueError(f"{setting} must be an environment variable name")
    return name


def load_config(path: str | Path) -> TrackerConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    base = config_path.parent
    tracker = raw.get("tracker", {})
    http = raw.get("http", {})
    chromestatus = raw.get("chromestatus", {})
    chromium = raw.get("chromium", {})
    gerrit = raw.get("gerrit", {})
    discord = raw.get("discord", {})
    health = raw.get("health", {})
    candidates = raw.get("candidates", {})

    min_severity = str(discord.get("min_severity", "medium")).casefold()
    if min_severity not in {"low", "medium", "high"}:
        raise ValueError("discord.min_severity must be low, medium, or high")
    transport = str(discord.get("transport", "webhook")).casefold()
    if transport not in {"webhook", "bot"}:
        raise ValueError("discord.transport must be webhook or bot")
    webhook_env = _environment_name(
        discord.get("webhook_env"),
        setting="discord.webhook_env",
        fallback="OT_TRACKER_DISCORD_WEBHOOK_URL",
    )
    bot_token_env = _environment_name(
        discord.get("bot_token_env"),
        setting="discord.bot_token_env",
        fallback="DISCORD_BOT_TOKEN",
    )
    channel_id_env = _environment_name(
        discord.get("channel_id_env"),
        setting="discord.channel_id_env",
        fallback="DISCORD_CHANNEL_ID",
    )
    bot_token_file = (
        _expand_path(str(discord["bot_token_file"]), base)
        if discord.get("bot_token_file")
        else None
    )
    channel_id = str(discord.get("channel_id") or "").strip() or None
    if channel_id is not None and (not channel_id.isdigit() or len(channel_id) > 25):
        raise ValueError("discord.channel_id must be a Discord snowflake ID")

    target_rules: dict[str, TargetRule] = {}
    for name, rule in raw.get("targets", {}).items():
        if not isinstance(rule, dict):
            raise ValueError(f"targets.{name} must be a table")
        target_rules[name] = TargetRule(
            name=name,
            aliases=_as_tuple(rule.get("aliases"), ()),
            paths=_as_tuple(rule.get("paths"), ()),
        )

    return TrackerConfig(
        config_path=config_path,
        database_path=_expand_path(
            str(tracker.get("database", "var/ot-tracker.sqlite3")), base
        ),
        reports_dir=_expand_path(str(tracker.get("reports_dir", "reports")), base),
        recent_event_days=int(tracker.get("recent_event_days", 14)),
        http_timeout_seconds=float(http.get("timeout_seconds", 30)),
        http_retries=int(http.get("retries", 3)),
        user_agent=str(http.get("user_agent", "chrome-ot-tracker/0.1")),
        chromestatus_base_url=str(
            chromestatus.get("base_url", "https://chromestatus.com/api/v0")
        ).rstrip("/"),
        feature_fetch_workers=max(1, int(chromestatus.get("feature_fetch_workers", 8))),
        gitiles_base_url=str(
            chromium.get(
                "gitiles_base_url",
                "https://chromium.googlesource.com/chromium/src",
            )
        ).rstrip("/"),
        chromium_ref=str(chromium.get("ref", "refs/heads/main")),
        chromium_source_files=_as_tuple(
            chromium.get("source_files"), DEFAULT_SOURCE_FILES
        ),
        chromiumdash_base_url=str(
            chromium.get(
                "chromiumdash_base_url",
                "https://chromiumdash.appspot.com",
            )
        ).rstrip("/"),
        chromium_release_channels=_as_tuple(
            chromium.get("release_channels"), ("Stable", "Beta")
        ),
        chromium_release_platform=str(
            chromium.get("release_platform", "Linux")
        ),
        gerrit_enabled=bool(gerrit.get("enabled", True)),
        gerrit_base_url=str(
            gerrit.get("base_url", "https://chromium-review.googlesource.com")
        ).rstrip("/"),
        gerrit_lookback_hours=max(1, int(gerrit.get("lookback_hours", 24))),
        gerrit_overlap_minutes=max(0, int(gerrit.get("overlap_minutes", 15))),
        gerrit_page_size=min(500, max(1, int(gerrit.get("page_size", 200)))),
        gerrit_max_changes=max(1, int(gerrit.get("max_changes", 5000))),
        gerrit_open_changes_enabled=bool(gerrit.get("open_changes_enabled", True)),
        gerrit_patch_details_enabled=bool(gerrit.get("patch_details_enabled", True)),
        gerrit_max_patch_bytes=max(
            100_000, int(gerrit.get("max_patch_bytes", 2_000_000))
        ),
        gerrit_max_detailed_changes=max(
            0, int(gerrit.get("max_detailed_changes", 20))
        ),
        gerrit_auto_path_min_score=min(
            100, max(50, int(gerrit.get("auto_path_min_score", 80)))
        ),
        discord=DiscordConfig(
            enabled=bool(discord.get("enabled", True)),
            transport=transport,
            webhook_env=webhook_env,
            bot_token_env=bot_token_env,
            bot_token_file=bot_token_file,
            channel_id_env=channel_id_env,
            channel_id=channel_id,
            username=str(discord.get("username", "Chrome OT Tracker"))[:80],
            avatar_url=(
                str(discord["avatar_url"]) if discord.get("avatar_url") else None
            ),
            min_severity=min_severity,
            implementation_digest_hours=min(
                24, max(0, int(discord.get("implementation_digest_hours", 6)))
            ),
            batch_size=min(8, max(1, int(discord.get("batch_size", 8)))),
            max_batches_per_run=max(
                1, int(discord.get("max_batches_per_run", 10))
            ),
            retries=max(1, int(discord.get("retries", 3))),
        ),
        health=HealthConfig(
            heartbeat_enabled=bool(health.get("heartbeat_enabled", True)),
            heartbeat_interval_hours=max(
                1, int(health.get("heartbeat_interval_hours", 24))
            ),
        ),
        candidate_ignore_patterns=_as_tuple(
            candidates.get("ignore_trial_names"), DEFAULT_IGNORE_PATTERNS
        ),
        target_rules=target_rules,
    )
