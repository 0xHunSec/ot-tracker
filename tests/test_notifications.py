import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ot_tracker.config import DiscordConfig, TrackerConfig
from ot_tracker.db import TrackerDB
from ot_tracker.notifications import (
    DISCORD_CHANNEL,
    DiscordWebhookError,
    build_discord_event_payload,
    build_discord_heartbeat_payload,
    build_discord_implementation_digest_payload,
    deliver_discord_pending,
    discord_configuration_status,
    execute_discord_bot,
    execute_discord_webhook,
    maybe_send_discord_heartbeat,
    validate_discord_webhook_url,
)


WEBHOOK = "https://discord.com/api/webhooks/123456789/secret-token_value"


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return b"{}"


def make_config(directory: str, **discord_overrides) -> TrackerConfig:
    defaults = {
        "enabled": True,
        "webhook_env": "OT_TRACKER_DISCORD_WEBHOOK_URL",
        "username": "Chrome OT Tracker",
        "min_severity": "medium",
        "batch_size": 8,
        "max_batches_per_run": 10,
        "retries": 3,
    }
    defaults.update(discord_overrides)
    root = Path(directory)
    return TrackerConfig(
        config_path=root / "config.toml",
        database_path=root / "tracker.sqlite3",
        reports_dir=root / "reports",
        discord=DiscordConfig(**defaults),
    )


def record_event(
    db: TrackerDB,
    *,
    baseline: bool,
    severity: str,
    key: str,
    category: str = "official_ot.registered",
) -> int:
    run_id = db.begin_run(baseline=baseline)
    db.begin_changes()
    created = db.record_event(
        run_id,
        category=category,
        severity=severity,
        source="chromestatus",
        entity_kind="origin_trial",
        entity_key=key,
        new={"display_name": f"Trial {key}", "trial_name": f"Trial{key}"},
        evidence={"chromestatus_url": f"https://chromestatus.com/feature/{key}"},
    )
    assert created
    db.commit_changes()
    db.finish_run(run_id, status="complete", stats={})
    return int(db.list_events(limit=1)[0]["id"])


def record_implementation_event(
    db: TrackerDB,
    *,
    trial_name: str,
    change_number: int,
    observed_at: str,
) -> int:
    run_id = db.begin_run(baseline=False)
    db.begin_changes()
    change_url = (
        "https://chromium-review.googlesource.com/c/chromium/src/+/"
        f"{change_number}"
    )
    change = {
        "change_number": change_number,
        "subject": f"Update {trial_name} implementation",
        "url": change_url,
        "diff_summary": {
            "files_changed": 3,
            "insertions": 12,
            "deletions": 4,
        },
    }
    created = db.record_event(
        run_id,
        category="chromium.implementation_changed",
        severity="medium",
        source="gerrit",
        entity_kind="ot_code_change",
        entity_key=f"{change_number}:{trial_name}",
        new={"trial_name": trial_name, "change": change},
        evidence={"change_url": change_url, "diff_summary": change["diff_summary"]},
        observed_at=observed_at,
    )
    assert created
    db.commit_changes()
    db.finish_run(run_id, status="complete", stats={})
    return int(db.list_events(limit=1)[0]["id"])


