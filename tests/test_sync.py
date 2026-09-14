import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ot_tracker.chromestatus import ChromeStatusSnapshot
from ot_tracker.chromium import (
    MANUAL_COMPLETION_PATH,
    NAVIGATION_FEATURES_PATH,
    PERSISTENT_TRIALS_PATH,
    RUNTIME_FEATURES_PATH,
    ChromiumSnapshot,
)
from ot_tracker.config import TargetRule, TrackerConfig
from ot_tracker.db import TrackerDB
from ot_tracker.diffing import deep_diff
from ot_tracker.reporting import build_report_data
from ot_tracker.sync import (
    _chromestatus_snapshot_health,
    _compact_diffs,
    _declaration_snapshot_health,
    _gerrit_comparison_payload,
    _premerge_match_strength,
    run_sync,
)


class StaticClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def fetch_snapshot(self):
        return self.snapshot


class MutableGerritClient:
    change = {}
    patch_summary = None

    def __init__(self, config, http):
        self.config = config

    def fetch_changes(self, after):
        return [dict(self.change)]

    def fetch_patch_summary(self, change):
        if self.patch_summary is None:
            raise AssertionError("patch details are disabled in this test")
        return dict(self.patch_summary)


def trial_snapshot() -> ChromeStatusSnapshot:
    trial = {
        "id": "1",
        "display_name": "Example Trial",
        "description": "Example",
        "trial_name": "ExampleTrial",
        "enabled": True,
        "status": "ACTIVE",
        "type": "ORIGIN_TRIAL",
        "allow_third_party_origins": False,
        "start_milestone": 140,
        "end_milestone": 142,
        "chromestatus_url": None,
        "chromestatus_feature_id": None,
        "trial_extensions": [],
    }
    return ChromeStatusSnapshot(
        trials=[trial],
        active_trials=[trial],
        features={},
        feature_errors={},
    )


def many_trial_snapshot(count: int) -> ChromeStatusSnapshot:
    trials = []
    for index in range(1, count + 1):
        trial = dict(trial_snapshot().trials[0])
        trial["id"] = str(index)
        trial["display_name"] = f"Trial {index}"
        trial["trial_name"] = "ExampleTrial" if index == 1 else f"Trial{index}"
        trials.append(trial)
    return ChromeStatusSnapshot(
        trials=trials,
        active_trials=list(trials),
        features={},
        feature_errors={},
    )


def chromium_snapshot() -> ChromiumSnapshot:
    return ChromiumSnapshot(
        revision="a" * 40,
        files={
            RUNTIME_FEATURES_PATH: "origin_trial_feature_name: 'ExampleTrial'",
            MANUAL_COMPLETION_PATH: "",
            NAVIGATION_FEATURES_PATH: "",
            PERSISTENT_TRIALS_PATH: "",
        },
        file_errors={},
        declarations=[
            {
                "runtime_feature_name": "ExampleRuntime",
                "trial_name": "ExampleTrial",
                "fields": {"name": "ExampleRuntime"},
                "classifications": [],
                "source_path": "runtime_enabled_features.json5",
                "source_line": 1,
                "source_excerpt": "{name: 'ExampleRuntime'}",
            }
        ],
        classifications={},
    )


def gerrit_change(
    *,
    status: str,
    revision: str,
    patchset: int,
    updated: str,
    number: int = 123,
    subject: str = "Implement ExampleTrial",
    changed_files=None,
):
    files = changed_files or [
        "third_party/blink/renderer/modules/example_trial/service.cc",
        "third_party/blink/renderer/modules/example_trial/service.h",
    ]
    return {
        "change_number": number,
        "change_id": "Iabc",
        "project_id": "chromium%2Fsrc~main~Iabc",
        "status": status,
        "work_in_progress": False,
        "subject": subject,
        "created": "2026-09-01 00:00:00.000000000",
        "submitted": None,
        "updated": updated,
        "revision": revision,
        "patchset": patchset,
        "commit_message": subject,
        "author": {"name": "Dev", "email": "dev@example.com"},
        "files": files,
        "file_stats": [
            {
                "path": path,
                "status": "M",
                "lines_inserted": patchset,
                "lines_deleted": 0,
            }
            for path in files
        ],
        "url": f"https://chromium-review.googlesource.com/c/chromium/src/+/{number}",
    }


class TrackerStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        MutableGerritClient.change = {}
        MutableGerritClient.patch_summary = None

    def test_compact_diffs_omits_parser_location_noise(self) -> None:
        previous = {
            "fields": {"origin_trial_os": ["linux"]},
            "source_line": 100,
            "source_excerpt": "old declaration text",
        }
        current = {
            "fields": {"origin_trial_os": ["linux", "android"]},
            "source_line": 120,
            "source_excerpt": "new declaration text",
        }

        compacted = _compact_diffs(deep_diff(previous, current))

        self.assertEqual(
            ["/fields/origin_trial_os"],
            [change["path"] for change in compacted],
        )
        self.assertEqual(["linux"], compacted[0]["old"])
        self.assertEqual(["linux", "android"], compacted[0]["new"])

    def test_implausible_declaration_count_drop_is_rejected(self) -> None:
        complete, error = _declaration_snapshot_health(
            ChromiumSnapshot(
                revision="b" * 40,
                files={RUNTIME_FEATURES_PATH: ""},
                file_errors={},
                declarations=[],
                classifications={},
            ),
            previous_count=103,
        )
        self.assertFalse(complete)
        self.assertIn("collapsed from 103 to 0", error or "")

    def test_malformed_chromestatus_active_list_is_rejected(self) -> None:
        snapshot = trial_snapshot()
        snapshot.active_trials.clear()
        complete, error = _chromestatus_snapshot_health(
            snapshot,
            previous_count=1,
            previous_active_count=1,
        )
        self.assertFalse(complete)
        self.assertIn("active list", error or "")

    def test_comment_timestamp_is_ignored_but_patchset_is_not(self) -> None:
        payload = {"change": gerrit_change(
            status="NEW", revision="one", patchset=1, updated="first"
        )}
        comment_only = {"change": {
            **payload["change"],
            "updated": "second",
            "diff_summary": {
                "detail": "unified_diff",
                "changed_symbols": ["ExampleService::Start"],
            },
        }}
        next_patchset = {"change": {
            **comment_only["change"],
            "revision": "two",
            "patchset": 2,
        }}
        self.assertEqual(
            _gerrit_comparison_payload(payload),
            _gerrit_comparison_payload(comment_only),
        )
        self.assertNotEqual(
            _gerrit_comparison_payload(payload),
            _gerrit_comparison_payload(next_patchset),
        )

    def test_premerge_match_strength_requires_direct_ot_evidence(self) -> None:
        path_only = {"aliases": [], "paths": ["content/browser/shared/service.cc"]}
        direct_alias = {"aliases": ["ExampleTrial"], "paths": []}
        change = gerrit_change(
            status="NEW",
            revision="one",
            patchset=1,
            updated="first",
            subject="Refactor shared service",
        )
        self.assertEqual(
            "path_only",
            _premerge_match_strength("ExampleTrial", path_only, change),
        )
        self.assertEqual(
            "direct",
            _premerge_match_strength("ExampleTrial", direct_alias, change),
        )
        change["diff_summary"] = {
            "added_origin_trial_feature_names": ["ExampleTrial"]
        }
        self.assertEqual(
            "direct",
            _premerge_match_strength("ExampleTrial", path_only, change),
        )

    def test_path_only_open_change_is_recorded_but_not_discord_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_patch_details_enabled=False,
                target_rules={
                    "ExampleTrial": TargetRule(
                        name="ExampleTrial",
                        paths=("content/browser/shared/service.cc",),
                    )
                },
            )
            chrome_client = StaticClient(trial_snapshot())
            chromium_client = StaticClient(chromium_snapshot())
            with (
                patch("ot_tracker.sync.ChromeStatusClient", return_value=chrome_client),
                patch("ot_tracker.sync.ChromiumSourceClient", return_value=chromium_client),
            ):
                self.assertEqual(
                    "complete", run_sync(config, gerrit_enabled=False).status
                )

            MutableGerritClient.change = gerrit_change(
                status="NEW",
                revision="path-only",
                patchset=1,
                updated="first",
                subject="Rename shared service helper",
                changed_files=["content/browser/shared/service.cc"],
            )
            with (
                patch("ot_tracker.sync.ChromeStatusClient", return_value=chrome_client),
                patch("ot_tracker.sync.ChromiumSourceClient", return_value=chromium_client),
                patch("ot_tracker.sync.GerritClient", MutableGerritClient),
            ):
                result = run_sync(config, emit_baseline=True)
                self.assertEqual("complete", result.status)

                MutableGerritClient.change = gerrit_change(
                    status="NEW",
                    revision="path-only-v2",
                    patchset=2,
                    updated="second",
                    subject="Rename shared service helper",
                    changed_files=["content/browser/shared/service.cc"],
                )
                updated = run_sync(config)
                self.assertEqual("complete", updated.status)

            with TrackerDB(config.database_path) as db:
                events = [
                    event
                    for event in db.list_events()
                    if event["category"].startswith("chromium.premerge_")
                ]
                self.assertEqual(
                    {
                        "chromium.premerge_change_detected",
                        "chromium.premerge_patchset_updated",
                    },
                    {event["category"] for event in events},
                )
                self.assertTrue(all(event["severity"] == "low" for event in events))
                self.assertTrue(
                    all(
                        event["evidence"]["match_strength"] == "path_only"
                        for event in events
                    )
                )
                self.assertFalse(
                    db.pending_notification_events(
                        channel="discord", min_severity="medium"
                    )
                )

    def test_open_patch_declaration_creates_named_early_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
            )
            MutableGerritClient.change = gerrit_change(
                status="NEW",
                revision="future",
                patchset=1,
                updated="first",
                subject="Enable the new API surface",
                changed_files=[RUNTIME_FEATURES_PATH],
            )
            MutableGerritClient.patch_summary = {
                "detail": "unified_diff",
                "files_changed": 1,
                "insertions": 2,
                "deletions": 0,
                "top_files": [
                    {
                        "path": RUNTIME_FEATURES_PATH,
                        "insertions": 2,
                        "deletions": 0,
                    }
                ],
                "hunk_contexts": [],
                "changed_symbols": [],
                "added_origin_trial_feature_names": ["FutureTrial"],
            }
            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(trial_snapshot()),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(chromium_snapshot()),
                ),
                patch("ot_tracker.sync.GerritClient", MutableGerritClient),
            ):
                result = run_sync(config, emit_baseline=True)
                self.assertEqual("complete", result.status)

            with TrackerDB(config.database_path) as db:
                candidates = db.list_entities(
                    source="gerrit",
                    kind="pre_registration_candidate",
                    active_only=True,
                )
                self.assertEqual(1, len(candidates))
                self.assertEqual("FutureTrial", candidates[0]["payload"]["trial_name"])
                self.assertGreaterEqual(candidates[0]["payload"]["score"], 90)
                events = [
                    event
                    for event in db.list_events()
                    if event["category"]
                    == "candidate.premerge_code_signal_detected"
                ]
                self.assertEqual("FutureTrial", events[0]["evidence"]["trial_name"])
                self.assertEqual("low", events[0]["severity"])
                self.assertFalse(
                    db.pending_notification_events(
                        channel="discord", min_severity="medium"
                    )
                )

    def test_open_patch_declaration_for_known_trial_is_not_new_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
            )
            MutableGerritClient.change = gerrit_change(
                status="NEW",
                revision="known",
                patchset=1,
                updated="first",
                subject="Enable the new API surface",
                changed_files=[RUNTIME_FEATURES_PATH],
            )
            MutableGerritClient.patch_summary = {
                "detail": "unified_diff",
                "files_changed": 1,
                "insertions": 1,
                "deletions": 0,
                "top_files": [],
                "hunk_contexts": [],
                "changed_symbols": [],
                "added_origin_trial_feature_names": ["ExampleTrial"],
            }
            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(trial_snapshot()),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(chromium_snapshot()),
                ),
                patch("ot_tracker.sync.GerritClient", MutableGerritClient),
            ):
                result = run_sync(config, emit_baseline=True)
                self.assertEqual("complete", result.status)

            with TrackerDB(config.database_path) as db:
                self.assertFalse(
                    db.list_entities(
                        source="gerrit",
                        kind="pre_registration_candidate",
                        active_only=True,
                    )
                )
                linked = db.list_entities(
                    source="gerrit",
                    kind="premerge_ot_code_change",
                    active_only=True,
                )
                self.assertEqual("ExampleTrial", linked[0]["payload"]["trial_name"])
                premerge_events = [
                    event
                    for event in db.list_events()
                    if event["category"] == "chromium.premerge_change_detected"
                ]
                self.assertEqual("low", premerge_events[0]["severity"])
                self.assertFalse(
                    db.pending_notification_events(
                        channel="discord", min_severity="medium"
                    )
                )

    def test_open_patch_does_not_reannounce_existing_runtime_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
            )
            source_snapshot = chromium_snapshot()
            source_snapshot.declarations.append(
                {
                    "runtime_feature_name": "ExistingRuntime",
                    "trial_name": "ExistingCandidate",
                    "fields": {
                        "name": "ExistingRuntime",
                        "origin_trial_feature_name": "ExistingCandidate",
                    },
                    "classifications": [],
                    "source_path": RUNTIME_FEATURES_PATH,
                    "source_line": 2,
                    "source_excerpt": "{name: 'ExistingRuntime'}",
                }
            )
            MutableGerritClient.change = gerrit_change(
                status="NEW",
                revision="stale-large-change",
                patchset=1,
                updated="first",
                subject="Mechanical generated-code update",
                changed_files=[RUNTIME_FEATURES_PATH],
            )
            MutableGerritClient.patch_summary = {
                "detail": "targeted_file_diff",
                "files_changed": 200,
                "insertions": 1000,
                "deletions": 1000,
                "top_files": [],
                "hunk_contexts": [],
                "changed_symbols": [],
                "added_origin_trial_feature_names": ["ExistingCandidate"],
                "targeted_files": [RUNTIME_FEATURES_PATH],
            }
            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(trial_snapshot()),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(source_snapshot),
                ),
                patch("ot_tracker.sync.GerritClient", MutableGerritClient),
            ):
                result = run_sync(config, emit_baseline=True)
                self.assertEqual("complete", result.status)
                self.assertEqual(
                    1, result.stats["sources"]["gerrit"]["targeted_file_diffs"]
                )

            with TrackerDB(config.database_path) as db:
                self.assertFalse(
                    db.list_entities(
                        source="gerrit",
                        kind="pre_registration_candidate",
                        active_only=True,
                    )
                )
                source_candidates = db.list_entities(
                    source="chromium",
                    kind="pre_registration_candidate",
                    active_only=True,
                )
                self.assertEqual(
                    "ExistingCandidate", source_candidates[0]["payload"]["trial_name"]
                )

    def test_chromestatus_change_event_keeps_name_and_evidence_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_enabled=False,
            )
            first = trial_snapshot()
            first.trials[0]["chromestatus_url"] = (
                "https://chromestatus.com/feature/123456"
            )
            first.trials[0]["chromestatus_feature_id"] = "123456"
            second = trial_snapshot()
            second.trials[0]["chromestatus_url"] = (
                "https://chromestatus.com/feature/123456"
            )
            second.trials[0]["chromestatus_feature_id"] = "123456"
            second.trials[0]["end_milestone"] = 143
            chromium_client = StaticClient(chromium_snapshot())
            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(first),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=chromium_client,
                ),
            ):
                baseline = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", baseline.status)
            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(second),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=chromium_client,
                ),
            ):
                changed = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", changed.status)

            with TrackerDB(config.database_path) as db:
                events = [
                    event
                    for event in db.list_events()
                    if event["category"] == "official_ot.milestone_changed"
                ]
                self.assertEqual(1, len(events))
                self.assertEqual("Example Trial", events[0]["evidence"]["display_name"])
                self.assertEqual(
                    "https://chromestatus.com/feature/123456",
                    events[0]["evidence"]["chromestatus_url"],
                )

    def test_chromestatus_audit_timestamp_is_removed_without_metadata_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_enabled=False,
            )
            old_feature = {
                "id": "42",
                "name": "Example Feature",
                "source_updated": {"when": "2026-08-31T00:00:00Z"},
                "stages": [],
            }
            current_feature = {
                "id": "42",
                "name": "Example Feature",
                "stages": [],
            }
            with TrackerDB(config.database_path) as db:
                old_run = db.begin_run(baseline=True)
                db.begin_changes()
                db.upsert_entity(
                    old_run,
                    source="chromestatus",
                    kind="feature",
                    entity_key="42",
                    payload=old_feature,
                )
                db.commit_changes()
                db.finish_run(old_run, status="complete", stats={})
                db.set_meta("source.chromestatus.initialized", "1")
                db.set_meta("source.chromium.initialized", "1")
                db.commit_changes()

            chrome_snapshot = trial_snapshot()
            chrome_snapshot.features["42"] = current_feature
            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(chrome_snapshot),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(chromium_snapshot()),
                ),
            ):
                result = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", result.status)

            with TrackerDB(config.database_path) as db:
                feature = db.list_entities(
                    source="chromestatus", kind="feature", active_only=True
                )[0]["payload"]
                self.assertNotIn("source_updated", feature)
                self.assertFalse(
                    any(
                        event["category"]
                        == "chromestatus.feature_metadata_changed"
                        for event in db.list_events()
                    )
                )

    def test_open_patchset_then_merge_promotes_inferred_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_patch_details_enabled=False,
            )
            chrome_client = StaticClient(trial_snapshot())
            chromium_client = StaticClient(chromium_snapshot())
            MutableGerritClient.change = gerrit_change(
                status="NEW", revision="one", patchset=1, updated="first"
            )
            with (
                patch("ot_tracker.sync.ChromeStatusClient", return_value=chrome_client),
                patch("ot_tracker.sync.ChromiumSourceClient", return_value=chromium_client),
                patch("ot_tracker.sync.GerritClient", MutableGerritClient),
            ):
                baseline = run_sync(config)
                self.assertEqual("complete", baseline.status)
                self.assertEqual(0, baseline.events_created)

                MutableGerritClient.change = gerrit_change(
                    status="NEW", revision="two", patchset=2, updated="second"
                )
                updated = run_sync(config)
                self.assertEqual("complete", updated.status)

                with TrackerDB(config.database_path) as db:
                    categories = {event["category"] for event in db.list_events()}
                    self.assertIn("chromium.premerge_patchset_updated", categories)
                    candidate = db.list_entities(
                        source="tracker",
                        kind="implementation_path_candidate",
                        active_only=True,
                    )[0]["payload"]
                    self.assertFalse(candidate["auto_applied"])

                MutableGerritClient.change = gerrit_change(
                    status="MERGED", revision="two", patchset=2, updated="third"
                )
                merged = run_sync(config)
                self.assertEqual("complete", merged.status)

            with TrackerDB(config.database_path) as db:
                self.assertFalse(db.list_entities(
                    source="gerrit",
                    kind="premerge_ot_code_change",
                    active_only=True,
                ))
                candidate = db.list_entities(
                    source="tracker",
                    kind="implementation_path_candidate",
                    active_only=True,
                )[0]["payload"]
                self.assertTrue(candidate["auto_applied"])
                report = build_report_data(config, db)
                self.assertEqual(
                    1,
                    report["summary"]["active_trials_with_implementation_paths"],
                )
                self.assertEqual(1, report["summary"]["auto_applied_implementation_paths"])

    def test_open_change_cannot_demote_path_approved_by_merged_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_patch_details_enabled=False,
            )
            chrome_client = StaticClient(trial_snapshot())
            chromium_client = StaticClient(chromium_snapshot())
            MutableGerritClient.change = gerrit_change(
                status="MERGED",
                revision="merged",
                patchset=1,
                updated="first",
                number=123,
            )
            with (
                patch("ot_tracker.sync.ChromeStatusClient", return_value=chrome_client),
                patch("ot_tracker.sync.ChromiumSourceClient", return_value=chromium_client),
                patch("ot_tracker.sync.GerritClient", MutableGerritClient),
            ):
                baseline = run_sync(config)
                self.assertEqual("complete", baseline.status)

                MutableGerritClient.change = gerrit_change(
                    status="NEW",
                    revision="open",
                    patchset=1,
                    updated="second",
                    number=456,
                    subject="Refactor feature service",
                )
                updated = run_sync(config)
                self.assertEqual("complete", updated.status)

            with TrackerDB(config.database_path) as db:
                candidate = db.list_entities(
                    source="tracker",
                    kind="implementation_path_candidate",
                    active_only=True,
                )[0]["payload"]
                self.assertTrue(candidate["auto_applied"])
                self.assertEqual(123, candidate["change_number"])
                self.assertEqual(123, candidate["approval"]["change_number"])
                self.assertEqual(456, candidate["latest_evidence"]["change_number"])
                report = build_report_data(config, db)
                self.assertEqual(1, report["summary"]["auto_applied_implementation_paths"])

    def test_runtime_source_failure_preserves_previous_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_enabled=False,
            )
            chrome_client = StaticClient(trial_snapshot())
            good_chromium = StaticClient(chromium_snapshot())
            failed_chromium = StaticClient(
                ChromiumSnapshot(
                    revision="b" * 40,
                    files={
                        MANUAL_COMPLETION_PATH: "",
                        NAVIGATION_FEATURES_PATH: "",
                        PERSISTENT_TRIALS_PATH: "",
                    },
                    file_errors={RUNTIME_FEATURES_PATH: "temporary HTTP 503"},
                    declarations=[],
                    classifications={},
                )
            )
            with (
                patch("ot_tracker.sync.ChromeStatusClient", return_value=chrome_client),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=good_chromium,
                ),
            ):
                baseline = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", baseline.status)

            with (
                patch("ot_tracker.sync.ChromeStatusClient", return_value=chrome_client),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=failed_chromium,
                ),
            ):
                partial = run_sync(config, gerrit_enabled=False)
                self.assertEqual("partial", partial.status)

            with TrackerDB(config.database_path) as db:
                declarations = db.list_entities(
                    source="chromium",
                    kind="runtime_declaration",
                    active_only=True,
                )
                categories = {event["category"] for event in db.list_events()}
                self.assertEqual(1, len(declarations))
                self.assertNotIn("chromium.ot_declaration_removed", categories)
                chromium_stats = partial.stats["sources"]["chromium"]
                self.assertFalse(chromium_stats["declaration_snapshot_complete"])
                self.assertEqual(1, chromium_stats["runtime_declarations"])
                self.assertEqual(0, chromium_stats["fetched_runtime_declarations"])

    def test_active_runtime_gap_alerts_once_and_then_reports_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_enabled=False,
            )
            chrome_client = StaticClient(trial_snapshot())
            present = chromium_snapshot()
            absent = ChromiumSnapshot(
                revision="b" * 40,
                files={
                    RUNTIME_FEATURES_PATH: "",
                    MANUAL_COMPLETION_PATH: "",
                    NAVIGATION_FEATURES_PATH: "",
                    PERSISTENT_TRIALS_PATH: "",
                },
                file_errors={},
                declarations=[],
                classifications={},
            )
            with patch(
                "ot_tracker.sync.ChromeStatusClient", return_value=chrome_client
            ):
                with patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(present),
                ):
                    self.assertEqual(
                        "complete", run_sync(config, gerrit_enabled=False).status
                    )
                with patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(absent),
                ):
                    missing = run_sync(config, gerrit_enabled=False)
                    duplicate = run_sync(config, gerrit_enabled=False)
                    self.assertEqual("complete", missing.status)
                    self.assertEqual("complete", duplicate.status)
                with patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(present),
                ):
                    restored = run_sync(config, gerrit_enabled=False)
                    self.assertEqual("complete", restored.status)

            with TrackerDB(config.database_path) as db:
                gap_events = [
                    event
                    for event in db.list_events()
                    if event["category"].startswith(
                        "coverage.runtime_declaration_"
                    )
                ]
                self.assertEqual(
                    [
                        "coverage.runtime_declaration_restored",
                        "coverage.runtime_declaration_missing",
                    ],
                    [event["category"] for event in gap_events],
                )
                self.assertFalse(
                    db.list_entities(
                        source="tracker",
                        kind="runtime_declaration_gap",
                        active_only=True,
                    )
                )

    def test_partial_chromestatus_feed_preserves_previous_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_enabled=False,
            )
            full_snapshot = many_trial_snapshot(40)
            truncated_snapshot = ChromeStatusSnapshot(
                trials=full_snapshot.trials[:5],
                active_trials=full_snapshot.active_trials[:5],
                features={},
                feature_errors={},
            )
            chromium_client = StaticClient(chromium_snapshot())
            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(full_snapshot),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=chromium_client,
                ),
            ):
                baseline = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", baseline.status)

            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(truncated_snapshot),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=chromium_client,
                ),
            ):
                partial = run_sync(config, gerrit_enabled=False)
                self.assertEqual("partial", partial.status)

            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(full_snapshot),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=chromium_client,
                ),
            ):
                recovered = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", recovered.status)

            chrome_stats = partial.stats["sources"]["chromestatus"]
            self.assertFalse(chrome_stats["trial_snapshot_complete"])
            self.assertEqual(40, chrome_stats["total_trials"])
            self.assertEqual(5, chrome_stats["fetched_total_trials"])
            with TrackerDB(config.database_path) as db:
                trials = db.list_entities(
                    source="chromestatus",
                    kind="origin_trial",
                    active_only=True,
                )
                categories = {event["category"] for event in db.list_events()}
                self.assertEqual(40, len(trials))
                self.assertNotIn("official_ot.removed_from_feed", categories)
                self.assertIn("source.health_degraded", categories)
                self.assertIn("source.health_recovered", categories)
                self.assertFalse(
                    db.list_entities(
                        source="tracker",
                        kind="source_health_issue",
                        active_only=True,
                    )
                )

    def test_contract_mismatch_detection_and_resolution_are_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TrackerConfig(
                config_path=root / "config.toml",
                database_path=root / "tracker.sqlite3",
                reports_dir=root / "reports",
                gerrit_enabled=False,
            )
            matching_chromium = chromium_snapshot()
            mismatching_chromium = chromium_snapshot()
            mismatching_chromium.declarations[0]["fields"][
                "origin_trial_allows_third_party"
            ] = True
            chrome_without_third_party = trial_snapshot()
            chrome_with_third_party = trial_snapshot()
            chrome_with_third_party.trials[0]["allow_third_party_origins"] = True

            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(chrome_without_third_party),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(matching_chromium),
                ),
            ):
                baseline = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", baseline.status)

            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(chrome_without_third_party),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(mismatching_chromium),
                ),
            ):
                detected = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", detected.status)
                self.assertEqual(1, detected.stats["contracts"]["active_mismatches"])

            with (
                patch(
                    "ot_tracker.sync.ChromeStatusClient",
                    return_value=StaticClient(chrome_with_third_party),
                ),
                patch(
                    "ot_tracker.sync.ChromiumSourceClient",
                    return_value=StaticClient(mismatching_chromium),
                ),
            ):
                resolved = run_sync(config, gerrit_enabled=False)
                self.assertEqual("complete", resolved.status)
                self.assertEqual(0, resolved.stats["contracts"]["active_mismatches"])

            with TrackerDB(config.database_path) as db:
                events = db.list_events()
                contract_events = [
                    event
                    for event in events
                    if event["category"].startswith(
                        "coverage.cross_source_contract_mismatch_"
                    )
                ]
                self.assertEqual(
                    {
                        "coverage.cross_source_contract_mismatch_detected",
                        "coverage.cross_source_contract_mismatch_resolved",
                    },
                    {event["category"] for event in contract_events},
                )
                self.assertFalse(
                    db.list_entities(
                        source="tracker",
                        kind="cross_source_contract_mismatch",
                        active_only=True,
                    )
                )
                by_category = {
                    event["category"]: event for event in contract_events
                }
                detected_event = by_category[
                    "coverage.cross_source_contract_mismatch_detected"
                ]
                resolved_event = by_category[
                    "coverage.cross_source_contract_mismatch_resolved"
                ]
                self.assertFalse(detected_event["evidence"]["console"])
                self.assertTrue(detected_event["evidence"]["chromium"])
                self.assertTrue(resolved_event["evidence"]["console"])
                self.assertTrue(resolved_event["evidence"]["chromium"])


if __name__ == "__main__":
    unittest.main()
