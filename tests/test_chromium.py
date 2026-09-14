import base64
import unittest
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from ot_tracker.chromium import (
    MANUAL_COMPLETION_PATH,
    PERSISTENT_TRIALS_PATH,
    RUNTIME_FEATURES_PATH,
    ChromiumReleaseClient,
    ChromiumSnapshot,
    GerritClient,
    build_target_index,
    gerrit_ot_signal,
    infer_implementation_path_candidates,
    match_change_to_targets,
    parse_gerrit_file_diff_summary,
    parse_unified_diff_summary,
    parse_runtime_declarations,
    parse_special_classifications,
)
from ot_tracker.config import TargetRule, TrackerConfig, load_config


RUNTIME_FIXTURE = r'''
{
  parameters: {
    origin_trial_feature_name: { valid_type: "str" },
  },
  data: [
    {
      // The first real declaration.
      name: "ExampleRuntime",
      origin_trial_feature_name: "ExampleTrial",
      origin_trial_allows_third_party: true,
      origin_trial_os: ["win", "linux"],
      status: {"Win": "experimental", "default": ""},
    },
    {
      name: "SecondRuntime",
      origin_trial_feature_name: "ExampleTrial",
      depends_on: ["ExampleRuntime"],
    },
  ],
}
'''


class RuntimeParserTest(unittest.TestCase):
    def test_extracts_smallest_feature_objects(self) -> None:
        declarations = parse_runtime_declarations(RUNTIME_FIXTURE)
        self.assertEqual(2, len(declarations))
        first = declarations[0]
        self.assertEqual("ExampleRuntime", first["runtime_feature_name"])
        self.assertEqual("ExampleTrial", first["trial_name"])
        self.assertTrue(first["fields"]["origin_trial_allows_third_party"])
        self.assertEqual(["win", "linux"], first["fields"]["origin_trial_os"])
        self.assertNotIn("parameters", first["source_excerpt"])

    def test_special_production_entries_exclude_samples(self) -> None:
        classifications = parse_special_classifications(
            {
                MANUAL_COMPLETION_PATH: """
                  kOriginTrialsSampleAPIExpiryGracePeriod,
                  // Production grace period trials start here:
                  OriginTrialFeature::kRealFeature,
                """,
                PERSISTENT_TRIALS_PATH: """
                  "FrobulatePersistent",
                  // Production persistent origin trials follow below:
                  "RealPersistentTrial",
                """,
            }
        )
        self.assertNotIn("OriginTrialsSampleAPIExpiryGracePeriod", classifications)
        self.assertEqual({"expiry_grace_period"}, classifications["RealFeature"])
        self.assertEqual(
            {"persistent_to_next_response"}, classifications["RealPersistentTrial"]
        )


class ChromiumReleaseClientTest(unittest.TestCase):
    def test_fetches_exact_stable_and_beta_revisions(self) -> None:
        revisions = {"Stable": "a" * 40, "Beta": "b" * 40}

        class FakeHttp:
            def get_json(self, url, params=None):
                channel = params["channel"]
                return [
                    {
                        "channel": channel,
                        "milestone": 152 if channel == "Stable" else 153,
                        "version": (
                            "152.0.7977.64"
                            if channel == "Stable"
                            else "153.0.8010.12"
                        ),
                        "hashes": {"chromium": revisions[channel]},
                    }
                ]

        class FakeSourceClient:
            def __init__(self):
                self.calls = []

            def fetch_snapshot_at(self, revision, *, source_files=None):
                self.calls.append((revision, tuple(source_files or ())))
                return ChromiumSnapshot(
                    revision=revision,
                    files={RUNTIME_FEATURES_PATH: RUNTIME_FIXTURE},
                    file_errors={},
                    declarations=parse_runtime_declarations(RUNTIME_FIXTURE),
                    classifications={},
                )

        config = TrackerConfig(
            config_path=Path("config.toml"),
            database_path=Path("tracker.sqlite3"),
            reports_dir=Path("reports"),
            chromium_release_channels=("Stable", "Beta"),
        )
        source_client = FakeSourceClient()
        collection = ChromiumReleaseClient(
            config,
            FakeHttp(),
            source_client=source_client,
        ).fetch_snapshots()
        self.assertFalse(collection.errors)
        self.assertEqual({"Stable", "Beta"}, set(collection.snapshots))
        self.assertEqual(152, collection.snapshots["Stable"].milestone)
        self.assertEqual("b" * 40, collection.snapshots["Beta"].revision)
        self.assertEqual(
            {
                ("a" * 40, (RUNTIME_FEATURES_PATH,)),
                ("b" * 40, (RUNTIME_FEATURES_PATH,)),
            },
            set(source_client.calls),
        )