class DiscordNotificationTest(unittest.TestCase):
    def test_webhook_url_validation_is_strict_and_secret_safe(self) -> None:
        self.assertEqual(WEBHOOK, validate_discord_webhook_url(WEBHOOK))
        self.assertEqual(
            "https://canary.discord.com/api/v10/webhooks/123/token?thread_id=456",
            validate_discord_webhook_url(
                "https://canary.discord.com/api/v10/webhooks/123/token?thread_id=456"
            ),
        )
        for invalid in (
            "http://discord.com/api/webhooks/123/token",
            "https://discord.example/api/webhooks/123/token",
            "https://discord.com/not-a-webhook/123/token",
            "https://discord.com:8443/api/webhooks/123/token",
        ):
            with self.assertRaises(DiscordWebhookError) as raised:
                validate_discord_webhook_url(invalid)
            self.assertNotIn("token", str(raised.exception))

    def test_payload_disables_mentions_and_summarizes_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            payload = build_discord_event_payload(
                config,
                [
                    {
                        "id": 7,
                        "run_id": 2,
                        "observed_at": "2026-08-31T00:00:00+00:00",
                        "category": "official_ot.milestone_changed",
                        "severity": "high",
                        "source": "chromestatus",
                        "entity_key": "123",
                        "new": None,
                        "old": None,
                        "field_path": None,
                        "evidence": {
                            "display_name": "Example OT",
                            "trial_name": "ExampleTrial",
                            "chromestatus_url": "https://chromestatus.com/feature/123",
                            "changes": [
                                {
                                    "path": "/end_milestone",
                                    "old": 140,
                                    "new": 142,
                                }
                            ]
                        },
                    }
                ],
            )
        self.assertEqual({"parse": []}, payload["allowed_mentions"])
        embed = payload["embeds"][0]
        self.assertEqual(0xE74C3C, embed["color"])
        self.assertIn("마일스톤", embed["fields"][0]["name"])
        self.assertIn("**Example OT**", embed["fields"][0]["value"])
        self.assertIn("140 → 142", embed["fields"][0]["value"])
        self.assertIn(
            "[근거 보기](https://chromestatus.com/feature/123)",
            embed["fields"][0]["value"],
        )
        self.assertIn("2026-08-31 09:00 KST", embed["fields"][0]["value"])
        self.assertNotIn("2026-08-31T00:00:00+00:00", embed["fields"][0]["value"])

    def test_payload_description_only_names_changes_in_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            payload = build_discord_event_payload(
                config,
                [
                    {
                        "id": 166,
                        "category": "chromestatus.ot_stage_milestone_changed",
                        "severity": "high",
                        "source": "chromestatus",
                        "evidence": {"display_name": "Email Verification Protocol"},
                    },
                    {
                        "id": 167,
                        "category": "chromestatus.feature_metadata_changed",
                        "severity": "medium",
                        "source": "chromestatus",
                        "evidence": {"display_name": "Email Verification Protocol"},
                    },
                ],
            )
        description = payload["embeds"][0]["description"]
        self.assertEqual(
            "Chrome Status에서 다음 변화를 감지했습니다: "
            "OT stage 마일스톤 변경 · 기본정보 변경",
            description,
        )
        self.assertNotIn("신규", description)

    def test_payload_groups_equivalent_release_contract_changes(self) -> None:
        common_evidence = {
            "channel": "Beta",
            "platform": "Linux",
            "milestone": 154,
            "version": "154.0.8037.0",
            "revision": "a" * 40,
            "trial_name": "EmailVerificationProtocol",
            "url": "https://chromiumdash.appspot.com/releases",
        }
        matching_changes = [
            {
                "path": "/fields/origin_trial_os",
                "old": ["win", "linux"],
                "new": ["win", "linux", "android"],
            },
            {"path": "/source_line", "old": 2625, "new": 2596},
            {
                "path": "/source_excerpt",
                "old": {"sha256": "old", "length": 483},
                "new": {"sha256": "new", "length": 345},
            },
        ]
        events = [
            {
                "id": 328,
                "observed_at": "2026-09-02T17:00:00+00:00",
                "category": "chromium.release_ot_code_changed",
                "severity": "high",
                "source": "chromium_release",
                "entity_key": "Beta:EmailVerificationProtocol",
                "evidence": {**common_evidence, "changes": matching_changes},
            },
            {
                "id": 329,
                "observed_at": "2026-09-02T17:00:00+00:00",
                "category": "chromium.release_ot_code_changed",
                "severity": "high",
                "source": "chromium_release",
                "entity_key": "Beta:EmailVerificationStatusIndicator",
                "evidence": {
                    **common_evidence,
                    "changes": [
                        matching_changes[0],
                        {"path": "/source_line", "old": 2641, "new": 2606},
                        {
                            "path": "/source_excerpt",
                            "old": {"sha256": "other-old", "length": 508},
                            "new": {"sha256": "other-new", "length": 340},
                        },
                    ],
                },
            },
            {
                "id": 331,
                "observed_at": "2026-09-02T17:00:00+00:00",
                "category": "chromium.release_ot_code_changed",
                "severity": "high",
                "source": "chromium_release",
                "entity_key": "Beta:EmailVerificationDifferentContract",
                "evidence": {
                    **common_evidence,
                    "changes": [
                        {
                            "path": "/fields/status",
                            "old": "experimental",
                            "new": "stable",
                        }
                    ],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            payload = build_discord_event_payload(make_config(directory), events)

        embed = payload["embeds"][0]
        self.assertEqual("Chrome Origin Trial 변경 3건", embed["title"])
        self.assertEqual(2, len(embed["fields"]))
        grouped_value = embed["fields"][0]["value"]
        self.assertIn("events #328–#329", grouped_value)
        self.assertIn("Runtime feature 2개", grouped_value)
        self.assertIn("`EmailVerificationProtocol`", grouped_value)
        self.assertIn("`EmailVerificationStatusIndicator`", grouped_value)
        self.assertEqual(1, grouped_value.count("/fields/origin_trial_os"))
        self.assertNotIn("source_line", grouped_value)
        self.assertNotIn("source_excerpt", grouped_value)
        self.assertIn("/fields/status", embed["fields"][1]["value"])

    def test_implementation_digest_stays_within_discord_embed_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, implementation_digest_hours=6)
            groups = []
            for group_number in range(6):
                events = []
                for change_number in range(8):
                    number = group_number * 100 + change_number
                    url = (
                        "https://chromium-review.googlesource.com/"
                        f"c/chromium/src/+/{number}"
                    )
                    events.append(
                        {
                            "id": number + 1,
                            "observed_at": "2026-09-01T20:00:00+00:00",
                            "category": "chromium.implementation_changed",
                            "severity": "medium",
                            "source": "gerrit",
                            "entity_key": f"{number}:Trial{group_number}",
                            "new": {
                                "trial_name": f"Trial{group_number}",
                                "change": {
                                    "change_number": number,
                                    "subject": "Long implementation subject " * 8,
                                    "url": url,
                                    "diff_summary": {
                                        "files_changed": 20,
                                        "insertions": 200,
                                        "deletions": 100,
                                    },
                                },
                            },
                            "evidence": {"change_url": url},
                        }
                    )
                groups.append(events)

            payload = build_discord_implementation_digest_payload(config, groups)

        embed = payload["embeds"][0]
        character_count = (
            len(embed["title"])
            + len(embed["description"])
            + len(embed["footer"]["text"])
            + sum(
                len(field["name"]) + len(field["value"])
                for field in embed["fields"]
            )
        )
        self.assertLessEqual(character_count, 6000)
        self.assertTrue(
            all(len(field["value"]) <= 1024 for field in embed["fields"])
        )

    def test_payload_summarizes_patch_files_and_function_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            payload = build_discord_event_payload(
                config,
                [
                    {
                        "id": 8,
                        "run_id": 3,
                        "observed_at": "2026-09-01T00:00:00+00:00",
                        "category": "chromium.premerge_patchset_updated",
                        "severity": "medium",
                        "source": "gerrit",
                        "entity_key": "123:ExampleTrial",
                        "new": {"trial_name": "ExampleTrial"},
                        "old": None,
                        "field_path": None,
                        "evidence": {
                            "old_patchset": 2,
                            "new_patchset": 3,
                            "change_url": "https://chromium-review.googlesource.com/c/chromium/src/+/123",
                            "diff_summary": {
                                "files_changed": 2,
                                "insertions": 14,
                                "deletions": 3,
                                "top_files": [
                                    {"path": "content/browser/example/service.cc"}
                                ],
                                "changed_symbols": ["ExampleService::Start"],
                                "hunk_contexts": [],
                                "added_origin_trial_feature_names": ["FutureTrial"],
                                "targeted_files": [
                                    "third_party/blink/renderer/platform/"
                                    "runtime_enabled_features.json5"
                                ],
                            },
                        },
                    }
                ],
            )
        value = payload["embeds"][0]["fields"][0]["value"]
        self.assertIn("patchset `2` → `3`", value)
        self.assertIn("파일 2개 · +14/-3", value)
        self.assertIn("ExampleService::Start", value)
        self.assertIn("추가 OT 선언: `FutureTrial`", value)
        self.assertIn(
            "대형 CL 정밀 검사: `runtime_enabled_features.json5`", value
        )

    def test_payload_prefers_trial_matched_files_over_unrelated_cl_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            payload = build_discord_event_payload(
                config,
                [
                    {
                        "id": 9,
                        "run_id": 4,
                        "observed_at": "2026-09-01T00:00:00+00:00",
                        "category": "chromium.implementation_changed",
                        "severity": "medium",
                        "source": "gerrit",
                        "entity_key": "123:ExampleTrial",
                        "new": {"trial_name": "ExampleTrial"},
                        "old": None,
                        "field_path": None,
                        "evidence": {
                            "matched_paths": [
                                "third_party/blink/renderer/modules/example/service.cc"
                            ],
                            "diff_summary": {
                                "files_changed": 12,
                                "insertions": 58,
                                "deletions": 60,
                                "top_files": [
                                    {"path": "unrelated/generated/top_file.cc"}
                                ],
                                "changed_symbols": ["UnrelatedFunction"],
                            },
                        },
                    }
                ],
            )
        value = payload["embeds"][0]["fields"][0]["value"]
        self.assertIn("매칭 파일:", value)
        self.assertIn("modules/example/service.cc", value)
        self.assertNotIn("주요 파일:", value)
        self.assertNotIn("UnrelatedFunction", value)

    def test_payload_summarizes_ot_stage_extension_without_raw_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            payload = build_discord_event_payload(
                config,
                [
                    {
                        "id": 10,
                        "run_id": 5,
                        "observed_at": "2026-09-01T00:00:00+00:00",
                        "category": "chromestatus.ot_stage_milestone_changed",
                        "severity": "high",
                        "source": "chromestatus",
                        "entity_key": "42",
                        "new": None,
                        "old": None,
                        "field_path": None,
                        "evidence": {
                            "feature_name": "Example OT",
                            "changes": [
                                {
                                    "path": "/stages/7/extensions",
                                    "old": [],
                                    "new": [
                                        {
                                            "id": 8,
                                            "desktop_last": 154,
                                            "experiment_extension_reason": (
                                                "Consumers need more migration time."
                                            ),
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ],
            )
        value = payload["embeds"][0]["fields"][0]["value"]
        self.assertIn("OT 연장: `없음` → `desktop 종료 M154`", value)
        self.assertIn("연장 사유: Consumers need more migration time.", value)
        self.assertNotIn('"desktop_last"', value)

    def test_payload_explains_cross_source_contract_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            payload = build_discord_event_payload(
                config,
                [
                    {
                        "id": 9,
                        "run_id": 4,
                        "observed_at": "2026-09-01T00:00:00+00:00",
                        "category": (
                            "coverage.cross_source_contract_mismatch_detected"
                        ),
                        "severity": "high",
                        "source": "tracker",
                        "entity_key": (
                            "AIPromptAPIParams:allow_third_party_origins"
                        ),
                        "new": None,
                        "old": None,
                        "field_path": None,
                        "evidence": {
                            "display_name": "Prompt API Sampling Parameters",
                            "trial_name": "AIPromptAPIParams",
                            "chromestatus_url": (
                                "https://chromestatus.com/feature/6325545693478912"
                            ),
                            "field": "allow_third_party_origins",
                            "console": False,
                            "chromium": True,
                            "runtime_features": ["AIPromptAPIParams"],
                        },
                    }
                ],
            )

        field = payload["embeds"][0]["fields"][0]
        self.assertIn("계약 불일치", field["name"])
        self.assertIn("Chrome Status `False` ≠ Chromium `True`", field["value"])
        self.assertIn("Runtime feature: `AIPromptAPIParams`", field["value"])
        self.assertIn(
            "[근거 보기](https://chromestatus.com/feature/6325545693478912)",
            field["value"],
        )

    def test_payload_explains_source_degradation_and_preserved_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            payload = build_discord_event_payload(
                config,
                [
                    {
                        "id": 10,
                        "run_id": 5,
                        "observed_at": "2026-09-01T00:00:00+00:00",
                        "category": "source.health_degraded",
                        "severity": "high",
                        "source": "tracker",
                        "entity_key": "chromestatus.inventory",
                        "new": None,
                        "old": None,
                        "field_path": None,
                        "evidence": {
                            "source_name": "Chrome Status OT inventory",
                            "error": "origin trial count dropped implausibly",
                            "fetched_total": 5,
                            "preserved_total": 318,
                            "url": "https://chromestatus.com/origintrials",
                        },
                    }
                ],
            )
        field = payload["embeds"][0]["fields"][0]
        self.assertIn("추적 수집원 이상", field["name"])
        self.assertIn("**Chrome Status OT inventory**", field["value"])
        self.assertIn("원인: `origin trial count dropped", field["value"])
        self.assertIn("수집 `5` / 기존 보존 `318`", field["value"])

    def test_daily_heartbeat_is_sent_once_per_interval(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeResponse()

        now = datetime(2026, 9, 1, 1, 2, 3, tzinfo=UTC)
        stats = {
            "sources": {
                "chromestatus": {"active_trials": 31, "total_trials": 318},
                "chromium": {"revision": "abcdef1234567890"},
                "gerrit": {"changes_scanned": 12},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with TrackerDB(config.database_path) as db:
                with patch.dict(
                    os.environ,
                    {config.discord.webhook_env: WEBHOOK},
                    clear=True,
                ):
                    sent = maybe_send_discord_heartbeat(
                        config,
                        db,
                        run_id=9,
                        run_status="complete",
                        stats=stats,
                        opener=opener,
                        sleep=lambda _: None,
                        now=now,
                    )
                    not_due = maybe_send_discord_heartbeat(
                        config,
                        db,
                        run_id=10,
                        run_status="complete",
                        stats=stats,
                        opener=opener,
                        sleep=lambda _: None,
                        now=now + timedelta(hours=1),
                    )
                self.assertEqual(now.isoformat(timespec="seconds"), db.get_meta(
                    "notification.discord.heartbeat_sent_at"
                ))

        self.assertEqual("sent", sent["status"])
        self.assertEqual("not_due", not_due["status"])
        self.assertEqual(1, len(requests))
        body = json.loads(requests[0].data.decode("utf-8"))
        self.assertEqual(
            "💚 Chrome OT Tracker 정상 작동 중",
            body["embeds"][0]["title"],
        )
        self.assertEqual(
            build_discord_heartbeat_payload(config, run_id=9, stats=stats)[
                "allowed_mentions"
            ],
            {"parse": []},
        )

    def test_queue_excludes_baseline_and_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with TrackerDB(config.database_path) as db:
                record_event(db, baseline=True, severity="high", key="baseline")
                record_event(db, baseline=False, severity="low", key="low")
                medium_id = record_event(
                    db, baseline=False, severity="medium", key="medium"
                )
                high_id = record_event(db, baseline=False, severity="high", key="high")

                pending = db.pending_notification_events(
                    channel=DISCORD_CHANNEL, min_severity="medium", limit=10
                )
                self.assertEqual([medium_id, high_id], [row["id"] for row in pending])
                db.record_notification_delivery(
                    channel=DISCORD_CHANNEL,
                    event_ids=[medium_id],
                    status="sent",
                )
                db.record_notification_delivery(
                    channel=DISCORD_CHANNEL,
                    event_ids=[high_id],
                    status="failed",
                    error="temporary",
                )
                summary = db.notification_summary(
                    channel=DISCORD_CHANNEL, min_severity="medium"
                )
                self.assertEqual(
                    {
                        "eligible": 2,
                        "sent": 1,
                        "skipped": 0,
                        "failed": 1,
                        "pending": 1,
                        "attempts": 2,
                    },
                    summary,
                )

    def test_skipped_event_is_terminal_without_delivery_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with TrackerDB(config.database_path) as db:
                event_id = record_event(
                    db, baseline=False, severity="high", key="historical"
                )
                db.record_notification_delivery(
                    channel=DISCORD_CHANNEL,
                    event_ids=[event_id],
                    status="skipped",
                    error="pre-connection backlog",
                )
                summary = db.notification_summary(
                    channel=DISCORD_CHANNEL, min_severity="medium"
                )
                self.assertEqual(1, summary["skipped"])
                self.assertEqual(0, summary["pending"])
                self.assertEqual(0, summary["attempts"])

    def test_missing_webhook_preserves_pending_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with TrackerDB(config.database_path) as db:
                record_event(db, baseline=False, severity="high", key="pending")
                with patch.dict(os.environ, {}, clear=True):
                    result = deliver_discord_pending(config, db)
                self.assertEqual("not_configured", result.status)
                self.assertEqual(1, result.pending_after)
                self.assertEqual(
                    1,
                    db.notification_summary(
                        channel=DISCORD_CHANNEL, min_severity="medium"
                    )["pending"],
                )

    def test_implementation_changes_wait_for_kst_digest_window(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, implementation_digest_hours=6)
            with TrackerDB(config.database_path) as db:
                first_id = record_implementation_event(
                    db,
                    trial_name="HTMLInCanvas",
                    change_number=101,
                    observed_at="2026-09-01T17:28:00+00:00",
                )
                second_id = record_implementation_event(
                    db,
                    trial_name="HTMLInCanvas",
                    change_number=102,
                    observed_at="2026-09-01T20:00:00+00:00",
                )
                with patch.dict(
                    os.environ,
                    {config.discord.webhook_env: WEBHOOK},
                    clear=True,
                ):
                    waiting = deliver_discord_pending(
                        config,
                        db,
                        opener=opener,
                        sleep=lambda _: None,
                        now=datetime(2026, 9, 1, 20, 30, tzinfo=UTC),
                    )
                    delivered = deliver_discord_pending(
                        config,
                        db,
                        opener=opener,
                        sleep=lambda _: None,
                        now=datetime(2026, 9, 1, 21, 1, tzinfo=UTC),
                    )

        self.assertEqual("deferred", waiting.status)
        self.assertEqual(0, waiting.sent)
        self.assertEqual(2, waiting.pending_after)
        self.assertEqual("sent", delivered.status)
        self.assertEqual(2, delivered.sent)
        self.assertEqual(0, delivered.pending_after)
        self.assertEqual(1, len(requests))
        body = json.loads(requests[0][0].data.decode("utf-8"))
        embed = body["embeds"][0]
        self.assertEqual(
            "Chromium OT 구현 변경 요약 · 1개 OT / 2건",
            embed["title"],
        )
        self.assertIn("KST 기준 6시간 단위", embed["description"])
        self.assertIn("HTMLInCanvas · 병합 CL 2건", embed["fields"][0]["name"])
        self.assertIn("[CL 101]", embed["fields"][0]["value"])
        self.assertIn("[CL 102]", embed["fields"][0]["value"])
        self.assertIn(f"event #{first_id}–#{second_id}", embed["fields"][0]["value"])

    def test_digest_wait_does_not_delay_important_events(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, implementation_digest_hours=6)
            with TrackerDB(config.database_path) as db:
                record_implementation_event(
                    db,
                    trial_name="HTMLInCanvas",
                    change_number=201,
                    observed_at="2026-09-01T20:00:00+00:00",
                )
                record_event(
                    db,
                    baseline=False,
                    severity="high",
                    key="new-official-ot",
                )
                with patch.dict(
                    os.environ,
                    {config.discord.webhook_env: WEBHOOK},
                    clear=True,
                ):
                    result = deliver_discord_pending(
                        config,
                        db,
                        opener=opener,
                        sleep=lambda _: None,
                        now=datetime(2026, 9, 1, 20, 30, tzinfo=UTC),
                    )

                summary = db.notification_summary(
                    channel=DISCORD_CHANNEL,
                    min_severity="medium",
                )

        self.assertEqual("deferred", result.status)
        self.assertEqual(1, result.sent)
        self.assertEqual(1, result.pending_after)
        self.assertEqual(1, summary["sent"])
        self.assertEqual(1, summary["pending"])
        self.assertEqual(1, len(requests))
        body = json.loads(requests[0][0].data.decode("utf-8"))
        self.assertEqual("Chrome Origin Trial 변경 1건", body["embeds"][0]["title"])
        self.assertIn("신규 공개 OT 등록", body["embeds"][0]["description"])

    def test_successful_delivery_marks_event_sent(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with TrackerDB(config.database_path) as db:
                record_event(db, baseline=False, severity="high", key="new")
                with patch.dict(
                    os.environ,
                    {config.discord.webhook_env: WEBHOOK},
                    clear=True,
                ):
                    result = deliver_discord_pending(
                        config, db, opener=opener, sleep=lambda _: None
                    )
                self.assertEqual("sent", result.status)
                self.assertEqual(1, result.sent)
                self.assertEqual(0, result.pending_after)
                summary = db.notification_summary(
                    channel=DISCORD_CHANNEL, min_severity="medium"
                )
                self.assertEqual(1, summary["sent"])

        self.assertEqual(1, len(requests))
        request, timeout = requests[0]
        self.assertIn("wait=true", request.full_url)
        self.assertGreater(timeout, 0)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual({"parse": []}, body["allowed_mentions"])

    def test_rate_limit_retries_and_http_errors_hide_webhook_token(self) -> None:
        calls = []
        responses = [
            urllib.error.HTTPError(
                WEBHOOK,
                429,
                "rate limited",
                {"Retry-After": "0.25"},
                io.BytesIO(b'{"retry_after":0.25}'),
            ),
            FakeResponse(),
        ]

        def retrying_opener(request, timeout):
            calls.append(request)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        sleeps = []
        execute_discord_webhook(
            WEBHOOK,
            {"content": "test"},
            timeout_seconds=1,
            retries=2,
            opener=retrying_opener,
            sleep=sleeps.append,
        )
        self.assertEqual(2, len(calls))
        self.assertEqual([0.25], sleeps)

        def failing_opener(request, timeout):
            raise urllib.error.HTTPError(
                WEBHOOK, 404, "not found", {}, io.BytesIO(b"unknown webhook")
            )

        with self.assertRaises(DiscordWebhookError) as raised:
            execute_discord_webhook(
                WEBHOOK,
                {"content": "test"},
                timeout_seconds=1,
                retries=1,
                opener=failing_opener,
                sleep=lambda _: None,
            )
        self.assertNotIn("secret-token_value", str(raised.exception))
        self.assertIn("HTTP 404", str(raised.exception))

    def test_bot_transport_reads_chmod_600_token_file(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "bot-token"
            token_file.write_text("dummy.bot.token_value_that_is_long_enough\n")
            token_file.chmod(0o600)
            config = make_config(
                directory,
                transport="bot",
                bot_token_file=token_file,
                channel_id="123456789012345678",
            )
            with patch.dict(os.environ, {}, clear=True):
                status = discord_configuration_status(config)
                self.assertTrue(status["valid"])
                self.assertEqual("file", status["credential_source"])

                with TrackerDB(config.database_path) as db:
                    record_event(db, baseline=False, severity="high", key="bot")
                    result = deliver_discord_pending(
                        config, db, opener=opener, sleep=lambda _: None
                    )
                    self.assertEqual("sent", result.status)

        self.assertEqual(1, len(requests))
        request = requests[0]
        self.assertEqual(
            "https://discord.com/api/v10/channels/123456789012345678/messages",
            request.full_url,
        )
        self.assertTrue(request.headers["Authorization"].startswith("Bot "))
        body = json.loads(request.data.decode("utf-8"))
        self.assertNotIn("username", body)
        self.assertEqual({"parse": []}, body["allowed_mentions"])

    def test_bot_executor_rejects_invalid_channel_without_exposing_token(self) -> None:
        token = "dummy.bot.token_value_that_is_long_enough"
        with self.assertRaises(DiscordWebhookError) as raised:
            execute_discord_bot(
                token,
                "not-a-channel",
                {"content": "test"},
                timeout_seconds=1,
                retries=1,
            )
        self.assertNotIn(token, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
