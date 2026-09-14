from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .chromestatus import ChromeStatusClient
from .chromium import ChromiumReleaseClient, ChromiumSourceClient
from .config import TrackerConfig, load_config
from .db import TrackerDB
from .http import HttpClient
from .notifications import (
    DISCORD_CHANNEL,
    deliver_discord_pending,
    discord_configuration_status,
    maybe_send_discord_heartbeat,
    send_discord_test,
)
from .reporting import build_report_data, write_reports
from .sync import run_sync


def _default_config() -> str:
    return os.environ.get("OT_TRACKER_CONFIG", "config.toml")


@contextmanager
def _sync_lock(config: TrackerConfig) -> Iterator[None]:
    lock_path = config.database_path.with_suffix(config.database_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another tracker sync holds {lock_path}") from exc
        yield


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _deliver_notifications_and_health(
    config: TrackerConfig,
    result: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with TrackerDB(config.database_path) as db:
        notifications = deliver_discord_pending(config, db).as_dict()
        if _notification_exit_code(notifications):
            health = {
                "status": "notification_delivery_incomplete",
                "sent": False,
            }
        else:
            health = maybe_send_discord_heartbeat(
                config,
                db,
                run_id=result.run_id,
                run_status=result.status,
                stats=result.stats,
            )
    return notifications, health


def _notification_exit_code(result: dict[str, Any]) -> int:
    if result["status"] in {
        "failed",
        "partial",
        "not_configured",
        "invalid_configuration",
    }:
        return 1
    return 0


def _health_exit_code(result: dict[str, Any]) -> int:
    return 1 if result.get("status") == "failed" else 0


def command_sync(args: argparse.Namespace, config: TrackerConfig) -> int:
    with _sync_lock(config):
        result = run_sync(
            config,
            emit_baseline=args.emit_baseline,
            gerrit_enabled=not args.no_gerrit,
        )
        report_paths = {} if args.no_report else write_reports(config)
        notifications, health = _deliver_notifications_and_health(config, result)
    output = {
        "run_id": result.run_id,
        "status": result.status,
        "events_created": result.events_created,
        "stats": result.stats,
        "reports": {key: str(path) for key, path in report_paths.items()},
        "notifications": notifications,
        "health": health,
    }
    _print_json(output)
    sync_exit = 0 if result.status == "complete" else (1 if result.status == "partial" else 2)
    return max(
        sync_exit,
        _notification_exit_code(notifications),
        _health_exit_code(health),
    )


def command_report(args: argparse.Namespace, config: TrackerConfig) -> int:
    paths = write_reports(config)
    _print_json({key: str(path) for key, path in paths.items()})
    return 0


def command_status(args: argparse.Namespace, config: TrackerConfig) -> int:
    with TrackerDB(config.database_path) as db:
        data = build_report_data(config, db)
        notifications = {
            "configuration": discord_configuration_status(config),
            "delivery": db.notification_summary(
                channel=DISCORD_CHANNEL,
                min_severity=config.discord.min_severity,
            ),
            "heartbeat_last_sent_at": db.get_meta(
                "notification.discord.heartbeat_sent_at"
            ),
        }
    data["notifications"] = notifications
    if args.full:
        _print_json(data)
    else:
        _print_json(
            {
                "generated_at": data["generated_at"],
                "latest_run": data["latest_run"],
                "summary": data["summary"],
                "coverage_unknowns": data["coverage_unknowns"],
                "notifications": notifications,
            }
        )
    return 0


def command_events(args: argparse.Namespace, config: TrackerConfig) -> int:
    since = None
    if args.since_hours is not None:
        since = (
            datetime.now(UTC) - timedelta(hours=args.since_hours)
        ).isoformat(timespec="seconds")
    with TrackerDB(config.database_path) as db:
        events = db.list_events(
            limit=args.limit, since=since, category=args.category
        )
    _print_json(events)
    return 0


def command_candidates(args: argparse.Namespace, config: TrackerConfig) -> int:
    with TrackerDB(config.database_path) as db:
        data = build_report_data(config, db)
    candidates = data["candidates"]
    if not args.include_rejected:
        candidates = [
            candidate
            for candidate in candidates
            if candidate.get("disposition") != "rejected"
        ]
    _print_json(candidates)
    return 0


def command_triage(args: argparse.Namespace, config: TrackerConfig) -> int:
    with TrackerDB(config.database_path) as db:
        known = {
            row["entity_key"]
            for row in db.list_entities(kind="pre_registration_candidate")
        }
        if args.candidate_key not in known:
            print(f"unknown candidate key: {args.candidate_key}", file=sys.stderr)
            return 2
        db.set_candidate_decision(args.candidate_key, args.disposition, args.note)
    write_reports(config)
    _print_json(
        {
            "candidate_key": args.candidate_key,
            "disposition": args.disposition,
            "note": args.note,
        }
    )
    return 0


def command_doctor(args: argparse.Namespace, config: TrackerConfig) -> int:
    http = HttpClient(
        timeout_seconds=config.http_timeout_seconds,
        retries=config.http_retries,
        user_agent=config.user_agent,
    )
    checks: dict[str, Any] = {
        "config": str(config.config_path),
        "database": str(config.database_path),
    }
    failed = False
    discord = discord_configuration_status(config)
    checks["discord"] = discord
    failed = failed or bool(discord["enabled"] and not discord["valid"])
    try:
        trials = ChromeStatusClient(config, http).fetch_trials()
        checks["chromestatus"] = {
            "ok": True,
            "records": len(trials),
            "active": sum(trial.get("status") == "ACTIVE" for trial in trials),
        }
    except Exception as exc:
        failed = True
        checks["chromestatus"] = {"ok": False, "error": str(exc)}
    chromium_client = ChromiumSourceClient(config, http)
    try:
        snapshot = chromium_client.fetch_snapshot()
        checks["chromium"] = {
            "ok": not snapshot.file_errors,
            "revision": snapshot.revision,
            "declarations": len(snapshot.declarations),
            "file_errors": snapshot.file_errors,
        }
        failed = failed or bool(snapshot.file_errors)
    except Exception as exc:
        failed = True
        checks["chromium"] = {"ok": False, "error": str(exc)}
    if config.chromium_release_channels:
        releases = ChromiumReleaseClient(
            config,
            http,
            source_client=chromium_client,
        ).fetch_snapshots()
        checks["chromium_releases"] = {
            "ok": not releases.errors,
            "platform": config.chromium_release_platform,
            "channels": {
                channel: {
                    "milestone": release.milestone,
                    "version": release.version,
                    "revision": release.revision,
                    "declarations": len(release.chromium.declarations),
                }
                for channel, release in sorted(releases.snapshots.items())
            },
            "errors": releases.errors,
        }
        failed = failed or bool(releases.errors)
    try:
        with TrackerDB(config.database_path) as db:
            checks["sqlite"] = {
                "ok": True,
                "latest_run": db.latest_run(),
                "discord_delivery": db.notification_summary(
                    channel=DISCORD_CHANNEL,
                    min_severity=config.discord.min_severity,
                ),
            }
    except Exception as exc:
        failed = True
        checks["sqlite"] = {"ok": False, "error": str(exc)}
    _print_json(checks)
    return 1 if failed else 0


def command_discord_test(args: argparse.Namespace, config: TrackerConfig) -> int:
    send_discord_test(config)
    _print_json(
        {
            "status": "sent",
            "channel": "discord",
            "message": "Discord 연결 테스트 메시지를 전송했습니다",
        }
    )
    return 0


def command_discord_skip_pending(
    args: argparse.Namespace, config: TrackerConfig
) -> int:
    with _sync_lock(config), TrackerDB(config.database_path) as db:
        pending = db.pending_notification_events(
            channel=DISCORD_CHANNEL,
            min_severity=config.discord.min_severity,
            limit=1_000_000,
        )
        event_ids = [
            int(event["id"])
            for event in pending
            if int(event["id"]) <= args.through_event
        ]
        db.record_notification_delivery(
            channel=DISCORD_CHANNEL,
            event_ids=event_ids,
            status="skipped",
            error=args.reason,
        )
        summary = db.notification_summary(
            channel=DISCORD_CHANNEL,
            min_severity=config.discord.min_severity,
        )
    _print_json(
        {
            "status": "skipped",
            "events_skipped": len(event_ids),
            "through_event": args.through_event,
            "delivery": summary,
        }
    )
    return 0


def command_watch(args: argparse.Namespace, config: TrackerConfig) -> int:
    interval = max(60, args.interval_seconds)
    while True:
        try:
            with _sync_lock(config):
                result = run_sync(
                    config,
                    emit_baseline=args.emit_baseline,
                    gerrit_enabled=not args.no_gerrit,
                )
                paths = write_reports(config)
                notifications, health = _deliver_notifications_and_health(config, result)
            _print_json(
                {
                    "run_id": result.run_id,
                    "status": result.status,
                    "events_created": result.events_created,
                    "reports": {key: str(path) for key, path in paths.items()},
                    "notifications": notifications,
                    "health": health,
                }
            )
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"sync failed: {exc}", file=sys.stderr, flush=True)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ot-tracker",
        description="Track Chrome Origin Trials and Chromium integration changes.",
    )
    parser.add_argument("--config", default=_default_config(), help="TOML config path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="collect, diff, and report")
    sync_parser.add_argument(
        "--emit-baseline",
        action="store_true",
        help="emit one event per entity during the first collection",
    )
    sync_parser.add_argument("--no-gerrit", action="store_true")
    sync_parser.add_argument("--no-report", action="store_true")
    sync_parser.set_defaults(handler=command_sync)

    report_parser = subparsers.add_parser("report", help="regenerate reports")
    report_parser.set_defaults(handler=command_report)

    status_parser = subparsers.add_parser("status", help="show tracker status")
    status_parser.add_argument("--full", action="store_true")
    status_parser.set_defaults(handler=command_status)

    events_parser = subparsers.add_parser("events", help="show recorded events")
    events_parser.add_argument("--limit", type=int, default=100)
    events_parser.add_argument("--since-hours", type=int)
    events_parser.add_argument("--category")
    events_parser.set_defaults(handler=command_events)

    candidate_parser = subparsers.add_parser(
        "candidates", help="show pre-registration candidates"
    )
    candidate_parser.add_argument("--include-rejected", action="store_true")
    candidate_parser.set_defaults(handler=command_candidates)

    triage_parser = subparsers.add_parser(
        "triage", help="record a human decision for a candidate"
    )
    triage_parser.add_argument("candidate_key")
    triage_parser.add_argument(
        "--disposition",
        required=True,
        choices=("unknown", "watching", "rejected", "promoted"),
    )
    triage_parser.add_argument("--note", default="")
    triage_parser.set_defaults(handler=command_triage)

    doctor_parser = subparsers.add_parser("doctor", help="check sources and storage")
    doctor_parser.set_defaults(handler=command_doctor)

    discord_test_parser = subparsers.add_parser(
        "discord-test", help="send a Discord webhook connection test"
    )
    discord_test_parser.set_defaults(handler=command_discord_test)

    discord_skip_parser = subparsers.add_parser(
        "discord-skip-pending",
        help="preserve but do not send an existing Discord event backlog",
    )
    discord_skip_parser.add_argument("--through-event", type=int, required=True)
    discord_skip_parser.add_argument(
        "--reason", default="operator skipped pre-connection backlog"
    )
    discord_skip_parser.set_defaults(handler=command_discord_skip_pending)

    watch_parser = subparsers.add_parser("watch", help="run sync in a loop")
    watch_parser.add_argument("--interval-seconds", type=int, default=3600)
    watch_parser.add_argument("--emit-baseline", action="store_true")
    watch_parser.add_argument("--no-gerrit", action="store_true")
    watch_parser.set_defaults(handler=command_watch)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        return int(args.handler(args, config))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