class GerritMatchingTest(unittest.TestCase):
    def test_alias_and_explicit_path_match(self) -> None:
        trials = [{"trial_name": "ExampleTrial"}]
        declarations = [
            {
                "trial_name": "ExampleTrial",
                "runtime_feature_name": "ExampleRuntime",
            }
        ]
        rules = {
            "ExampleTrial": TargetRule(
                "ExampleTrial", aliases=("Example API",), paths=("content/example/",)
            )
        }
        targets = build_target_index(trials, declarations, rules)
        matches = match_change_to_targets(
            {
                "subject": "Refactor unrelated wording",
                "commit_message": "No symbol in this message",
                "files": ["content/example/consumer.cc"],
            },
            targets,
        )
        self.assertIn("ExampleTrial", matches)
        self.assertEqual(
            ["content/example/consumer.cc"], matches["ExampleTrial"]["paths"]
        )

    def test_unmatched_wiring_change_is_high_signal(self) -> None:
        signal = gerrit_ot_signal(
            {
                "subject": "Add origin trial for NewThing",
                "commit_message": "Wire origin_trial_feature_name.",
                "files": [
                    "third_party/blink/renderer/platform/runtime_enabled_features.json5"
                ],
            }
        )
        self.assertIsNotNone(signal)
        self.assertGreaterEqual(signal["score"], 80)

    def test_wpt_only_import_does_not_match_trial_aliases(self) -> None:
        targets = build_target_index(
            [{"trial_name": "ExampleTrial"}],
            [],
            {},
        )
        matches = match_change_to_targets(
            {
                "subject": "Import wpt@abcdef",
                "commit_message": "Imported ExampleTrial expectations",
                "files": [
                    "third_party/blink/web_tests/external/wpt/example-trial/test.html",
                    "third_party/blink/web_tests/external/wpt/example-trial/test.js",
                ],
            },
            targets,
        )
        self.assertEqual({}, matches)

    def test_unified_diff_summary_includes_functions_and_line_counts(self) -> None:
        summary = parse_unified_diff_summary(
            """diff --git a/content/browser/example.cc b/content/browser/example.cc
index 111..222 100644
--- a/content/browser/example.cc
+++ b/content/browser/example.cc
@@ -10,2 +10,3 @@ void ExampleService::Start() {
-  Stop();
+  Restart();
+  Notify();
 }
diff --git a/content/browser/helper.h b/content/browser/helper.h
index 333..444 100644
--- a/content/browser/helper.h
+++ b/content/browser/helper.h
@@ -1 +1 @@ class ExampleHelper {
-  void OldMethod();
+  void NewMethod();
"""
        )
        self.assertEqual(2, summary["files_changed"])
        self.assertEqual(3, summary["insertions"])
        self.assertEqual(2, summary["deletions"])
        self.assertIn("void ExampleService::Start() {", summary["hunk_contexts"])
        self.assertIn("Restart", summary["changed_symbols"])

    def test_unified_diff_summary_extracts_only_added_ot_declarations(self) -> None:
        summary = parse_unified_diff_summary(
            f"""diff --git a/{RUNTIME_FEATURES_PATH} b/{RUNTIME_FEATURES_PATH}
index 111..222 100644
--- a/{RUNTIME_FEATURES_PATH}
+++ b/{RUNTIME_FEATURES_PATH}
@@ -10,2 +10,2 @@
-  origin_trial_feature_name: "RetiredTrial",
+  origin_trial_feature_name: "FutureTrial",
 """
        )
        self.assertEqual(
            ["FutureTrial"], summary["added_origin_trial_feature_names"]
        )

    def test_gerrit_file_diff_extracts_added_declaration_but_not_moved_line(self) -> None:
        summary = parse_gerrit_file_diff_summary(
            RUNTIME_FEATURES_PATH,
            {
                "content": [
                    {"ab": ["  name: \"ExampleRuntime\","]},
                    {"a": ["  origin_trial_feature_name: \"RetiredTrial\","]},
                    {"b": ["  origin_trial_feature_name: \"FutureTrial\","]},
                    {
                        "b": ["  origin_trial_feature_name: \"MovedTrial\","],
                        "due_to_move": True,
                    },
                ]
            },
        )
        self.assertEqual(2, summary["insertions"])
        self.assertEqual(1, summary["deletions"])
        self.assertEqual(
            ["FutureTrial"], summary["added_origin_trial_feature_names"]
        )

    def test_large_change_fetches_only_runtime_file_diff(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.json_urls = []

            def get_json(self, url):
                self.json_urls.append(url)
                return {
                    "content": [
                        {
                            "b": [
                                "  origin_trial_feature_name: \"LargeChangeTrial\","
                            ]
                        }
                    ]
                }

            def get_bytes(self, url):
                raise AssertionError("the full patch must not be downloaded")

        config = TrackerConfig(
            config_path=Path("config.toml"),
            database_path=Path("tracker.sqlite3"),
            reports_dir=Path("reports"),
        )
        http = FakeHttp()
        files = [RUNTIME_FEATURES_PATH, *[f"content/generated/{i}.cc" for i in range(81)]]
        change = {
            "change_number": 123,
            "revision": "deadbeef",
            "subject": "Mechanical generated-code update",
            "commit_message": "Refresh generated files.",
            "files": files,
            "file_stats": [
                {
                    "path": path,
                    "lines_inserted": 1,
                    "lines_deleted": 0,
                }
                for path in files
            ],
        }
        summary = GerritClient(config, http).fetch_patch_summary(change)
        self.assertEqual("targeted_file_diff", summary["detail"])
        self.assertTrue(summary["truncated"])
        self.assertEqual(82, summary["files_changed"])
        self.assertEqual(
            ["LargeChangeTrial"], summary["added_origin_trial_feature_names"]
        )
        self.assertEqual([RUNTIME_FEATURES_PATH], summary["targeted_files"])
        self.assertIn(
            urllib.parse.quote(RUNTIME_FEATURES_PATH, safe=""), http.json_urls[0]
        )
        signal = gerrit_ot_signal({**change, "diff_summary": summary})
        self.assertIsNotNone(signal)
        self.assertGreaterEqual(signal["score"], 90)

    def test_truncated_full_patch_falls_back_to_runtime_file_diff(self) -> None:
        class FakeHttp:
            def get_bytes(self, url):
                patch = (
                    "diff --git a/content/generated/a.cc b/content/generated/a.cc\n"
                    + "+generated line\n" * 20
                )
                return base64.b64encode(patch.encode("utf-8"))

            def get_json(self, url):
                return {
                    "content": [
                        {
                            "b": [
                                "  origin_trial_feature_name: \"TruncatedPatchTrial\","
                            ]
                        }
                    ]
                }

        config = TrackerConfig(
            config_path=Path("config.toml"),
            database_path=Path("tracker.sqlite3"),
            reports_dir=Path("reports"),
            gerrit_max_patch_bytes=80,
        )
        change = {
            "change_number": 456,
            "revision": "cafebabe",
            "files": [RUNTIME_FEATURES_PATH, "content/generated/a.cc"],
            "file_stats": [
                {
                    "path": RUNTIME_FEATURES_PATH,
                    "lines_inserted": 1,
                    "lines_deleted": 0,
                },
                {
                    "path": "content/generated/a.cc",
                    "lines_inserted": 20,
                    "lines_deleted": 0,
                },
            ],
        }
        summary = GerritClient(config, FakeHttp()).fetch_patch_summary(change)
        self.assertEqual("unified_diff_with_targeted_file", summary["detail"])
        self.assertTrue(summary["truncated"])
        self.assertEqual(
            ["TruncatedPatchTrial"], summary["added_origin_trial_feature_names"]
        )

    def test_patch_declaration_matches_known_target_without_ot_commit_text(self) -> None:
        targets = build_target_index([{"trial_name": "ExampleTrial"}], [], {})
        change = {
            "subject": "Enable the new API surface",
            "commit_message": "Update runtime configuration.",
            "files": [RUNTIME_FEATURES_PATH],
            "diff_summary": {
                "added_origin_trial_feature_names": ["ExampleTrial"],
            },
        }
        self.assertIn("ExampleTrial", match_change_to_targets(change, targets))

    def test_patch_declaration_is_strong_signal_without_ot_commit_text(self) -> None:
        signal = gerrit_ot_signal(
            {
                "subject": "Enable the new API surface",
                "commit_message": "Update runtime configuration.",
                "files": [RUNTIME_FEATURES_PATH],
                "diff_summary": {
                    "added_origin_trial_feature_names": ["FutureTrial"],
                },
            }
        )
        self.assertIsNotNone(signal)
        self.assertGreaterEqual(signal["score"], 90)
        self.assertEqual(["FutureTrial"], signal["declared_trial_names"])

    def test_path_inference_prefers_feature_directory_and_rejects_broad_parent(self) -> None:
        candidates = infer_implementation_path_candidates(
            {
                "files": [
                    "third_party/blink/renderer/modules/example_trial/service.cc",
                    "third_party/blink/renderer/modules/example_trial/service.h",
                    "third_party/blink/renderer/modules/example_trial/service_test.cc",
                    "third_party/blink/renderer/modules/example_trial/service_browsertest_base.cc",
                ]
            },
            ["ExampleTrial"],
        )
        self.assertEqual(
            "third_party/blink/renderer/modules/example_trial/",
            candidates[0]["path"],
        )
        self.assertGreaterEqual(candidates[0]["score"], 80)
        self.assertEqual(2, len(candidates[0]["files"]))

    def test_explicit_paths_do_not_match_test_only_files_in_large_change(self) -> None:
        targets = build_target_index(
            [{"trial_name": "LocalNetworkAccessTrial"}],
            [],
            {
                "LocalNetworkAccessTrial": TargetRule(
                    "LocalNetworkAccessTrial",
                    paths=("chrome/browser/local_network_access/",),
                )
            },
        )
        matches = match_change_to_targets(
            {
                "subject": "Remove obsolete includes",
                "commit_message": "Mechanical cleanup",
                "files": [
                    "chrome/browser/actor/actor_keyed_service.cc",
                    "chrome/browser/local_network_access/local_network_access_browsertest.cc",
                    "chrome/browser/local_network_access/local_network_access_browsertest_base.cc",
                    "chrome/browser/local_network_access/local_network_access_unittest.cc",
                ],
            },
            targets,
        )
        self.assertEqual({}, matches)

        broad = infer_implementation_path_candidates(
            {
                "files": [
                    "third_party/blink/renderer/core/dom/node.cc",
                    "third_party/blink/renderer/core/dom/document.cc",
                ]
            },
            ["ExampleTrial"],
        )
        self.assertNotIn(
            "third_party/blink/renderer/core/dom/",
            [candidate["path"] for candidate in broad],
        )
        self.assertTrue(all(candidate["score"] < 80 for candidate in broad))

    def test_gerrit_query_includes_open_changes_and_current_patchset(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.urls = []

            def get_json(self, url):
                self.urls.append(url)
                return [
                    {
                        "_number": 123,
                        "id": "chromium%2Fsrc~main~Iabc",
                        "change_id": "Iabc",
                        "status": "NEW",
                        "subject": "ExampleTrial implementation",
                        "created": "2026-09-01 00:00:00.000000000",
                        "updated": "2026-09-01 01:00:00.000000000",
                        "current_revision": "deadbeef",
                        "revisions": {
                            "deadbeef": {
                                "_number": 4,
                                "commit": {
                                    "message": "ExampleTrial implementation",
                                    "author": {"name": "Dev", "email": "dev@example.com"},
                                },
                                "files": {
                                    "content/browser/example.cc": {
                                        "lines_inserted": 7,
                                        "lines_deleted": 2,
                                    }
                                },
                            }
                        },
                    }
                ]

        config = TrackerConfig(
            config_path=Path("config.toml"),
            database_path=Path("tracker.sqlite3"),
            reports_dir=Path("reports"),
        )
        http = FakeHttp()
        changes = GerritClient(config, http).fetch_changes(
            datetime(2026, 9, 1, tzinfo=UTC)
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(http.urls[0]).query)["q"][0]
        self.assertNotIn("status:merged", query)
        self.assertEqual("NEW", changes[0]["status"])
        self.assertEqual(4, changes[0]["patchset"])
        self.assertEqual(7, changes[0]["file_stats"][0]["lines_inserted"])


class OperationalCoverageConfigTest(unittest.TestCase):
    def test_all_current_active_trials_have_explicit_paths(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config.toml"
        config = load_config(config_path)
        self.assertEqual(31, len(config.target_rules))
        self.assertFalse(
            [name for name, rule in config.target_rules.items() if not rule.paths]
        )
        self.assertEqual(("Stable", "Beta"), config.chromium_release_channels)
        self.assertEqual("Linux", config.chromium_release_platform)


if __name__ == "__main__":
    unittest.main()
