from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EntityChange:
    source: str
    kind: str
    entity_key: str
    created: bool
    changed: bool
    reactivated: bool
    previous: dict[str, Any] | None
    current: dict[str, Any]
    previous_hash: str | None
    current_hash: str


class TrackerDB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TrackerDB":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              completed_at TEXT,
              status TEXT NOT NULL,
              baseline INTEGER NOT NULL DEFAULT 0,
              stats_json TEXT NOT NULL DEFAULT '{}',
              error_text TEXT
            );

            CREATE TABLE IF NOT EXISTS entities (
              source TEXT NOT NULL,
              kind TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              current_hash TEXT NOT NULL,
              current_json TEXT NOT NULL,
              PRIMARY KEY (source, kind, entity_key)
            );

            CREATE INDEX IF NOT EXISTS entities_kind_active
              ON entities(source, kind, active);

            CREATE TABLE IF NOT EXISTS versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL REFERENCES runs(id),
              source TEXT NOT NULL,
              kind TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              UNIQUE(source, kind, entity_key, payload_hash)
            );

            CREATE INDEX IF NOT EXISTS versions_entity
              ON versions(source, kind, entity_key, id DESC);

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL REFERENCES runs(id),
              observed_at TEXT NOT NULL,
              category TEXT NOT NULL,
              severity TEXT NOT NULL,
              source TEXT NOT NULL,
              entity_kind TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              field_path TEXT,
              old_json TEXT NOT NULL,
              new_json TEXT NOT NULL,
              evidence_json TEXT NOT NULL,
              dedupe_key TEXT NOT NULL UNIQUE
            );

            CREATE INDEX IF NOT EXISTS events_time
              ON events(observed_at DESC);
            CREATE INDEX IF NOT EXISTS events_category
              ON events(category, observed_at DESC);

            CREATE TABLE IF NOT EXISTS notification_deliveries (
              channel TEXT NOT NULL,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              last_attempt_at TEXT NOT NULL,
              sent_at TEXT,
              PRIMARY KEY (channel, event_id)
            );

            CREATE INDEX IF NOT EXISTS notification_deliveries_status
              ON notification_deliveries(channel, status, event_id);

            CREATE TABLE IF NOT EXISTS candidate_decisions (
              candidate_key TEXT PRIMARY KEY,
              disposition TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def begin_run(self, *, baseline: bool) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs(started_at, status, baseline) VALUES (?, 'running', ?)",
            (utc_now(), int(baseline)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def begin_changes(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def rollback_changes(self) -> None:
        self.conn.rollback()

    def commit_changes(self) -> None:
        self.conn.commit()

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        stats: dict[str, Any],
        error_text: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
               SET completed_at = ?, status = ?, stats_json = ?, error_text = ?
             WHERE id = ?
            """,
            (utc_now(), status, canonical_json(stats), error_text, run_id),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (key, value, utc_now()),
        )

    def upsert_entity(
        self,
        run_id: int,
        *,
        source: str,
        kind: str,
        entity_key: str,
        payload: dict[str, Any],
        comparison_payload: dict[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> EntityChange:
        observed_at = observed_at or utc_now()
        encoded = canonical_json(payload)
        comparison_encoded = canonical_json(
            payload if comparison_payload is None else comparison_payload
        )
        current_hash = hashlib.sha256(comparison_encoded.encode("utf-8")).hexdigest()
        row = self.conn.execute(
            """
            SELECT active, current_hash, current_json
              FROM entities
             WHERE source = ? AND kind = ? AND entity_key = ?
            """,
            (source, kind, entity_key),
        ).fetchone()

        created = row is None
        previous = None if row is None else json.loads(row["current_json"])
        previous_hash = None if row is None else str(row["current_hash"])
        reactivated = bool(row is not None and not row["active"])
        changed = created or previous_hash != current_hash

        if created:
            self.conn.execute(
                """
                INSERT INTO entities(
                  source, kind, entity_key, first_seen, last_seen,
                  active, current_hash, current_json
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    source,
                    kind,
                    entity_key,
                    observed_at,
                    observed_at,
                    current_hash,
                    encoded,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE entities
                   SET last_seen = ?, active = 1,
                       current_hash = ?, current_json = ?
                 WHERE source = ? AND kind = ? AND entity_key = ?
                """,
                (
                    observed_at,
                    current_hash,
                    encoded,
                    source,
                    kind,
                    entity_key,
                ),
            )

        if changed:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO versions(
                  run_id, source, kind, entity_key, observed_at,
                  payload_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    kind,
                    entity_key,
                    observed_at,
                    current_hash,
                    encoded,
                ),
            )

        return EntityChange(
            source=source,
            kind=kind,
            entity_key=entity_key,
            created=created,
            changed=changed,
            reactivated=reactivated,
            previous=previous,
            current=payload,
            previous_hash=previous_hash,
            current_hash=current_hash,
        )

    def mark_missing(
        self,
        *,
        source: str,
        kind: str,
        present_keys: Iterable[str],
        key_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        present = set(present_keys)
        sql = """
            SELECT entity_key, current_json
              FROM entities
             WHERE source = ? AND kind = ? AND active = 1
        """
        params: list[Any] = [source, kind]
        if key_prefix is not None:
            sql += " AND entity_key LIKE ? ESCAPE '\\'"
            escaped = key_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"{escaped}%")
        rows = self.conn.execute(sql, params).fetchall()
        missing: list[dict[str, Any]] = []
        for row in rows:
            key = str(row["entity_key"])
            if key in present:
                continue
            payload = json.loads(row["current_json"])
            missing.append({"entity_key": key, "payload": payload})
            self.conn.execute(
                """
                UPDATE entities SET active = 0
                 WHERE source = ? AND kind = ? AND entity_key = ?
                """,
                (source, kind, key),
            )
        return missing

    def deactivate_entity(
        self, *, source: str, kind: str, entity_key: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT current_json, active FROM entities
             WHERE source = ? AND kind = ? AND entity_key = ?
            """,
            (source, kind, entity_key),
        ).fetchone()
        if row is None or not row["active"]:
            return None
        self.conn.execute(
            """
            UPDATE entities SET active = 0
             WHERE source = ? AND kind = ? AND entity_key = ?
            """,
            (source, kind, entity_key),
        )
        return json.loads(row["current_json"])

    def record_event(
        self,
        run_id: int,
        *,
        category: str,
        severity: str,
        source: str,
        entity_kind: str,
        entity_key: str,
        field_path: str | None = None,
        old: Any = None,
        new: Any = None,
        evidence: dict[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> bool:
        observed_at = observed_at or utc_now()
        evidence = evidence or {}
        material = {
            "run_id": run_id,
            "category": category,
            "source": source,
            "entity_kind": entity_kind,
            "entity_key": entity_key,
            "field_path": field_path,
            "old": old,
            "new": new,
            "evidence": evidence,
        }
        dedupe_key = payload_hash(material)
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO events(
              run_id, observed_at, category, severity, source,
              entity_kind, entity_key, field_path, old_json,
              new_json, evidence_json, dedupe_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                observed_at,
                category,
                severity,
                source,
                entity_kind,
                entity_key,
                field_path,
                canonical_json(old),
                canonical_json(new),
                canonical_json(evidence),
                dedupe_key,
            ),
        )
        return cursor.rowcount > 0

    def list_entities(
        self,
        *,
        source: str | None = None,
        kind: str | None = None,
        active_only: bool = False,
        key_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if active_only:
            clauses.append("active = 1")
        if key_prefix is not None:
            clauses.append("entity_key LIKE ?")
            params.append(f"{key_prefix}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            """
            SELECT source, kind, entity_key, first_seen, last_seen,
                   active, current_hash, current_json
              FROM entities
            """
            + where
            + " ORDER BY source, kind, entity_key",
            params,
        ).fetchall()
        return [
            {
                "source": row["source"],
                "kind": row["kind"],
                "entity_key": row["entity_key"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "active": bool(row["active"]),
                "current_hash": row["current_hash"],
                "payload": json.loads(row["current_json"]),
            }
            for row in rows
        ]

    def list_events(
        self,
        *,
        limit: int = 100,
        since: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("observed_at >= ?")
            params.append(since)
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, limit))
        rows = self.conn.execute(
            """
            SELECT id, run_id, observed_at, category, severity, source,
                   entity_kind, entity_key, field_path, old_json,
                   new_json, evidence_json
              FROM events
            """
            + where
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "observed_at": row["observed_at"],
                "category": row["category"],
                "severity": row["severity"],
                "source": row["source"],
                "entity_kind": row["entity_kind"],
                "entity_key": row["entity_key"],
                "field_path": row["field_path"],
                "old": json.loads(row["old_json"]),
                "new": json.loads(row["new_json"]),
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _severity_rank(severity: str) -> int:
        ranks = {"low": 1, "medium": 2, "high": 3}
        try:
            return ranks[severity.casefold()]
        except KeyError as exc:
            raise ValueError("severity must be low, medium, or high") from exc

    def pending_notification_events(
        self,
        *,
        channel: str,
        min_severity: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return durable, non-baseline events not yet sent to a channel."""
        minimum_rank = self._severity_rank(min_severity)
        rows = self.conn.execute(
            """
            SELECT e.id, e.run_id, e.observed_at, e.category, e.severity,
                   e.source, e.entity_kind, e.entity_key, e.field_path,
                   e.old_json, e.new_json, e.evidence_json,
                   COALESCE(d.attempts, 0) AS delivery_attempts,
                   d.last_error AS delivery_error
              FROM events AS e
              JOIN runs AS r ON r.id = e.run_id
         LEFT JOIN notification_deliveries AS d
                ON d.channel = ? AND d.event_id = e.id
             WHERE r.baseline = 0
               AND r.completed_at IS NOT NULL
               AND CASE e.severity
                     WHEN 'high' THEN 3
                     WHEN 'medium' THEN 2
                     WHEN 'low' THEN 1
                     ELSE 0
                   END >= ?
               AND (d.status IS NULL OR d.status NOT IN ('sent', 'skipped'))
          ORDER BY e.id ASC
             LIMIT ?
            """,
            (channel, minimum_rank, max(1, limit)),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "run_id": int(row["run_id"]),
                "observed_at": str(row["observed_at"]),
                "category": str(row["category"]),
                "severity": str(row["severity"]),
                "source": str(row["source"]),
                "entity_kind": str(row["entity_kind"]),
                "entity_key": str(row["entity_key"]),
                "field_path": row["field_path"],
                "old": json.loads(row["old_json"]),
                "new": json.loads(row["new_json"]),
                "evidence": json.loads(row["evidence_json"]),
                "delivery_attempts": int(row["delivery_attempts"]),
                "delivery_error": row["delivery_error"],
            }
            for row in rows
        ]

    def record_notification_delivery(
        self,
        *,
        channel: str,
        event_ids: Iterable[int],
        status: str,
        error: str | None = None,
    ) -> None:
        if status not in {"sent", "failed", "skipped"}:
            raise ValueError("notification status must be sent, failed, or skipped")
        now = utc_now()
        sent_at = now if status == "sent" else None
        attempt_increment = 0 if status == "skipped" else 1
        for event_id in event_ids:
            self.conn.execute(
                """
                INSERT INTO notification_deliveries(
                  channel, event_id, status, attempts, last_error,
                  last_attempt_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, event_id) DO UPDATE SET
                  status = excluded.status,
                  attempts = notification_deliveries.attempts + excluded.attempts,
                  last_error = excluded.last_error,
                  last_attempt_at = excluded.last_attempt_at,
                  sent_at = CASE
                    WHEN excluded.status = 'sent' THEN excluded.sent_at
                    ELSE notification_deliveries.sent_at
                  END
                """,
                (
                    channel,
                    int(event_id),
                    status,
                    attempt_increment,
                    error,
                    now,
                    sent_at,
                ),
            )
        self.conn.commit()

    def set_event_severity(
        self,
        *,
        categories: Iterable[str],
        severity: str,
    ) -> int:
        """Reclassify stored events after a notification-policy change."""
        self._severity_rank(severity)
        names = sorted({str(category) for category in categories if category})
        if not names:
            return 0
        placeholders = ",".join("?" for _ in names)
        cursor = self.conn.execute(
            f"UPDATE events SET severity = ? "
            f"WHERE category IN ({placeholders}) AND severity != ?",
            (severity, *names, severity),
        )
        self.conn.commit()
        return max(0, int(cursor.rowcount))

    def notification_summary(
        self, *, channel: str, min_severity: str
    ) -> dict[str, int]:
        minimum_rank = self._severity_rank(min_severity)
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS eligible,
                   SUM(CASE WHEN d.status = 'sent' THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN d.status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                   SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN d.status IS NULL
                                  OR d.status NOT IN ('sent', 'skipped')
                            THEN 1 ELSE 0 END) AS pending,
                   COALESCE(SUM(d.attempts), 0) AS attempts
              FROM events AS e
              JOIN runs AS r ON r.id = e.run_id
         LEFT JOIN notification_deliveries AS d
                ON d.channel = ? AND d.event_id = e.id
             WHERE r.baseline = 0
               AND r.completed_at IS NOT NULL
               AND CASE e.severity
                     WHEN 'high' THEN 3
                     WHEN 'medium' THEN 2
                     WHEN 'low' THEN 1
                     ELSE 0
                   END >= ?
            """,
            (channel, minimum_rank),
        ).fetchone()
        return {
            "eligible": int(row["eligible"] or 0),
            "sent": int(row["sent"] or 0),
            "skipped": int(row["skipped"] or 0),
            "failed": int(row["failed"] or 0),
            "pending": int(row["pending"] or 0),
            "attempts": int(row["attempts"] or 0),
        }

    def rehash_entities(
        self,
        *,
        source: str | None,
        kind: str,
        transform: Callable[[dict[str, Any]], Any],
    ) -> int:
        clauses = ["kind = ?"]
        params: list[Any] = [kind]
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        rows = self.conn.execute(
            "SELECT source, kind, entity_key, current_json FROM entities WHERE "
            + " AND ".join(clauses),
            params,
        ).fetchall()
        for row in rows:
            payload = json.loads(row["current_json"])
            self.conn.execute(
                """
                UPDATE entities SET current_hash = ?
                 WHERE source = ? AND kind = ? AND entity_key = ?
                """,
                (
                    payload_hash(transform(payload)),
                    row["source"],
                    row["kind"],
                    row["entity_key"],
                ),
            )
        self.conn.commit()
        return len(rows)

    def set_candidate_decision(
        self, candidate_key: str, disposition: str, note: str
    ) -> None:
        allowed = {"unknown", "watching", "rejected", "promoted"}
        if disposition not in allowed:
            raise ValueError(f"disposition must be one of {sorted(allowed)}")
        self.conn.execute(
            """
            INSERT INTO candidate_decisions(
              candidate_key, disposition, note, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(candidate_key) DO UPDATE SET
              disposition = excluded.disposition,
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (candidate_key, disposition, note, utc_now()),
        )
        self.conn.commit()

    def candidate_decisions(self) -> dict[str, dict[str, str]]:
        rows = self.conn.execute(
            "SELECT candidate_key, disposition, note, updated_at FROM candidate_decisions"
        ).fetchall()
        return {
            str(row["candidate_key"]): {
                "disposition": str(row["disposition"]),
                "note": str(row["note"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        }

    def latest_run(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, started_at, completed_at, status, baseline,
                   stats_json, error_text
              FROM runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "status": row["status"],
            "baseline": bool(row["baseline"]),
            "stats": json.loads(row["stats_json"]),
            "error": row["error_text"],
        }
