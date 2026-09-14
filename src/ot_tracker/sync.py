from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .chromestatus import ChromeStatusClient, ChromeStatusSnapshot
from .chromium import (
    MANUAL_COMPLETION_PATH,
    NAVIGATION_FEATURES_PATH,
    PERSISTENT_TRIALS_PATH,
    RUNTIME_FEATURES_PATH,
    ChromiumSnapshot,
    ChromiumReleaseClient,
    ChromiumReleaseCollection,
    ChromiumSourceClient,
    GerritClient,
    build_target_index,
    canonical_source_file,
    gerrit_ot_signal,
    infer_implementation_path_candidates,
    match_change_to_targets,
    matches_ignore_pattern,
    summarize_change_file_stats,
)
from .config import TrackerConfig
from .contracts import cross_source_contract_observations
from .db import EntityChange, TrackerDB, utc_now
from .diffing import (
    deep_diff,
    is_contract_diff,
    is_ot_code_diff,
    partition_diffs,
    serialize_diffs,
)
from .http import HttpClient


@dataclass(frozen=True)
class SyncResult:
    run_id: int
    status: str
    stats: dict[str, Any]
    events_created: int


_LOCATION_ONLY_KEYS = {"source_line", "source_excerpt"}
_CHROMESTATUS_AUDIT_ONLY_KEYS = {"source_updated"}
_PREMERGE_EVENT_CATEGORIES = {
    "chromium.premerge_change_detected",
    "chromium.premerge_patchset_updated",
    "chromium.premerge_framework_change_detected",
    "chromium.premerge_change_merged",
    "chromium.premerge_change_abandoned",
    "candidate.premerge_code_signal_detected",
    "candidate.premerge_patchset_updated",
}
_DECLARATION_SOURCE_PATHS = {
    RUNTIME_FEATURES_PATH,
    MANUAL_COMPLETION_PATH,
    NAVIGATION_FEATURES_PATH,
    PERSISTENT_TRIALS_PATH,
}


def _trial_event_evidence(
    trial: dict[str, Any], *, chromestatus_base_url: str
) -> dict[str, Any]:
    feature_id = trial.get("chromestatus_feature_id")
    url = trial.get("chromestatus_url")
    if not url and feature_id:
        url = f"https://chromestatus.com/feature/{feature_id}"
    if not url:
        url = f"{chromestatus_base_url.rstrip('/')}/origintrials"
    return {
        "display_name": trial.get("display_name"),
        "trial_name": trial.get("trial_name"),
        "chromestatus_url": url,
    }


def _feature_event_evidence(
    feature_id: str, feature: dict[str, Any]
) -> dict[str, Any]:
    return {
        "feature_name": feature.get("name"),
        "url": f"https://chromestatus.com/feature/{feature_id}",
    }


def _without_location_noise(value: Any) -> Any:
    """Remove parser locations that move when unrelated Chromium lines change."""
    if isinstance(value, dict):
        return {
            key: _without_location_noise(item)
            for key, item in value.items()
            if key not in _LOCATION_ONLY_KEYS
        }
    if isinstance(value, list):
        return [_without_location_noise(item) for item in value]
    return value


