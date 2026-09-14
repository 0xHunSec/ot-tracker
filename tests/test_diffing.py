import unittest

from ot_tracker.diffing import deep_diff, is_ot_code_diff, partition_diffs


class DiffingTest(unittest.TestCase):
    def test_keyed_stage_list_yields_field_path(self) -> None:
        old = {"stages": [{"id": "10", "desktop_last": 152, "name": "OT"}]}
        new = {"stages": [{"id": "10", "desktop_last": 154, "name": "OT"}]}
        differences = deep_diff(old, new)
        self.assertEqual("/stages/10/desktop_last", differences[0].path)
        milestones, statuses, metadata = partition_diffs(differences)
        self.assertEqual(1, len(milestones))
        self.assertFalse(statuses)
        self.assertFalse(metadata)

    def test_trial_status_is_separate_from_metadata(self) -> None:
        differences = deep_diff(
            {"status": "ACTIVE", "description": "old"},
            {"status": "COMPLETE", "description": "new"},
        )
        milestones, statuses, metadata = partition_diffs(differences)
        self.assertFalse(milestones)
        self.assertEqual(["/status"], [item.path for item in statuses])
        self.assertEqual(["/description"], [item.path for item in metadata])

    def test_ot_code_fields_are_identified(self) -> None:
        differences = deep_diff(
            {"stages": [{"id": "10", "ot_chromium_trial_name": "Old"}]},
            {"stages": [{"id": "10", "ot_chromium_trial_name": "New"}]},
        )
        self.assertTrue(is_ot_code_diff(differences[0]))


if __name__ == "__main__":
    unittest.main()
