import unittest

from ot_tracker.chromestatus import canonical_feature, canonical_trial, feature_id_from_url


class ChromeStatusCanonicalizationTest(unittest.TestCase):
    def test_trial_normalizes_milestones_and_extensions(self) -> None:
        trial = canonical_trial(
            {
                "id": "-123",
                "display_name": "Example",
                "origin_trial_feature_name": "ExampleTrial",
                "status": "ACTIVE",
                "enabled": True,
                "type": "ORIGIN_TRIAL",
                "start_milestone": "150",
                "end_milestone": "154",
                "original_end_milestone": "153",
                "chromestatus_url": "https://chromestatus.com/feature/123456",
                "trial_extensions": [
                    {
                        "endMilestone": "154",
                        "endTime": "2026-10-01T00:00:00Z",
                        "extensionIntentUrl": "https://example.test/intent",
                    }
                ],
            }
        )
        self.assertEqual(150, trial["start_milestone"])
        self.assertEqual("123456", trial["chromestatus_feature_id"])
        self.assertEqual(154, trial["trial_extensions"][0]["end_milestone"])

    def test_feature_keeps_only_ot_and_shipping_stages(self) -> None:
        feature = canonical_feature(
            {
                "id": 42,
                "name": "Example",
                "updated": {"by": "owner@example.test", "when": "2026-09-01"},
                "blink_components": ["Blink>Example"],
                "browsers": {"chrome": {"owners": ["owner@example.test"]}},
                "stages": [
                    {"id": 1, "stage_type": 120},
                    {
                        "id": 2,
                        "stage_type": 150,
                        "desktop_first": 150,
                        "desktop_last": 154,
                        "ot_chromium_trial_name": "ExampleTrial",
                    },
                    {"id": 3, "stage_type": 160, "desktop_first": 155},
                ],
            }
        )
        self.assertEqual([150, 160], [stage["stage_type"] for stage in feature["stages"]])
        self.assertEqual(150, feature["stages"][0]["desktop_first"])
        self.assertNotIn("source_updated", feature)

    def test_feature_id_rejects_non_feature_url(self) -> None:
        self.assertIsNone(feature_id_from_url("https://example.test/123"))


if __name__ == "__main__":
    unittest.main()