def _without_chromestatus_audit_noise(value: Any) -> Any:
    """Remove Chrome Status audit timestamps that do not change OT semantics."""
    if isinstance(value, dict):
        return {
            key: _without_chromestatus_audit_noise(item)
            for key, item in value.items()
            if key not in _CHROMESTATUS_AUDIT_ONLY_KEYS
        }
    if isinstance(value, list):
        return [_without_chromestatus_audit_noise(item) for item in value]
    return value


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _compact_diffs(differences: list[Any]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for diff in differences:
        item = diff.as_dict()
        if item["path"].rsplit("/", 1)[-1] in _LOCATION_ONLY_KEYS:
            continue
        for side in ("old", "new"):
            item[side] = _without_location_noise(item[side])
        compacted.append(item)
    return compacted


def _trial_lookup_from_db(db: TrackerDB) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_trials = [
        row["payload"]
        for row in db.list_entities(
            source="chromestatus", kind="origin_trial", active_only=True
        )
    ]
    active = [trial for trial in all_trials if trial.get("status") == "ACTIVE"]
    return all_trials, active


def _declarations_from_db(db: TrackerDB) -> list[dict[str, Any]]:
    return [
        row["payload"]
        for row in db.list_entities(
            source="chromium", kind="runtime_declaration", active_only=True
        )
    ]


def _candidate_score(
    declarations: list[dict[str, Any]], *, newly_added: bool, baseline_existing: bool
) -> tuple[int, list[str]]:
    score = 95 if newly_added else 65
    reasons = [
        "new runtime origin_trial_feature_name declaration"
        if newly_added
        else "runtime origin_trial_feature_name exists without public console record"
    ]
    if baseline_existing:
        score -= 10
        reasons.append("declaration predates tracker baseline; introduction time is unknown")
    statuses = {str(item.get("fields", {}).get("status") or "") for item in declarations}
    if statuses and statuses <= {"stable", ""}:
        score -= 15
        reasons.append("stable/unspecified runtime status lowers pre-registration confidence")
    if any(item.get("classifications") for item in declarations):
        score += 5
        reasons.append("special OT lifecycle classification is present")
    return max(0, min(100, score)), reasons


def _gerrit_comparison_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Ignore comment-only Gerrit activity while preserving patchset changes."""
    comparable = _without_location_noise(payload)
    change = comparable.get("change") if isinstance(comparable, dict) else None
    if isinstance(change, dict):
        change.pop("updated", None)
        # Patch summaries are presentation-only and can move between the cheap
        # CURRENT_FILES fallback and a full unified diff as request budgets vary.
        # The Gerrit revision, patchset, commit message, and files already carry
        # the durable code-change identity.
        change.pop("diff_summary", None)
    return comparable


def _premerge_match_strength(
    trial_name: str,
    match: dict[str, list[str]],
    change: dict[str, Any],
) -> str:
    """Separate direct OT evidence from a curated-path-only Gerrit match."""
    if match.get("aliases"):
        return "direct"
    summary = change.get("diff_summary")
    declared_names = (
        summary.get("added_origin_trial_feature_names") or []
        if isinstance(summary, dict)
        else []
    )
    if any(str(name).casefold() == trial_name.casefold() for name in declared_names):
        return "direct"
    return "path_only"


def _gerrit_signal_is_direct(signal: dict[str, Any] | None) -> bool:
    return bool(
        signal
        and (signal.get("matched_terms") or signal.get("declared_trial_names"))
    )


def _implementation_candidate_key(trial_name: str, path: str) -> str:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return f"{trial_name}:{digest}"


def _apply_persisted_implementation_paths(
    db: TrackerDB,
    targets: dict[str, dict[str, list[str]]],
    *,
    minimum_score: int,
) -> None:
    for row in db.list_entities(
        source="tracker", kind="implementation_path_candidate", active_only=True
    ):
        payload = row["payload"]
        trial_name = str(payload.get("trial_name") or "")
        path = str(payload.get("path") or "")
        score = int(payload.get("approved_score") or payload.get("score") or 0)
        if (
            trial_name not in targets
            or not path
            or score < minimum_score
            or not payload.get("auto_applied")
        ):
            continue
        targets[trial_name]["paths"] = sorted(
            {*targets[trial_name]["paths"], path}
        )


def _declaration_snapshot_health(
    snapshot: ChromiumSnapshot,
    *,
    previous_count: int,
) -> tuple[bool, str | None]:
    """Reject incomplete derived snapshots before reconciling stored declarations."""
    failed_sources = sorted(_DECLARATION_SOURCE_PATHS & set(snapshot.file_errors))
    if failed_sources:
        return False, None
    if RUNTIME_FEATURES_PATH not in snapshot.files:
        return False, "runtime declaration source file is missing from the snapshot"

    current_count = len(snapshot.declarations)
    runtime_source = snapshot.files[RUNTIME_FEATURES_PATH]
    if "origin_trial_feature_name" in runtime_source and current_count == 0:
        return False, "runtime declaration parser returned no declarations"
    if previous_count >= 20 and current_count * 2 < previous_count:
        return (
            False,
            "runtime declaration count collapsed "
            f"from {previous_count} to {current_count}",
        )
    return True, None


def _chromestatus_snapshot_health(
    snapshot: ChromeStatusSnapshot,
    *,
    previous_count: int,
    previous_active_count: int,
) -> tuple[bool, str | None]:
    """Reject malformed or implausibly truncated Chrome Status inventories."""
    current_count = len(snapshot.trials)
    if current_count == 0:
        return False, "origin trial feed returned no records"

    trial_ids = [str(trial.get("id") or "") for trial in snapshot.trials]
    if any(not trial_id or trial_id == "None" for trial_id in trial_ids):
        return False, "origin trial feed contains a record without a valid id"
    if len(set(trial_ids)) != current_count:
        return False, "origin trial feed contains duplicate ids"

    expected_active_ids = {
        str(trial["id"])
        for trial in snapshot.trials
        if trial.get("status") == "ACTIVE"
    }
    active_ids = [str(trial.get("id") or "") for trial in snapshot.active_trials]
    if len(set(active_ids)) != len(active_ids):
        return False, "origin trial active list contains duplicate ids"
    if set(active_ids) != expected_active_ids:
        return False, "origin trial active list does not match trial statuses"

    if previous_count >= 20:
        allowed_drop = max(10, previous_count // 10)
        if current_count < previous_count - allowed_drop:
            return (
                False,
                "origin trial count dropped implausibly "
                f"from {previous_count} to {current_count}",
            )
    current_active_count = len(active_ids)
    if previous_active_count >= 10 and current_active_count * 2 < previous_active_count:
        return (
            False,
            "active origin trial count dropped implausibly "
            f"from {previous_active_count} to {current_active_count}",
        )
    return True, None


def run_sync(
    config: TrackerConfig,
    *,
    emit_baseline: bool = False,
    gerrit_enabled: bool | None = None,
) -> SyncResult:
    http = HttpClient(
        timeout_seconds=config.http_timeout_seconds,
        retries=config.http_retries,
        user_agent=config.user_agent,
    )
    use_gerrit = config.gerrit_enabled if gerrit_enabled is None else gerrit_enabled
    collection_started = datetime.now(UTC)

    with TrackerDB(config.database_path) as db:
        cs_initialized = db.get_meta("source.chromestatus.initialized") == "1"
        chromium_initialized = db.get_meta("source.chromium.initialized") == "1"
        gerrit_initialized = db.get_meta("source.gerrit.initialized") == "1"
        gerrit_open_initialized = db.get_meta("source.gerrit.open_initialized") == "1"
        contracts_initialized = db.get_meta("source.contracts.initialized") == "1"
        run_id = db.begin_run(
            baseline=not cs_initialized and not chromium_initialized and not gerrit_initialized
        )

        if db.get_meta("normalization.chromium_location_noise.v1") != "1":
            db.rehash_entities(
                source="chromium",
                kind="runtime_declaration",
                transform=_without_location_noise,
            )
            db.rehash_entities(
                source="chromium",
                kind="pre_registration_candidate",
                transform=_without_location_noise,
            )
            db.set_meta("normalization.chromium_location_noise.v1", "1")
            db.commit_changes()

        if db.get_meta("normalization.chromestatus_audit_noise.v1") != "1":
            db.rehash_entities(
                source="chromestatus",
                kind="feature",
                transform=_without_chromestatus_audit_noise,
            )
            db.set_meta("normalization.chromestatus_audit_noise.v1", "1")
            db.commit_changes()

        if db.get_meta("policy.premerge_events_low.v1") != "1":
            db.set_event_severity(
                categories=_PREMERGE_EVENT_CATEGORIES,
                severity="low",
            )
            db.set_meta("policy.premerge_events_low.v1", "1")
            db.commit_changes()

        errors: list[dict[str, str]] = []
        health_issues: dict[str, dict[str, Any]] = {}
        health_checked_scopes = {"chromestatus", "chromium"}
        health_recovery_scopes: set[str] = set()
        chrome_snapshot: ChromeStatusSnapshot | None = None
        chromium_snapshot: ChromiumSnapshot | None = None
        chromium_releases = ChromiumReleaseCollection(snapshots={}, errors={})
        gerrit_changes: list[dict[str, Any]] | None = None
        gerrit_client: GerritClient | None = None

        def health_issue(
            key: str,
            *,
            scope: str,
            source_name: str,
            error: str,
            url: str | None = None,
            **details: Any,
        ) -> None:
            health_issues[key] = {
                "scope": scope,
                "source_name": source_name,
                "error": error,
                "url": url,
                **details,
            }

        try:
            chrome_snapshot = ChromeStatusClient(config, http).fetch_snapshot()
        except Exception as exc:
            errors.append({"source": "chromestatus", "error": str(exc)})
            health_issue(
                "chromestatus.fetch",
                scope="chromestatus",
                source_name="Chrome Status",
                error=str(exc),
                url=f"{config.chromestatus_base_url.rstrip('/')}/origintrials",
            )

        chromium_client = ChromiumSourceClient(config, http)
        try:
            chromium_snapshot = chromium_client.fetch_snapshot()
        except Exception as exc:
            errors.append({"source": "chromium", "error": str(exc)})
            health_issue(
                "chromium.fetch",
                scope="chromium",
                source_name="Chromium main",
                error=str(exc),
                url=config.gitiles_base_url,
            )

        release_channels = tuple(
            dict.fromkeys(
                channel.strip().title()
                for channel in config.chromium_release_channels
                if channel.strip()
            )
        )
        health_checked_scopes.update(
            f"chromium.release.{channel}" for channel in release_channels
        )
        if release_channels:
            chromium_releases = ChromiumReleaseClient(
                config,
                http,
                source_client=chromium_client,
            ).fetch_snapshots()
            for channel, error in sorted(chromium_releases.errors.items()):
                source = f"chromium.release.{channel}"
                errors.append({"source": source, "error": error})
                health_issue(
                    source,
                    scope=source,
                    source_name=f"Chromium {channel} release",
                    error=error,
                    url=f"{config.chromiumdash_base_url}/releases",
                    channel=channel,
                    platform=config.chromium_release_platform,
                )

        if use_gerrit:
            health_checked_scopes.add("gerrit")
            watermark = _parse_timestamp(db.get_meta("source.gerrit.watermark"))
            if watermark is None:
                after = collection_started - timedelta(hours=config.gerrit_lookback_hours)
            else:
                after = watermark - timedelta(minutes=config.gerrit_overlap_minutes)
            try:
                gerrit_client = GerritClient(config, http)
                gerrit_changes = gerrit_client.fetch_changes(after)
            except Exception as exc:
                errors.append({"source": "gerrit", "error": str(exc)})
                health_issue(
                    "gerrit.fetch",
                    scope="gerrit",
                    source_name="Chromium Gerrit",
                    error=str(exc),
                    url=config.gerrit_base_url,
                )

        if chrome_snapshot and chrome_snapshot.feature_errors:
            errors.extend(
                {
                    "source": f"chromestatus.feature.{feature_id}",
                    "error": error,
                }
                for feature_id, error in sorted(chrome_snapshot.feature_errors.items())
            )
            health_issue(
                "chromestatus.features",
                scope="chromestatus",
                source_name="Chrome Status feature details",
                error=(
                    f"{len(chrome_snapshot.feature_errors)} feature detail request(s) failed"
                ),
                url="https://chromestatus.com/features",
                failed_count=len(chrome_snapshot.feature_errors),
                examples=sorted(chrome_snapshot.feature_errors)[:5],
            )
        if chromium_snapshot and chromium_snapshot.file_errors:
            errors.extend(
                {"source": f"chromium.file.{path}", "error": error}
                for path, error in sorted(chromium_snapshot.file_errors.items())
            )
            health_issue(
                "chromium.files",
                scope="chromium",
                source_name="Chromium OT source files",
                error=(
                    f"{len(chromium_snapshot.file_errors)} source file request(s) failed"
                ),
                url=config.gitiles_base_url,
                failed_count=len(chromium_snapshot.file_errors),
                examples=sorted(chromium_snapshot.file_errors)[:5],
            )

        stats: dict[str, Any] = {
            "collection_started": collection_started.isoformat(timespec="seconds"),
            "errors": errors,
            "sources": {},
        }
        events_created = 0

        def event(**kwargs: Any) -> None:
            nonlocal events_created
            if db.record_event(run_id, **kwargs):
                events_created += 1

        try:
            db.begin_changes()

            previous_chrome_trials, previous_active_trials = _trial_lookup_from_db(db)
            previous_feature_count = len(
                db.list_entities(
                    source="chromestatus", kind="feature", active_only=True
                )
            )
            usable_chrome_snapshot: ChromeStatusSnapshot | None = None
            if chrome_snapshot is not None:
                chrome_snapshot_complete, chrome_snapshot_error = (
                    _chromestatus_snapshot_health(
                        chrome_snapshot,
                        previous_count=len(previous_chrome_trials),
                        previous_active_count=len(previous_active_trials),
                    )
                )
                if chrome_snapshot_complete:
                    usable_chrome_snapshot = chrome_snapshot
                else:
                    health_error = (
                        chrome_snapshot_error
                        or "origin trial inventory failed validation"
                    )
                    errors.append(
                        {
                            "source": "chromestatus.inventory",
                            "error": health_error,
                        }
                    )
                    health_issue(
                        "chromestatus.inventory",
                        scope="chromestatus",
                        source_name="Chrome Status OT inventory",
                        error=health_error,
                        url=f"{config.chromestatus_base_url.rstrip('/')}/origintrials",
                        preserved_total=len(previous_chrome_trials),
                        preserved_active=len(previous_active_trials),
                        fetched_total=len(chrome_snapshot.trials),
                        fetched_active=len(chrome_snapshot.active_trials),
                    )
                    stats["sources"]["chromestatus"] = {
                        "total_trials": len(previous_chrome_trials),
                        "active_trials": len(previous_active_trials),
                        "active_features": previous_feature_count,
                        "fetched_total_trials": len(chrome_snapshot.trials),
                        "fetched_active_trials": len(chrome_snapshot.active_trials),
                        "fetched_active_features": len(chrome_snapshot.features),
                        "feature_errors": len(chrome_snapshot.feature_errors),
                        "trial_snapshot_complete": False,
                    }

            if usable_chrome_snapshot is not None:
                if not usable_chrome_snapshot.feature_errors:
                    health_recovery_scopes.add("chromestatus")
                emit = emit_baseline or cs_initialized
                present_trial_keys: set[str] = set()
                for trial in usable_chrome_snapshot.trials:
                    key = str(trial["id"])
                    present_trial_keys.add(key)
                    trial_evidence = _trial_event_evidence(
                        trial,
                        chromestatus_base_url=config.chromestatus_base_url,
                    )
                    change = db.upsert_entity(
                        run_id,
                        source="chromestatus",
                        kind="origin_trial",
                        entity_key=key,
                        payload=trial,
                    )
                    if change.created and emit:
                        event(
                            category="official_ot.registered",
                            severity="high",
                            source="chromestatus",
                            entity_kind="origin_trial",
                            entity_key=key,
                            new=trial,
                            evidence=trial_evidence,
                        )
                    elif change.reactivated and emit:
                        event(
                            category="official_ot.reappeared_in_feed",
                            severity="high",
                            source="chromestatus",
                            entity_kind="origin_trial",
                            entity_key=key,
                            new=trial,
                            evidence=trial_evidence,
                        )
                    elif change.changed and not change.created and emit:
                        differences = deep_diff(change.previous, change.current)
                        code_diffs = [diff for diff in differences if is_ot_code_diff(diff)]
                        differences = [
                            diff for diff in differences if not is_ot_code_diff(diff)
                        ]
                        milestone_diffs, status_diffs, metadata_diffs = partition_diffs(
                            differences
                        )
                        if code_diffs:
                            event(
                                category="official_ot.code_changed",
                                severity="high",
                                source="chromestatus",
                                entity_kind="origin_trial",
                                entity_key=key,
                                evidence={
                                    **trial_evidence,
                                    "changes": serialize_diffs(code_diffs),
                                },
                            )
                        if milestone_diffs:
                            event(
                                category="official_ot.milestone_changed",
                                severity="high",
                                source="chromestatus",
                                entity_kind="origin_trial",
                                entity_key=key,
                                old=None,
                                new=None,
                                evidence={
                                    **trial_evidence,
                                    "changes": serialize_diffs(milestone_diffs),
                                },
                            )
                        if status_diffs:
                            event(
                                category="official_ot.status_changed",
                                severity="high",
                                source="chromestatus",
                                entity_kind="origin_trial",
                                entity_key=key,
                                evidence={
                                    **trial_evidence,
                                    "changes": serialize_diffs(status_diffs),
                                },
                            )
                        if metadata_diffs:
                            event(
                                category="official_ot.metadata_changed",
                                severity="medium",
                                source="chromestatus",
                                entity_kind="origin_trial",
                                entity_key=key,
                                evidence={
                                    **trial_evidence,
                                    "changes": serialize_diffs(metadata_diffs),
                                },
                            )

                missing_trials = db.mark_missing(
                    source="chromestatus",
                    kind="origin_trial",
                    present_keys=present_trial_keys,
                )
                if emit:
                    for missing in missing_trials:
                        missing_evidence = _trial_event_evidence(
                            missing["payload"],
                            chromestatus_base_url=config.chromestatus_base_url,
                        )
                        event(
                            category="official_ot.removed_from_feed",
                            severity="high",
                            source="chromestatus",
                            entity_kind="origin_trial",
                            entity_key=missing["entity_key"],
                            old=missing["payload"],
                            evidence={
                                **missing_evidence,
                                "reason": "record absent from a successful full feed",
                            },
                        )

                present_feature_keys = set(usable_chrome_snapshot.features)
                # A failed detail request must not make the existing feature look removed.
                present_feature_keys.update(usable_chrome_snapshot.feature_errors)
                for feature_id, feature in sorted(
                    usable_chrome_snapshot.features.items()
                ):
                    feature_evidence = _feature_event_evidence(feature_id, feature)
                    change = db.upsert_entity(
                        run_id,
                        source="chromestatus",
                        kind="feature",
                        entity_key=feature_id,
                        payload=feature,
                    )
                    if change.created and emit:
                        event(
                            category="chromestatus.feature_tracking_started",
                            severity="medium",
                            source="chromestatus",
                            entity_kind="feature",
                            entity_key=feature_id,
                            new=feature,
                            evidence=feature_evidence,
                        )
                    elif change.reactivated and emit:
                        event(
                            category="chromestatus.feature_tracking_resumed",
                            severity="medium",
                            source="chromestatus",
                            entity_kind="feature",
                            entity_key=feature_id,
                            new=feature,
                            evidence=feature_evidence,
                        )
                    elif change.changed and not change.created and emit:
                        differences = [
                            diff
                            for diff in deep_diff(change.previous, change.current)
                            if not any(
                                part in _CHROMESTATUS_AUDIT_ONLY_KEYS
                                for part in diff.path.split("/")
                            )
                        ]
                        code_diffs = [diff for diff in differences if is_ot_code_diff(diff)]
                        differences = [
                            diff for diff in differences if not is_ot_code_diff(diff)
                        ]
                        milestone_diffs, status_diffs, metadata_diffs = partition_diffs(
                            differences
                        )
                        if code_diffs:
                            event(
                                category="chromestatus.ot_code_changed",
                                severity="high",
                                source="chromestatus",
                                entity_kind="feature",
                                entity_key=feature_id,
                                evidence={
                                    **feature_evidence,
                                    "changes": serialize_diffs(code_diffs),
                                },
                            )
                        if milestone_diffs:
                            event(
                                category="chromestatus.ot_stage_milestone_changed",
                                severity="high",
                                source="chromestatus",
                                entity_kind="feature",
                                entity_key=feature_id,
                                evidence={
                                    **feature_evidence,
                                    "changes": serialize_diffs(milestone_diffs),
                                },
                            )
                        if status_diffs:
                            metadata_diffs.extend(status_diffs)
                        if metadata_diffs:
                            event(
                                category="chromestatus.feature_metadata_changed",
                                severity="medium",
                                source="chromestatus",
                                entity_kind="feature",
                                entity_key=feature_id,
                                evidence={
                                    **feature_evidence,
                                    "changes": serialize_diffs(metadata_diffs),
                                },
                            )

                db.mark_missing(
                    source="chromestatus",
                    kind="feature",
                    present_keys=present_feature_keys,
                )
                db.set_meta("source.chromestatus.initialized", "1")
                stats["sources"]["chromestatus"] = {
                    "total_trials": len(usable_chrome_snapshot.trials),
                    "active_trials": len(usable_chrome_snapshot.active_trials),
                    "active_features": len(usable_chrome_snapshot.features),
                    "fetched_total_trials": len(usable_chrome_snapshot.trials),
                    "fetched_active_trials": len(
                        usable_chrome_snapshot.active_trials
                    ),
                    "fetched_active_features": len(
                        usable_chrome_snapshot.features
                    ),
                    "feature_errors": len(usable_chrome_snapshot.feature_errors),
                    "trial_snapshot_complete": True,
                }

            declaration_changes: dict[str, EntityChange] = {}
            usable_chromium_declarations: list[dict[str, Any]] | None = None
            declarations_complete = False
            if chromium_snapshot is not None:
                emit = emit_baseline or chromium_initialized
                present_files = set(chromium_snapshot.files) | set(
                    chromium_snapshot.file_errors
                )
                for path, content in sorted(chromium_snapshot.files.items()):
                    payload = canonical_source_file(
                        path, content, chromium_snapshot.revision
                    )
                    change = db.upsert_entity(
                        run_id,
                        source="chromium",
                        kind="source_file",
                        entity_key=path,
                        payload=payload,
                    )
                    if change.changed and not change.created and emit:
                        event(
                            category="chromium.ot_source_file_changed",
                            severity="low" if path == RUNTIME_FEATURES_PATH else "high",
                            source="chromium",
                            entity_kind="source_file",
                            entity_key=path,
                            old=change.previous,
                            new=change.current,
                            evidence={"revision": chromium_snapshot.revision},
                        )
                db.mark_missing(
                    source="chromium", kind="source_file", present_keys=present_files
                )

                previous_declarations = _declarations_from_db(db)
                declarations_complete, declaration_error = _declaration_snapshot_health(
                    chromium_snapshot,
                    previous_count=len(previous_declarations),
                )
                if declaration_error is not None:
                    errors.append(
                        {
                            "source": "chromium.declarations",
                            "error": declaration_error,
                        }
                    )
                    health_issue(
                        "chromium.declarations",
                        scope="chromium",
                        source_name="Chromium main OT declarations",
                        error=declaration_error,
                        url=config.gitiles_base_url,
                        preserved_count=len(previous_declarations),
                        fetched_count=len(chromium_snapshot.declarations),
                        revision=chromium_snapshot.revision,
                    )
                usable_chromium_declarations = (
                    chromium_snapshot.declarations
                    if declarations_complete
                    else previous_declarations
                )

                present_declarations: set[str] = set()
                official_trials, active_trials = (
                    (
                        usable_chrome_snapshot.trials,
                        usable_chrome_snapshot.active_trials,
                    )
                    if usable_chrome_snapshot is not None
                    else _trial_lookup_from_db(db)
                )
                active_names = {
                    str(trial["trial_name"]).casefold()
                    for trial in active_trials
                    if trial.get("trial_name")
                }
                official_names = {
                    str(trial["trial_name"]).casefold()
                    for trial in official_trials
                    if trial.get("trial_name")
                }
                for declaration in usable_chromium_declarations:
                    key = str(declaration["runtime_feature_name"])
                    present_declarations.add(key)
                    change = db.upsert_entity(
                        run_id,
                        source="chromium",
                        kind="runtime_declaration",
                        entity_key=key,
                        payload=declaration,
                        comparison_payload=_without_location_noise(declaration),
                    )
                    declaration_changes[key] = change
                    official_active = str(declaration.get("trial_name", "")).casefold() in active_names
                    if change.created and emit:
                        event(
                            category="chromium.ot_declaration_added",
                            severity="high" if official_active else "medium",
                            source="chromium",
                            entity_kind="runtime_declaration",
                            entity_key=key,
                            new=declaration,
                            evidence={
                                "revision": chromium_snapshot.revision,
                                "trial_name": declaration.get("trial_name"),
                                "official_active": official_active,
                            },
                        )
                    elif change.reactivated and emit:
                        event(
                            category="chromium.ot_declaration_restored",
                            severity="high",
                            source="chromium",
                            entity_kind="runtime_declaration",
                            entity_key=key,
                            new=declaration,
                            evidence={
                                "revision": chromium_snapshot.revision,
                                "trial_name": declaration.get("trial_name"),
                            },
                        )
                    elif change.changed and not change.created and emit:
                        differences = deep_diff(change.previous, change.current)
                        contract_change = any(is_contract_diff(diff) for diff in differences)
                        event(
                            category="chromium.ot_code_changed",
                            severity="high" if contract_change else "medium",
                            source="chromium",
                            entity_kind="runtime_declaration",
                            entity_key=key,
                            evidence={
                                "revision": chromium_snapshot.revision,
                                "trial_name": declaration.get("trial_name"),
                                "contract_change": contract_change,
                                "changes": _compact_diffs(differences),
                            },
                        )

                removed = db.mark_missing(
                    source="chromium",
                    kind="runtime_declaration",
                    present_keys=present_declarations,
                )
                if emit:
                    for missing in removed:
                        event(
                            category="chromium.ot_declaration_removed",
                            severity="high",
                            source="chromium",
                            entity_kind="runtime_declaration",
                            entity_key=missing["entity_key"],
                            old=missing["payload"],
                            evidence={"revision": chromium_snapshot.revision},
                        )

                grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for declaration in usable_chromium_declarations:
                    grouped[str(declaration["trial_name"])].append(declaration)
                existing_declaration_candidates = {
                    row["entity_key"]: row["payload"]
                    for row in db.list_entities(
                        source="chromium",
                        kind="pre_registration_candidate",
                        active_only=True,
                        key_prefix="declaration:",
                    )
                }
                present_candidates: set[str] = set()
                present_rejections: set[str] = set()
                for trial_name, declarations in sorted(grouped.items()):
                    if trial_name.casefold() in official_names:
                        continue
                    ignored_by = matches_ignore_pattern(
                        trial_name, config.candidate_ignore_patterns
                    )
                    if ignored_by:
                        key = f"declaration:{trial_name}"
                        present_rejections.add(key)
                        db.upsert_entity(
                            run_id,
                            source="chromium",
                            kind="candidate_rejection",
                            entity_key=key,
                            payload={
                                "trial_name": trial_name,
                                "reason": "matched configured test/scaffold ignore pattern",
                                "pattern": ignored_by,
                                "runtime_features": sorted(
                                    item["runtime_feature_name"] for item in declarations
                                ),
                            },
                        )
                        continue
                    newly_added = any(
                        declaration_changes[item["runtime_feature_name"]].created
                        for item in declarations
                    ) and chromium_initialized
                    key = f"declaration:{trial_name}"
                    previous_candidate = existing_declaration_candidates.get(key)
                    baseline_existing = (
                        bool(previous_candidate.get("baseline_existing", True))
                        if previous_candidate
                        else not chromium_initialized
                    )
                    score, reasons = _candidate_score(
                        declarations,
                        newly_added=newly_added,
                        baseline_existing=baseline_existing,
                    )
                    present_candidates.add(key)
                    payload = {
                        "candidate_key": key,
                        "signal_type": "runtime_declaration_without_console_record",
                        "trial_name": trial_name,
                        "score": score,
                        "reasons": reasons,
                        "baseline_existing": baseline_existing,
                        "runtime_features": sorted(
                            item["runtime_feature_name"] for item in declarations
                        ),
                        "declarations": [
                            {
                                "runtime_feature_name": item["runtime_feature_name"],
                                "fields": item.get("fields"),
                                "classifications": item.get("classifications"),
                                "source_path": item.get("source_path"),
                                "source_line": item.get("source_line"),
                            }
                            for item in declarations
                        ],
                    }
                    change = db.upsert_entity(
                        run_id,
                        source="chromium",
                        kind="pre_registration_candidate",
                        entity_key=key,
                        payload=payload,
                        comparison_payload=_without_location_noise(payload),
                    )
                    if change.created and emit:
                        event(
                            category="candidate.pre_registration_detected",
                            severity="high" if score >= 80 else "medium",
                            source="chromium",
                            entity_kind="pre_registration_candidate",
                            entity_key=key,
                            new=payload,
                            evidence={"signal": payload["signal_type"]},
                        )
                    elif change.reactivated and emit:
                        event(
                            category="candidate.signal_restored",
                            severity="high" if score >= 80 else "medium",
                            source="chromium",
                            entity_kind="pre_registration_candidate",
                            entity_key=key,
                            new=payload,
                            evidence={"signal": payload["signal_type"]},
                        )
                    elif (
                        change.changed
                        and not change.created
                        and emit
                        and any(
                            declaration_changes[item["runtime_feature_name"]].changed
                            for item in declarations
                        )
                    ):
                        event(
                            category="candidate.evidence_updated",
                            severity="medium",
                            source="chromium",
                            entity_kind="pre_registration_candidate",
                            entity_key=key,
                            evidence={
                                "changes": _compact_diffs(
                                    deep_diff(change.previous, change.current)
                                )
                            },
                        )

                missing_candidates = db.mark_missing(
                    source="chromium",
                    kind="pre_registration_candidate",
                    present_keys=present_candidates,
                    key_prefix="declaration:",
                )
                if emit:
                    current_official = {
                        str(trial["trial_name"]).casefold()
                        for trial in usable_chrome_snapshot.trials
                        if trial.get("trial_name")
                    } if usable_chrome_snapshot is not None else set()
                    for missing in missing_candidates:
                        old = missing["payload"]
                        registered = str(old.get("trial_name", "")).casefold() in current_official
                        event(
                            category=(
                                "candidate.resolved_by_official_registration"
                                if registered
                                else "candidate.signal_removed"
                            ),
                            severity="high" if registered else "medium",
                            source="chromium",
                            entity_kind="pre_registration_candidate",
                            entity_key=missing["entity_key"],
                            old=old,
                            evidence={
                                "registered": registered,
                                "official_source_available": (
                                    usable_chrome_snapshot is not None
                                ),
                            },
                        )
                db.mark_missing(
                    source="chromium",
                    kind="candidate_rejection",
                    present_keys=present_rejections,
                    key_prefix="declaration:",
                )

                if declarations_complete:
                    db.set_meta("source.chromium.initialized", "1")
                db.set_meta("source.chromium.revision", chromium_snapshot.revision)
                stats["sources"]["chromium"] = {
                    "revision": chromium_snapshot.revision,
                    "source_files": len(chromium_snapshot.files),
                    "file_errors": len(chromium_snapshot.file_errors),
                    "runtime_declarations": len(usable_chromium_declarations),
                    "fetched_runtime_declarations": len(
                        chromium_snapshot.declarations
                    ),
                    "declaration_snapshot_complete": declarations_complete,
                    "pre_registration_candidates": len(present_candidates),
                    "candidate_rejections": len(present_rejections),
                }
                if declarations_complete and not chromium_snapshot.file_errors:
                    health_recovery_scopes.add("chromium")

            release_stats: dict[str, dict[str, Any]] = {}
            for channel in release_channels:
                release_snapshot = chromium_releases.snapshots.get(channel)
                previous_release_rows = db.list_entities(
                    source="chromium_release",
                    kind="runtime_declaration",
                    active_only=True,
                    key_prefix=f"{channel}:",
                )
                if release_snapshot is None:
                    release_stats[channel] = {
                        "available": False,
                        "preserved_runtime_declarations": len(previous_release_rows),
                    }
                    continue

                release_chromium = release_snapshot.chromium
                release_complete, release_error = _declaration_snapshot_health(
                    release_chromium,
                    previous_count=len(previous_release_rows),
                )
                if not release_complete:
                    release_error = (
                        release_error
                        or f"{channel} declaration snapshot failed validation"
                    )
                    source = f"chromium.release.{channel}"
                    errors.append({"source": source, "error": release_error})
                    health_issue(
                        source,
                        scope=source,
                        source_name=f"Chromium {channel} release declarations",
                        error=release_error,
                        url=f"{config.chromiumdash_base_url}/releases",
                        channel=channel,
                        platform=release_snapshot.platform,
                        milestone=release_snapshot.milestone,
                        version=release_snapshot.version,
                        revision=release_snapshot.revision,
                        preserved_count=len(previous_release_rows),
                        fetched_count=len(release_chromium.declarations),
                    )
                    release_stats[channel] = {
                        "available": True,
                        "snapshot_complete": False,
                        "milestone": release_snapshot.milestone,
                        "version": release_snapshot.version,
                        "revision": release_snapshot.revision,
                        "runtime_declarations": len(previous_release_rows),
                        "fetched_runtime_declarations": len(
                            release_chromium.declarations
                        ),
                    }
                    continue

                release_initialized = (
                    db.get_meta(f"source.chromium_release.{channel}.initialized")
                    == "1"
                )
                release_emit = emit_baseline or release_initialized
                present_release_keys: set[str] = set()
                active_names = {
                    str(trial.get("trial_name") or "").casefold()
                    for trial in (
                        usable_chrome_snapshot.active_trials
                        if usable_chrome_snapshot is not None
                        else _trial_lookup_from_db(db)[1]
                    )
                    if trial.get("trial_name")
                }
                for declaration in release_chromium.declarations:
                    runtime_feature = str(declaration["runtime_feature_name"])
                    key = f"{channel}:{runtime_feature}"
                    present_release_keys.add(key)
                    payload = {
                        "channel": channel,
                        "platform": release_snapshot.platform,
                        "milestone": release_snapshot.milestone,
                        "version": release_snapshot.version,
                        "revision": release_snapshot.revision,
                        "runtime_feature_name": runtime_feature,
                        "trial_name": declaration.get("trial_name"),
                        "declaration": declaration,
                    }
                    change = db.upsert_entity(
                        run_id,
                        source="chromium_release",
                        kind="runtime_declaration",
                        entity_key=key,
                        payload=payload,
                        comparison_payload=_without_location_noise(declaration),
                    )
                    evidence = {
                        "channel": channel,
                        "platform": release_snapshot.platform,
                        "milestone": release_snapshot.milestone,
                        "version": release_snapshot.version,
                        "revision": release_snapshot.revision,
                        "runtime_feature_name": runtime_feature,
                        "trial_name": declaration.get("trial_name"),
                        "url": f"{config.chromiumdash_base_url}/releases",
                    }
                    official_active = (
                        str(declaration.get("trial_name") or "").casefold()
                        in active_names
                    )
                    if change.created and release_emit:
                        event(
                            category="chromium.release_ot_declaration_added",
                            severity="high" if official_active else "medium",
                            source="chromium_release",
                            entity_kind="runtime_declaration",
                            entity_key=key,
                            new=payload,
                            evidence=evidence,
                        )
                    elif change.reactivated and release_emit:
                        event(
                            category="chromium.release_ot_declaration_restored",
                            severity="high",
                            source="chromium_release",
                            entity_kind="runtime_declaration",
                            entity_key=key,
                            new=payload,
                            evidence=evidence,
                        )
                    elif change.changed and not change.created and release_emit:
                        previous_declaration = (
                            change.previous.get("declaration", {})
                            if isinstance(change.previous, dict)
                            else {}
                        )
                        differences = deep_diff(previous_declaration, declaration)
                        event(
                            category="chromium.release_ot_code_changed",
                            severity=(
                                "high"
                                if any(is_contract_diff(diff) for diff in differences)
                                else "medium"
                            ),
                            source="chromium_release",
                            entity_kind="runtime_declaration",
                            entity_key=key,
                            evidence={
                                **evidence,
                                "changes": _compact_diffs(differences),
                            },
                        )

                removed_release = db.mark_missing(
                    source="chromium_release",
                    kind="runtime_declaration",
                    present_keys=present_release_keys,
                    key_prefix=f"{channel}:",
                )
                if release_emit:
                    for missing in removed_release:
                        event(
                            category="chromium.release_ot_declaration_removed",
                            severity="high",
                            source="chromium_release",
                            entity_kind="runtime_declaration",
                            entity_key=missing["entity_key"],
                            old=missing["payload"],
                            evidence={
                                "channel": channel,
                                "platform": release_snapshot.platform,
                                "milestone": release_snapshot.milestone,
                                "version": release_snapshot.version,
                                "revision": release_snapshot.revision,
                                "runtime_feature_name": (
                                    missing["payload"].get("runtime_feature_name")
                                    if isinstance(missing["payload"], dict)
                                    else None
                                ),
                                "trial_name": (
                                    missing["payload"].get("trial_name")
                                    if isinstance(missing["payload"], dict)
                                    else None
                                ),
                                "url": f"{config.chromiumdash_base_url}/releases",
                            },
                        )
                db.set_meta(f"source.chromium_release.{channel}.initialized", "1")
                health_recovery_scopes.add(f"chromium.release.{channel}")
                db.set_meta(
                    f"source.chromium_release.{channel}.revision",
                    release_snapshot.revision,
                )
                release_stats[channel] = {
                    "available": True,
                    "snapshot_complete": True,
                    "platform": release_snapshot.platform,
                    "milestone": release_snapshot.milestone,
                    "version": release_snapshot.version,
                    "revision": release_snapshot.revision,
                    "runtime_declarations": len(release_chromium.declarations),
                }

            if release_channels:
                stats["sources"]["chromium_releases"] = release_stats

            release_rows = db.list_entities(
                source="chromium_release",
                kind="runtime_declaration",
                active_only=True,
            )
            releases_by_trial: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in release_rows:
                payload = row["payload"]
                if payload.get("channel") not in release_channels:
                    continue
                trial_name = str(payload.get("trial_name") or "")
                if trial_name:
                    releases_by_trial[trial_name.casefold()].append(payload)

            existing_runtime_gaps = db.list_entities(
                source="tracker",
                kind="runtime_declaration_gap",
                active_only=True,
            )
            stats["runtime_declaration_coverage"] = {
                "evaluated": False,
                "active_main_gaps": len(existing_runtime_gaps),
            }
            if (
                usable_chrome_snapshot is not None
                and chromium_snapshot is not None
                and declarations_complete
            ):
                coverage_emit = (
                    emit_baseline or (cs_initialized and chromium_initialized)
                )
                main_trial_names = {
                    str(declaration.get("trial_name") or "").casefold()
                    for declaration in (usable_chromium_declarations or [])
                    if declaration.get("trial_name")
                }
                active_trials_by_name = {
                    str(trial.get("trial_name") or ""): trial
                    for trial in usable_chrome_snapshot.active_trials
                    if trial.get("trial_name")
                }
                present_gap_keys: set[str] = set()
                for trial_name, trial in sorted(active_trials_by_name.items()):
                    if trial_name.casefold() in main_trial_names:
                        continue
                    present_gap_keys.add(trial_name)
                    release_presence = sorted(
                        {
                            (
                                f"{item.get('channel')} M{item.get('milestone')} "
                                f"({item.get('version')})"
                            )
                            for item in releases_by_trial.get(trial_name.casefold(), [])
                        }
                    )
                    trial_evidence = _trial_event_evidence(
                        trial,
                        chromestatus_base_url=config.chromestatus_base_url,
                    )
                    payload = {
                        "trial_name": trial_name,
                        "display_name": trial.get("display_name"),
                        "reason": (
                            "active Chrome Status OT has no runtime declaration on "
                            "Chromium main"
                        ),
                        "main_revision": chromium_snapshot.revision,
                        "release_presence": release_presence,
                        "chromestatus_url": trial_evidence["chromestatus_url"],
                    }
                    gap_change = db.upsert_entity(
                        run_id,
                        source="tracker",
                        kind="runtime_declaration_gap",
                        entity_key=trial_name,
                        payload=payload,
                        comparison_payload={"trial_name": trial_name},
                    )
                    if (
                        gap_change.created or gap_change.reactivated
                    ) and coverage_emit:
                        event(
                            category="coverage.runtime_declaration_missing",
                            severity="high",
                            source="tracker",
                            entity_kind="runtime_declaration_gap",
                            entity_key=trial_name,
                            new=payload,
                            evidence={
                                **trial_evidence,
                                "main_revision": chromium_snapshot.revision,
                                "release_presence": release_presence,
                            },
                        )
                resolved_runtime_gaps = db.mark_missing(
                    source="tracker",
                    kind="runtime_declaration_gap",
                    present_keys=present_gap_keys,
                )
                if coverage_emit:
                    for resolved in resolved_runtime_gaps:
                        trial_name = resolved["entity_key"]
                        trial = active_trials_by_name.get(trial_name)
                        if trial is None or trial_name.casefold() not in main_trial_names:
                            continue
                        trial_evidence = _trial_event_evidence(
                            trial,
                            chromestatus_base_url=config.chromestatus_base_url,
                        )
                        event(
                            category="coverage.runtime_declaration_restored",
                            severity="medium",
                            source="tracker",
                            entity_kind="runtime_declaration_gap",
                            entity_key=trial_name,
                            old=resolved["payload"],
                            evidence={
                                **trial_evidence,
                                "main_revision": chromium_snapshot.revision,
                            },
                        )
                stats["runtime_declaration_coverage"] = {
                    "evaluated": True,
                    "active_main_gaps": len(present_gap_keys),
                    "active_with_main_mapping": (
                        len(active_trials_by_name) - len(present_gap_keys)
                    ),
                    "active_trials": len(active_trials_by_name),
                }

            existing_contract_mismatches = db.list_entities(
                source="tracker",
                kind="cross_source_contract_mismatch",
                active_only=True,
            )
            stats["contracts"] = {
                "evaluated": False,
                "active_mismatches": len(existing_contract_mismatches),
            }
            if (
                usable_chrome_snapshot is not None
                and chromium_snapshot is not None
                and declarations_complete
            ):
                contract_emit = (
                    emit_baseline
                    or contracts_initialized
                    or (cs_initialized and chromium_initialized)
                )
                trial_by_name = {
                    str(trial.get("trial_name") or "").casefold(): trial
                    for trial in usable_chrome_snapshot.active_trials
                    if trial.get("trial_name")
                }
                contract_observations = cross_source_contract_observations(
                    usable_chrome_snapshot.active_trials,
                    usable_chromium_declarations or [],
                )
                observations_by_key = {
                    f"{observation['trial_name']}:{observation['field']}": observation
                    for observation in contract_observations
                }
                present_mismatch_keys: set[str] = set()
                for observation in contract_observations:
                    if observation["matches"]:
                        continue
                    key = f"{observation['trial_name']}:{observation['field']}"
                    present_mismatch_keys.add(key)
                    trial = trial_by_name[
                        str(observation["trial_name"]).casefold()
                    ]
                    trial_evidence = _trial_event_evidence(
                        trial,
                        chromestatus_base_url=config.chromestatus_base_url,
                    )
                    payload = {
                        "trial_name": observation["trial_name"],
                        "display_name": trial.get("display_name"),
                        "field": observation["field"],
                        "console": observation["console"],
                        "chromium": observation["chromium"],
                        "runtime_features": observation["runtime_features"],
                        "chromestatus_url": trial_evidence["chromestatus_url"],
                    }
                    change = db.upsert_entity(
                        run_id,
                        source="tracker",
                        kind="cross_source_contract_mismatch",
                        entity_key=key,
                        payload=payload,
                        comparison_payload={
                            field: payload[field]
                            for field in (
                                "trial_name",
                                "field",
                                "console",
                                "chromium",
                                "runtime_features",
                            )
                        },
                    )
                    if (
                        change.created
                        or change.reactivated
                        or (change.changed and not change.created)
                    ) and contract_emit:
                        event(
                            category="coverage.cross_source_contract_mismatch_detected",
                            severity="high",
                            source="tracker",
                            entity_kind="cross_source_contract_mismatch",
                            entity_key=key,
                            new=payload,
                            evidence={
                                **trial_evidence,
                                "field": observation["field"],
                                "console": observation["console"],
                                "chromium": observation["chromium"],
                                "runtime_features": observation["runtime_features"],
                            },
                        )

                resolved_mismatches = db.mark_missing(
                    source="tracker",
                    kind="cross_source_contract_mismatch",
                    present_keys=present_mismatch_keys,
                )
                if contract_emit:
                    for resolved in resolved_mismatches:
                        current = observations_by_key.get(resolved["entity_key"])
                        if current is None or not current["matches"]:
                            continue
                        trial = trial_by_name[
                            str(current["trial_name"]).casefold()
                        ]
                        trial_evidence = _trial_event_evidence(
                            trial,
                            chromestatus_base_url=config.chromestatus_base_url,
                        )
                        event(
                            category="coverage.cross_source_contract_mismatch_resolved",
                            severity="medium",
                            source="tracker",
                            entity_kind="cross_source_contract_mismatch",
                            entity_key=resolved["entity_key"],
                            old=resolved["payload"],
                            evidence={
                                **trial_evidence,
                                "field": current["field"],
                                "console": current["console"],
                                "chromium": current["chromium"],
                                "runtime_features": current["runtime_features"],
                            },
                        )
                db.set_meta("source.contracts.initialized", "1")
                stats["contracts"] = {
                    "evaluated": True,
                    "active_mismatches": len(present_mismatch_keys),
                }

            if gerrit_changes is not None:
                emit = emit_baseline or gerrit_initialized
                open_emit = emit_baseline or gerrit_open_initialized
                all_trials, active_trials = (
                    (
                        usable_chrome_snapshot.trials,
                        usable_chrome_snapshot.active_trials,
                    )
                    if usable_chrome_snapshot is not None
                    else _trial_lookup_from_db(db)
                )
                declarations = (
                    usable_chromium_declarations
                    if usable_chromium_declarations is not None
                    else _declarations_from_db(db)
                )
                active_targets = build_target_index(
                    active_trials, declarations, config.target_rules
                )
                known_targets = build_target_index(
                    all_trials, declarations, config.target_rules
                )
                official_trial_names = {
                    str(trial.get("trial_name") or "").casefold()
                    for trial in all_trials
                    if trial.get("trial_name")
                }
                current_runtime_trial_names = {
                    str(declaration.get("trial_name") or "").casefold()
                    for declaration in declarations
                    if declaration.get("trial_name")
                }

                def novel_candidate_declarations(
                    signal: dict[str, Any],
                ) -> list[str]:
                    return [
                        str(name)
                        for name in signal.get("declared_trial_names") or []
                        if str(name).casefold() not in official_trial_names
                        and str(name).casefold() not in current_runtime_trial_names
                        and not matches_ignore_pattern(
                            str(name), config.candidate_ignore_patterns
                        )
                    ]

                def is_unmatched_candidate_signal(
                    signal: dict[str, Any] | None,
                    known_matches: dict[str, Any],
                ) -> tuple[bool, list[str]]:
                    if signal is None:
                        return False, []
                    declared = signal.get("declared_trial_names") or []
                    novel = novel_candidate_declarations(signal)
                    if declared:
                        return bool(novel), novel
                    return not known_matches, []

                _apply_persisted_implementation_paths(
                    db,
                    active_targets,
                    minimum_score=config.gerrit_auto_path_min_score,
                )
                _apply_persisted_implementation_paths(
                    db,
                    known_targets,
                    minimum_score=config.gerrit_auto_path_min_score,
                )

                patch_cache: dict[str, dict[str, Any]] = {}
                detailed_patch_count = 0
                detailed_patch_errors = 0
                targeted_file_diff_count = 0
                implementation_candidate_state = {
                    row["entity_key"]: row["payload"]
                    for row in db.list_entities(
                        source="tracker",
                        kind="implementation_path_candidate",
                        active_only=True,
                    )
                }

                def with_diff_summary(change: dict[str, Any]) -> dict[str, Any]:
                    nonlocal detailed_patch_count, detailed_patch_errors
                    nonlocal targeted_file_diff_count
                    number = str(change.get("change_number"))
                    if number not in patch_cache:
                        summary = summarize_change_file_stats(change)
                        summary["detail"] = "file_stats"
                        if (
                            gerrit_client is not None
                            and config.gerrit_patch_details_enabled
                            and detailed_patch_count < config.gerrit_max_detailed_changes
                        ):
                            detailed_patch_count += 1
                            try:
                                summary = gerrit_client.fetch_patch_summary(change)
                                if summary.get("targeted_files"):
                                    targeted_file_diff_count += 1
                            except Exception:
                                detailed_patch_errors += 1
                                summary["detail_error"] = "Gerrit patch detail unavailable"
                        patch_cache[number] = summary
                    return {**change, "diff_summary": patch_cache[number]}

                def discover_paths(
                    trial_name: str,
                    target: dict[str, list[str]],
                    change: dict[str, Any],
                    *,
                    should_emit: bool,
                ) -> int:
                    rule = config.target_rules.get(trial_name)
                    if rule is not None and rule.paths:
                        return 0
                    created = 0
                    inferred = infer_implementation_path_candidates(
                        change, target["aliases"]
                    )
                    present_keys: set[str] = set()
                    status = str(change.get("status") or "").upper()
                    for candidate in inferred:
                        path = str(candidate["path"])
                        candidate_key = _implementation_candidate_key(trial_name, path)
                        present_keys.add(candidate_key)
                        current_score = int(candidate["score"])
                        accepted_now = (
                            current_score
                            >= config.gerrit_auto_path_min_score
                            and status == "MERGED"
                        )
                        previous = implementation_candidate_state.get(candidate_key) or {}
                        approval = previous.get("approval")
                        if not isinstance(approval, dict):
                            approval = None
                        if approval is None and previous.get("auto_applied"):
                            approval = {
                                "score": int(
                                    previous.get("approved_score")
                                    or previous.get("score")
                                    or 0
                                ),
                                "change_number": previous.get("approved_change_number")
                                or previous.get("change_number"),
                                "change_url": previous.get("approved_change_url")
                                or previous.get("change_url"),
                                "revision": previous.get("approved_revision")
                                or previous.get("revision"),
                                "change_status": "MERGED",
                            }
                        if approval is None and accepted_now:
                            approval = {
                                "score": current_score,
                                "change_number": change.get("change_number"),
                                "change_url": change.get("url"),
                                "revision": change.get("revision"),
                                "change_status": status,
                            }
                        accepted = approval is not None
                        approved_score = int((approval or {}).get("score") or 0)
                        payload = {
                            **candidate,
                            "score": approved_score if accepted else current_score,
                            "trial_name": trial_name,
                            "change_number": (
                                approval.get("change_number")
                                if accepted
                                else change.get("change_number")
                            ),
                            "change_url": (
                                approval.get("change_url")
                                if accepted
                                else change.get("url")
                            ),
                            "revision": (
                                approval.get("revision")
                                if accepted
                                else change.get("revision")
                            ),
                            "change_status": (
                                approval.get("change_status") if accepted else status
                            ),
                            "auto_applied": accepted,
                            "approved_score": approved_score,
                            "approval": approval,
                            "latest_evidence": {
                                "score": current_score,
                                "kind": candidate.get("kind"),
                                "files": candidate.get("files"),
                                "reasons": candidate.get("reasons"),
                                "change_number": change.get("change_number"),
                                "change_url": change.get("url"),
                                "revision": change.get("revision"),
                                "change_status": status,
                            },
                        }
                        candidate_change = db.upsert_entity(
                            run_id,
                            source="tracker",
                            kind="implementation_path_candidate",
                            entity_key=candidate_key,
                            payload=payload,
                        )
                        implementation_candidate_state[candidate_key] = payload
                        if accepted:
                            active_targets[trial_name]["paths"] = sorted(
                                {*active_targets[trial_name]["paths"], path}
                            )
                            known_targets[trial_name]["paths"] = sorted(
                                {*known_targets[trial_name]["paths"], path}
                            )
                        crossed_threshold = bool(
                            candidate_change.previous
                            and not candidate_change.previous.get("auto_applied")
                            and accepted
                        )
                        if candidate_change.created:
                            created += 1
                        if should_emit and (candidate_change.created or crossed_threshold):
                            event(
                                category="coverage.implementation_path_candidate_detected",
                                severity="medium" if accepted else "low",
                                source="tracker",
                                entity_kind="implementation_path_candidate",
                                entity_key=candidate_change.entity_key,
                                new=payload,
                                evidence={
                                    "trial_name": trial_name,
                                    "change_url": change.get("url"),
                                    "candidate_path": path,
                                    "score": candidate["score"],
                                    "auto_applied": accepted,
                                    "diff_summary": change.get("diff_summary"),
                                },
                            )

                    # Patchsets can move or remove files. Retain already merged
                    # evidence, but retire stale proposals from this same CL.
                    for row in db.list_entities(
                        source="tracker",
                        kind="implementation_path_candidate",
                        active_only=True,
                        key_prefix=f"{trial_name}:",
                    ):
                        prior = row["payload"]
                        if (
                            str(prior.get("change_number"))
                            == str(change.get("change_number"))
                            and row["entity_key"] not in present_keys
                            and not prior.get("auto_applied")
                        ):
                            db.deactivate_entity(
                                source="tracker",
                                kind="implementation_path_candidate",
                                entity_key=row["entity_key"],
                            )
                            implementation_candidate_state.pop(row["entity_key"], None)
                    return created

                # Re-evaluate persisted Gerrit candidates when scoring or target
                # mappings evolve.
                for row in db.list_entities(
                    source="gerrit",
                    kind="pre_registration_candidate",
                    active_only=True,
                ):
                    if not str(row["entity_key"]).startswith(("gerrit:", "gerrit-open:")):
                        continue
                    prior = row["payload"]
                    prior_change = prior.get("change") or {}
                    recalculated = gerrit_ot_signal(prior_change)
                    now_known = match_change_to_targets(prior_change, known_targets)
                    still_candidate, _ = is_unmatched_candidate_signal(
                        recalculated, now_known
                    )
                    if (
                        recalculated is None
                        or recalculated["score"] < 50
                        or not still_candidate
                    ):
                        old = db.deactivate_entity(
                            source="gerrit",
                            kind="pre_registration_candidate",
                            entity_key=row["entity_key"],
                        )
                        if str(row["entity_key"]).startswith("gerrit:"):
                            db.upsert_entity(
                                run_id,
                                source="gerrit",
                                kind="candidate_rejection",
                                entity_key=row["entity_key"],
                                payload={
                                    **prior,
                                    "score": (
                                        recalculated["score"] if recalculated else 0
                                    ),
                                    "reasons": (
                                        recalculated["reasons"]
                                        if recalculated
                                        else ["no longer satisfies OT signal rules"]
                                    ),
                                    "reclassified": True,
                                },
                            )
                        if old is not None and emit:
                            event(
                                category="candidate.reclassified_as_noise",
                                severity="low",
                                source="gerrit",
                                entity_kind="pre_registration_candidate",
                                entity_key=row["entity_key"],
                                old=old,
                                evidence={
                                    "known_target_match": sorted(now_known),
                                    "new_score": recalculated["score"] if recalculated else 0,
                                },
                            )

                merged_changes = [
                    change
                    for change in gerrit_changes
                    if str(change.get("status") or "MERGED").upper() == "MERGED"
                ]
                open_changes = [
                    change
                    for change in gerrit_changes
                    if str(change.get("status") or "").upper() in {"NEW", "OPEN"}
                ]
                closed_changes = [
                    change
                    for change in gerrit_changes
                    if str(change.get("status") or "").upper() in {"MERGED", "ABANDONED"}
                ]

                matched_change_count = 0
                candidate_change_count = 0
                framework_change_count = 0
                inferred_path_count = 0
                for change in merged_changes:
                    number = str(change.get("change_number"))
                    initial_active_matches = match_change_to_targets(
                        change, active_targets
                    )
                    initial_signal = gerrit_ot_signal(change)
                    detailed_change = (
                        with_diff_summary(change)
                        if initial_active_matches or initial_signal is not None
                        else change
                    )
                    active_matches = match_change_to_targets(
                        detailed_change, active_targets
                    )
                    known_matches = match_change_to_targets(
                        detailed_change, known_targets
                    )
                    signal = gerrit_ot_signal(detailed_change)
                    candidate_signal, candidate_declared_trial_names = (
                        is_unmatched_candidate_signal(signal, known_matches)
                    )
                    for trial_name, match in sorted(active_matches.items()):
                        inferred_path_count += discover_paths(
                            trial_name,
                            active_targets[trial_name],
                            detailed_change,
                            should_emit=emit,
                        )
                        key = f"{number}:{trial_name}"
                        payload = {
                            "trial_name": trial_name,
                            "match": match,
                            "change": detailed_change,
                        }
                        entity_change = db.upsert_entity(
                            run_id,
                            source="gerrit",
                            kind="ot_code_change",
                            entity_key=key,
                            payload=payload,
                        )
                        matched_change_count += int(entity_change.created)
                        if entity_change.created and emit:
                            event(
                                category="chromium.implementation_changed",
                                severity="medium",
                                source="gerrit",
                                entity_kind="ot_code_change",
                                entity_key=key,
                                new=payload,
                                evidence={
                                    "change_url": change.get("url"),
                                    "matched_aliases": match["aliases"],
                                    "matched_paths": match["paths"],
                                    "diff_summary": detailed_change.get("diff_summary"),
                                },
                            )

                    framework_files = [
                        path
                        for path in change.get("files") or []
                        if "/origin_trials/" in path
                    ]
                    if signal and framework_files:
                        key = f"framework:{number}"
                        payload = {
                            "change": detailed_change,
                            "signal": signal,
                            "framework_files": framework_files,
                        }
                        entity_change = db.upsert_entity(
                            run_id,
                            source="gerrit",
                            kind="framework_change",
                            entity_key=key,
                            payload=payload,
                        )
                        framework_change_count += int(entity_change.created)
                        if entity_change.created and emit:
                            event(
                                category="chromium.ot_framework_changed",
                                severity="medium",
                                source="gerrit",
                                entity_kind="framework_change",
                                entity_key=key,
                                new=payload,
                                evidence={
                                    "change_url": change.get("url"),
                                    "diff_summary": detailed_change.get("diff_summary"),
                                },
                            )

                    if signal and candidate_signal:
                        key = f"gerrit:{number}"
                        declared_trial_names = candidate_declared_trial_names
                        payload = {
                            "candidate_key": key,
                            "signal_type": "unmatched_origin_trial_code_change",
                            "trial_name": (
                                declared_trial_names[0]
                                if len(declared_trial_names) == 1
                                else None
                            ),
                            "declared_trial_names": declared_trial_names,
                            "score": signal["score"],
                            "reasons": signal["reasons"],
                            "change": detailed_change,
                            "matched_terms": signal["matched_terms"],
                            "special_paths": signal["special_paths"],
                        }
                        kind = (
                            "pre_registration_candidate"
                            if signal["score"] >= 50
                            else "candidate_rejection"
                        )
                        entity_change = db.upsert_entity(
                            run_id,
                            source="gerrit",
                            kind=kind,
                            entity_key=key,
                            payload=payload,
                        )
                        if kind == "pre_registration_candidate":
                            candidate_change_count += int(entity_change.created)
                            if entity_change.created and emit:
                                event(
                                    category="candidate.code_signal_detected",
                                    severity=(
                                        "high" if signal["score"] >= 80 else "medium"
                                    ),
                                    source="gerrit",
                                    entity_kind=kind,
                                    entity_key=key,
                                    new=payload,
                                    evidence={
                                        "change_url": change.get("url"),
                                        "trial_name": payload["trial_name"],
                                        "declared_trial_names": declared_trial_names,
                                        "diff_summary": detailed_change.get("diff_summary"),
                                    },
                                )

                new_premerge_count = 0
                updated_premerge_count = 0
                open_candidate_count = 0
                for change in open_changes:
                    number = str(change.get("change_number"))
                    initial_active_matches = match_change_to_targets(
                        change, active_targets
                    )
                    initial_signal = gerrit_ot_signal(change)
                    detailed_change = (
                        with_diff_summary(change)
                        if initial_active_matches or initial_signal is not None
                        else change
                    )
                    active_matches = match_change_to_targets(
                        detailed_change, active_targets
                    )
                    known_matches = match_change_to_targets(
                        detailed_change, known_targets
                    )
                    signal = gerrit_ot_signal(detailed_change)
                    candidate_signal, candidate_declared_trial_names = (
                        is_unmatched_candidate_signal(signal, known_matches)
                    )
                    current_keys: set[str] = set()
                    for trial_name, match in sorted(active_matches.items()):
                        match_strength = _premerge_match_strength(
                            trial_name, match, detailed_change
                        )
                        # Open CLs are evidence for internal tracking only. A
                        # user-facing code alert is emitted after the CL merges.
                        notification_severity = "low"
                        inferred_path_count += discover_paths(
                            trial_name,
                            active_targets[trial_name],
                            detailed_change,
                            should_emit=open_emit,
                        )
                        key = f"{number}:{trial_name}"
                        current_keys.add(key)
                        payload = {
                            "trial_name": trial_name,
                            "match": match,
                            "change": detailed_change,
                        }
                        entity_change = db.upsert_entity(
                            run_id,
                            source="gerrit",
                            kind="premerge_ot_code_change",
                            entity_key=key,
                            payload=payload,
                            comparison_payload=_gerrit_comparison_payload(payload),
                        )
                        if entity_change.created or entity_change.reactivated:
                            new_premerge_count += 1
                            if open_emit:
                                event(
                                    category="chromium.premerge_change_detected",
                                    severity=notification_severity,
                                    source="gerrit",
                                    entity_kind="premerge_ot_code_change",
                                    entity_key=key,
                                    new=payload,
                                    evidence={
                                        "change_url": change.get("url"),
                                        "patchset": change.get("patchset"),
                                        "work_in_progress": change.get("work_in_progress"),
                                        "matched_aliases": match["aliases"],
                                        "matched_paths": match["paths"],
                                        "match_strength": match_strength,
                                        "diff_summary": detailed_change.get("diff_summary"),
                                    },
                                )
                        elif entity_change.changed:
                            updated_premerge_count += 1
                            if open_emit:
                                event(
                                    category="chromium.premerge_patchset_updated",
                                    severity=notification_severity,
                                    source="gerrit",
                                    entity_kind="premerge_ot_code_change",
                                    entity_key=key,
                                    old=entity_change.previous,
                                    new=payload,
                                    evidence={
                                        "change_url": change.get("url"),
                                        "old_patchset": (
                                            (entity_change.previous or {}).get("change") or {}
                                        ).get("patchset"),
                                        "new_patchset": change.get("patchset"),
                                        "matched_aliases": match["aliases"],
                                        "matched_paths": match["paths"],
                                        "match_strength": match_strength,
                                        "diff_summary": detailed_change.get("diff_summary"),
                                    },
                                )

                    for row in db.list_entities(
                        source="gerrit",
                        kind="premerge_ot_code_change",
                        active_only=True,
                        key_prefix=f"{number}:",
                    ):
                        if row["entity_key"] not in current_keys:
                            db.deactivate_entity(
                                source="gerrit",
                                kind="premerge_ot_code_change",
                                entity_key=row["entity_key"],
                            )

                    framework_files = [
                        path
                        for path in change.get("files") or []
                        if "/origin_trials/" in path
                    ]
                    if signal and framework_files:
                        key = f"framework-open:{number}"
                        payload = {
                            "change": detailed_change,
                            "signal": signal,
                            "framework_files": framework_files,
                        }
                        framework_change = db.upsert_entity(
                            run_id,
                            source="gerrit",
                            kind="premerge_framework_change",
                            entity_key=key,
                            payload=payload,
                            comparison_payload=_gerrit_comparison_payload(payload),
                        )
                        if (framework_change.created or framework_change.reactivated) and open_emit:
                            event(
                                category="chromium.premerge_framework_change_detected",
                                severity="low",
                                source="gerrit",
                                entity_kind="premerge_framework_change",
                                entity_key=key,
                                new=payload,
                                evidence={
                                    "change_url": change.get("url"),
                                    "patchset": change.get("patchset"),
                                    "match_strength": (
                                        "direct"
                                        if _gerrit_signal_is_direct(signal)
                                        else "path_only"
                                    ),
                                    "diff_summary": detailed_change.get("diff_summary"),
                                },
                            )

                    open_key = f"gerrit-open:{number}"
                    if signal and candidate_signal and signal["score"] >= 50:
                        declared_trial_names = candidate_declared_trial_names
                        payload = {
                            "candidate_key": open_key,
                            "signal_type": "open_origin_trial_code_change",
                            "trial_name": (
                                declared_trial_names[0]
                                if len(declared_trial_names) == 1
                                else None
                            ),
                            "declared_trial_names": declared_trial_names,
                            "score": signal["score"],
                            "reasons": signal["reasons"],
                            "change": detailed_change,
                            "matched_terms": signal["matched_terms"],
                            "special_paths": signal["special_paths"],
                        }
                        candidate_change = db.upsert_entity(
                            run_id,
                            source="gerrit",
                            kind="pre_registration_candidate",
                            entity_key=open_key,
                            payload=payload,
                            comparison_payload=_gerrit_comparison_payload(payload),
                        )
                        if candidate_change.created or candidate_change.reactivated:
                            open_candidate_count += 1
                            if open_emit:
                                event(
                                    category="candidate.premerge_code_signal_detected",
                                    severity="low",
                                    source="gerrit",
                                    entity_kind="pre_registration_candidate",
                                    entity_key=open_key,
                                    new=payload,
                                    evidence={
                                        "change_url": change.get("url"),
                                        "patchset": change.get("patchset"),
                                        "trial_name": payload["trial_name"],
                                        "declared_trial_names": declared_trial_names,
                                        "diff_summary": detailed_change.get("diff_summary"),
                                    },
                                )
                        elif candidate_change.changed and open_emit:
                            event(
                                category="candidate.premerge_patchset_updated",
                                severity="low",
                                source="gerrit",
                                entity_kind="pre_registration_candidate",
                                entity_key=open_key,
                                old=candidate_change.previous,
                                new=payload,
                                evidence={
                                    "change_url": change.get("url"),
                                    "patchset": change.get("patchset"),
                                    "trial_name": payload["trial_name"],
                                    "declared_trial_names": declared_trial_names,
                                    "diff_summary": detailed_change.get("diff_summary"),
                                },
                            )
                    else:
                        db.deactivate_entity(
                            source="gerrit",
                            kind="pre_registration_candidate",
                            entity_key=open_key,
                        )

                for change in closed_changes:
                    number = str(change.get("change_number"))
                    resolution = str(change.get("status") or "").upper()
                    for row in db.list_entities(
                        source="gerrit",
                        kind="premerge_ot_code_change",
                        active_only=True,
                        key_prefix=f"{number}:",
                    ):
                        old = db.deactivate_entity(
                            source="gerrit",
                            kind="premerge_ot_code_change",
                            entity_key=row["entity_key"],
                        )
                        if old is not None and open_emit:
                            event(
                                category=(
                                    "chromium.premerge_change_merged"
                                    if resolution == "MERGED"
                                    else "chromium.premerge_change_abandoned"
                                ),
                                severity="low",
                                source="gerrit",
                                entity_kind="premerge_ot_code_change",
                                entity_key=row["entity_key"],
                                old=old,
                                evidence={"change_url": change.get("url")},
                            )
                    db.deactivate_entity(
                        source="gerrit",
                        kind="premerge_framework_change",
                        entity_key=f"framework-open:{number}",
                    )
                    db.deactivate_entity(
                        source="gerrit",
                        kind="pre_registration_candidate",
                        entity_key=f"gerrit-open:{number}",
                    )
                    if resolution == "ABANDONED":
                        for row in db.list_entities(
                            source="tracker",
                            kind="implementation_path_candidate",
                            active_only=True,
                        ):
                            payload = row["payload"]
                            if (
                                str(payload.get("change_number")) == number
                                and not payload.get("auto_applied")
                            ):
                                db.deactivate_entity(
                                    source="tracker",
                                    kind="implementation_path_candidate",
                                    entity_key=row["entity_key"],
                                )

                implementation_gap_keys: set[str] = set()
                for trial_name, target in sorted(active_targets.items()):
                    if target["paths"]:
                        continue
                    implementation_gap_keys.add(trial_name)
                    candidate_paths = [
                        row["payload"]
                        for row in db.list_entities(
                            source="tracker",
                            kind="implementation_path_candidate",
                            active_only=True,
                            key_prefix=f"{trial_name}:",
                        )
                    ]
                    payload = {
                        "trial_name": trial_name,
                        "aliases": target["aliases"],
                        "path_candidates": candidate_paths,
                        "reason": (
                            "alias monitoring is active, but no explicit or high-confidence "
                            "inferred implementation path is available"
                        ),
                    }
                    gap_change = db.upsert_entity(
                        run_id,
                        source="tracker",
                        kind="implementation_path_gap",
                        entity_key=trial_name,
                        payload=payload,
                    )
                    if (gap_change.created or gap_change.reactivated) and emit:
                        event(
                            category="coverage.implementation_path_missing",
                            severity="medium",
                            source="tracker",
                            entity_kind="implementation_path_gap",
                            entity_key=trial_name,
                            new=payload,
                            evidence={"trial_name": trial_name},
                        )
                resolved_gaps = db.mark_missing(
                    source="tracker",
                    kind="implementation_path_gap",
                    present_keys=implementation_gap_keys,
                )
                if emit:
                    for resolved in resolved_gaps:
                        event(
                            category="coverage.implementation_path_added",
                            severity="low",
                            source="tracker",
                            entity_kind="implementation_path_gap",
                            entity_key=resolved["entity_key"],
                            old=resolved["payload"],
                            evidence={"trial_name": resolved["entity_key"]},
                        )

                db.set_meta("source.gerrit.initialized", "1")
                db.set_meta("source.gerrit.open_initialized", "1")
                db.set_meta(
                    "source.gerrit.watermark",
                    collection_started.isoformat(timespec="seconds"),
                )
                stats["sources"]["gerrit"] = {
                    "changes_scanned": len(gerrit_changes),
                    "merged_changes_scanned": len(merged_changes),
                    "open_changes_scanned": len(open_changes),
                    "closed_changes_scanned": len(closed_changes),
                    "new_target_matches": matched_change_count,
                    "new_framework_changes": framework_change_count,
                    "new_candidate_signals": candidate_change_count,
                    "new_premerge_matches": new_premerge_count,
                    "updated_premerge_patchsets": updated_premerge_count,
                    "new_open_candidate_signals": open_candidate_count,
                    "new_inferred_path_candidates": inferred_path_count,
                    "detailed_patches": detailed_patch_count,
                    "detailed_patch_errors": detailed_patch_errors,
                    "targeted_file_diffs": targeted_file_diff_count,
                    "targets_with_alias_monitoring": len(active_targets),
                    "targets_with_explicit_paths": sum(
                        1 for target in active_targets.values() if target["paths"]
                    ),
                    "targets_without_explicit_paths": len(implementation_gap_keys),
                }
                health_recovery_scopes.add("gerrit")

            existing_health_rows = db.list_entities(
                source="tracker",
                kind="source_health_issue",
                active_only=True,
            )
            current_health_keys = set(health_issues)
            for key, payload in sorted(health_issues.items()):
                health_change = db.upsert_entity(
                    run_id,
                    source="tracker",
                    kind="source_health_issue",
                    entity_key=key,
                    payload=payload,
                )
                previous_error = (
                    health_change.previous.get("error")
                    if isinstance(health_change.previous, dict)
                    else None
                )
                if (
                    health_change.created
                    or health_change.reactivated
                    or (
                        health_change.changed
                        and previous_error != payload.get("error")
                    )
                ):
                    event(
                        category="source.health_degraded",
                        severity="high",
                        source="tracker",
                        entity_kind="source_health_issue",
                        entity_key=key,
                        new=payload,
                        evidence=payload,
                    )

            recovered_health_count = 0
            for row in existing_health_rows:
                key = row["entity_key"]
                payload = row["payload"]
                if (
                    key in current_health_keys
                    or payload.get("scope") not in health_recovery_scopes
                ):
                    continue
                previous = db.deactivate_entity(
                    source="tracker",
                    kind="source_health_issue",
                    entity_key=key,
                )
                if previous is None:
                    continue
                recovered_health_count += 1
                event(
                    category="source.health_recovered",
                    severity="medium",
                    source="tracker",
                    entity_kind="source_health_issue",
                    entity_key=key,
                    old=previous,
                    evidence={
                        **previous,
                        "recovered_at": utc_now(),
                    },
                )
            stats["source_health"] = {
                "checked_scopes": sorted(health_checked_scopes),
                "healthy_scopes": sorted(health_recovery_scopes),
                "active_issues": len(
                    db.list_entities(
                        source="tracker",
                        kind="source_health_issue",
                        active_only=True,
                    )
                ),
                "recovered": recovered_health_count,
            }

            db.commit_changes()
            if not stats["sources"]:
                status = "failed"
            elif errors:
                status = "partial"
            else:
                status = "complete"
            stats["events_created"] = events_created
            db.finish_run(
                run_id,
                status=status,
                stats=stats,
                error_text="; ".join(
                    f"{item['source']}: {item['error']}" for item in errors
                )
                or None,
            )
            return SyncResult(
                run_id=run_id,
                status=status,
                stats=stats,
                events_created=events_created,
            )
        except Exception as exc:
            db.rollback_changes()
            stats["events_created"] = 0
            errors.append({"source": "tracker", "error": str(exc)})
            stats["errors"] = errors
            db.finish_run(
                run_id,
                status="failed",
                stats=stats,
                error_text=str(exc),
            )
            raise
