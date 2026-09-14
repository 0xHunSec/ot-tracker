import tempfile
import unittest
from pathlib import Path

from ot_tracker.db import TrackerDB


class DatabaseTest(unittest.TestCase):
    def test_versions_removals_and_event_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracker.sqlite3"
            with TrackerDB(path) as db:
                run_id = db.begin_run(baseline=True)
                db.begin_changes()
                first = db.upsert_entity(
                    run_id,
                    source="test",
                    kind="thing",
                    entity_key="one",
                    payload={"value": 1},
                )
                self.assertTrue(first.created)
                db.commit_changes()
                db.finish_run(run_id, status="complete", stats={})

                run_id = db.begin_run(baseline=False)
                db.begin_changes()
                second = db.upsert_entity(
                    run_id,
                    source="test",
                    kind="thing",
                    entity_key="one",
                    payload={"value": 2},
                )
                self.assertTrue(second.changed)
                created = db.record_event(
                    run_id,
                    category="thing.changed",
                    severity="medium",
                    source="test",
                    entity_kind="thing",
                    entity_key="one",
                    old=1,
                    new=2,
                )
                duplicate = db.record_event(
                    run_id,
                    category="thing.changed",
                    severity="medium",
                    source="test",
                    entity_kind="thing",
                    entity_key="one",
                    old=1,
                    new=2,
                )
                self.assertTrue(created)
                self.assertFalse(duplicate)
                missing = db.mark_missing(
                    source="test", kind="thing", present_keys=set()
                )
                self.assertEqual("one", missing[0]["entity_key"])
                db.commit_changes()
                db.finish_run(run_id, status="complete", stats={})
                self.assertFalse(
                    db.list_entities(source="test", kind="thing")[0]["active"]
                )

                third_run = db.begin_run(baseline=False)
                db.begin_changes()
                recurring = db.record_event(
                    third_run,
                    category="thing.changed",
                    severity="medium",
                    source="test",
                    entity_kind="thing",
                    entity_key="one",
                    old=1,
                    new=2,
                )
                self.assertTrue(recurring)
                db.commit_changes()
                db.finish_run(third_run, status="complete", stats={})

    def test_candidate_decision_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with TrackerDB(Path(directory) / "tracker.sqlite3") as db:
                db.set_candidate_decision("declaration:NewTrial", "watching", "check CL")
                decision = db.candidate_decisions()["declaration:NewTrial"]
                self.assertEqual("watching", decision["disposition"])
                self.assertEqual("check CL", decision["note"])

    def test_comparison_payload_ignores_evidence_only_movement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with TrackerDB(Path(directory) / "tracker.sqlite3") as db:
                first_run = db.begin_run(baseline=True)
                db.begin_changes()
                first = db.upsert_entity(
                    first_run,
                    source="chromium",
                    kind="runtime_declaration",
                    entity_key="Example",
                    payload={"field": "same", "source_line": 10},
                    comparison_payload={"field": "same"},
                )
                db.commit_changes()
                db.finish_run(first_run, status="complete", stats={})
                self.assertTrue(first.changed)

                second_run = db.begin_run(baseline=False)
                db.begin_changes()
                second = db.upsert_entity(
                    second_run,
                    source="chromium",
                    kind="runtime_declaration",
                    entity_key="Example",
                    payload={"field": "same", "source_line": 99},
                    comparison_payload={"field": "same"},
                )
                db.commit_changes()
                db.finish_run(second_run, status="complete", stats={})
                self.assertFalse(second.changed)
                entity = db.list_entities(
                    source="chromium", kind="runtime_declaration"
                )[0]
                self.assertEqual(99, entity["payload"]["source_line"])

    def test_event_severity_can_be_reclassified_by_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with TrackerDB(Path(directory) / "tracker.sqlite3") as db:
                run_id = db.begin_run(baseline=False)
                db.begin_changes()
                db.record_event(
                    run_id,
                    category="chromium.premerge_change_detected",
                    severity="medium",
                    source="gerrit",
                    entity_kind="premerge_ot_code_change",
                    entity_key="123:ExampleTrial",
                )
                db.record_event(
                    run_id,
                    category="official_ot.registered",
                    severity="high",
                    source="chromestatus",
                    entity_kind="origin_trial",
                    entity_key="1",
                )
                db.commit_changes()
                db.finish_run(run_id, status="complete", stats={})

                changed = db.set_event_severity(
                    categories={"chromium.premerge_change_detected"},
                    severity="low",
                )
                self.assertEqual(1, changed)
                events = {
                    event["category"]: event["severity"]
                    for event in db.list_events()
                }
                self.assertEqual("low", events["chromium.premerge_change_detected"])
                self.assertEqual("high", events["official_ot.registered"])


if __name__ == "__main__":
    unittest.main()
