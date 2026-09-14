from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import TrackerConfig
from .contracts import cross_source_contract_mismatches
from .db import TrackerDB


def _rows_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["entity_key"]): row for row in rows}


def _markdown(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value).replace("\n", " ").replace("|", "\\|")
    return text if text else "—"


def _milestone_range(trial: dict[str, Any]) -> str:
    start = trial.get("start_milestone")
    end = trial.get("end_milestone")
    if start is None and end is None:
        return "—"
    if start == end:
        return f"M{start}"
    return f"M{start or '?'}–M{end or '?'}"


def build_report_data(config: TrackerConfig, db: TrackerDB) -> dict[str, Any]:
    trial_rows = db.list_entities(
        source="chromestatus", kind="origin_trial", active_only=True
    )
    trials = [row["payload"] for row in trial_rows]
    active_trials = sorted(
        (trial for trial in trials if trial.get("status") == "ACTIVE"),
        key=lambda item: (str(item.get("display_name") or "").casefold(), item["id"]),
    )
    feature_rows = _rows_by_key(
        db.list_entities(source="chromestatus", kind="feature", active_only=True)
    )
    declaration_rows = db.list_entities(
        source="chromium", kind="runtime_declaration", active_only=True
    )
    release_declaration_rows = db.list_entities(
        source="chromium_release", kind="runtime_declaration", active_only=True
    )
    declarations_by_trial: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in declaration_rows:
        declaration = row["payload"]
        declarations_by_trial[str(declaration.get("trial_name")).casefold()].append(
            declaration
        )
    release_declarations_by_trial: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in release_declaration_rows:
        declaration = row["payload"]
        release_declarations_by_trial[
            str(declaration.get("trial_name") or "").casefold()
        ].append(declaration)

    decisions = db.candidate_decisions()
    candidate_rows = db.list_entities(
        kind="pre_registration_candidate", active_only=True
    )
    candidates = []
    for row in candidate_rows:
        payload = dict(row["payload"])
        decision = decisions.get(row["entity_key"], {})
        payload["source"] = row["source"]
        payload["first_seen"] = row["first_seen"]
        payload["last_seen"] = row["last_seen"]
        payload["disposition"] = decision.get("disposition", "unknown")
        payload["decision_note"] = decision.get("note", "")
        candidates.append(payload)
    candidates.sort(
        key=lambda item: (-int(item.get("score") or 0), str(item.get("candidate_key")))
    )

    implementation_candidate_rows = db.list_entities(
        source="tracker",
        kind="implementation_path_candidate",
        active_only=True,
    )
    implementation_path_candidates: list[dict[str, Any]] = []
    implementation_candidates_by_trial: dict[str, list[dict[str, Any]]] = defaultdict(
        list
    )
    for row in implementation_candidate_rows:
        payload = {
            **row["payload"],
            "candidate_key": row["entity_key"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
        }
        implementation_path_candidates.append(payload)
        implementation_candidates_by_trial[str(payload.get("trial_name") or "")].append(
            payload
        )
    implementation_path_candidates.sort(
        key=lambda item: (
            str(item.get("trial_name") or "").casefold(),
            -int(item.get("score") or 0),
            str(item.get("path") or ""),
        )
    )

    inventory = []
    missing_runtime: list[str] = []
    missing_any_tracked_runtime: list[str] = []
    missing_feature: list[str] = []
    missing_explicit_paths: list[str] = []
    missing_implementation_paths: list[str] = []
    contract_mismatches = cross_source_contract_mismatches(
        active_trials,
        (row["payload"] for row in declaration_rows),
    )
    for trial in active_trials:
        trial_name = str(trial.get("trial_name") or "")
        target_rule = config.target_rules.get(trial_name)
        explicit_paths = sorted(target_rule.paths) if target_rule else []
        inferred_paths = sorted(
            {
                str(candidate.get("path"))
                for candidate in implementation_candidates_by_trial.get(trial_name, [])
                if candidate.get("auto_applied")
                and int(candidate.get("score") or 0)
                >= config.gerrit_auto_path_min_score
                and candidate.get("path")
            }
        )
        implementation_paths = sorted({*explicit_paths, *inferred_paths})
        feature_id = trial.get("chromestatus_feature_id")
        feature_row = feature_rows.get(str(feature_id)) if feature_id else None
        feature = feature_row["payload"] if feature_row else None
        declarations = sorted(
            declarations_by_trial.get(trial_name.casefold(), []),
            key=lambda item: item["runtime_feature_name"],
        )
        release_declarations = sorted(
            release_declarations_by_trial.get(trial_name.casefold(), []),
            key=lambda item: (
                str(item.get("channel") or ""),
                str(item.get("runtime_feature_name") or ""),
            ),
        )
        if not declarations:
            missing_runtime.append(trial_name)
        if not declarations and not release_declarations:
            missing_any_tracked_runtime.append(trial_name)
        if feature is None:
            missing_feature.append(trial_name)
        if not explicit_paths:
            missing_explicit_paths.append(trial_name)
        if not implementation_paths:
            missing_implementation_paths.append(trial_name)
        inventory.append(
            {
                "trial": trial,
                "feature": feature,
                "runtime_features": [
                    declaration["runtime_feature_name"] for declaration in declarations
                ],
                "explicit_implementation_paths": explicit_paths,
                "inferred_implementation_paths": inferred_paths,
                "implementation_paths": implementation_paths,
                "classifications": sorted(
                    {
                        classification
                        for declaration in declarations
                        for classification in declaration.get("classifications", [])
                    }
                ),
                "runtime_declarations": [
                    {
                        "runtime_feature_name": declaration["runtime_feature_name"],
                        "fields": declaration.get("fields", {}),
                        "classifications": declaration.get("classifications", []),
                        "source_path": declaration.get("source_path"),
                        "source_line": declaration.get("source_line"),
                    }
                    for declaration in declarations
                ],
                "release_runtime_declarations": [
                    {
                        "channel": declaration.get("channel"),
                        "platform": declaration.get("platform"),
                        "milestone": declaration.get("milestone"),
                        "version": declaration.get("version"),
                        "revision": declaration.get("revision"),
                        "runtime_feature_name": declaration.get(
                            "runtime_feature_name"
                        ),
                    }
                    for declaration in release_declarations
                ],
            }
        )

    since = (
        datetime.now(UTC) - timedelta(days=config.recent_event_days)
    ).isoformat(timespec="seconds")
    events = db.list_events(limit=500, since=since)
    rejection_count = len(
        db.list_entities(kind="candidate_rejection", active_only=True)
    )
    open_premerge_rows = db.list_entities(
        source="gerrit", kind="premerge_ot_code_change", active_only=True
    )
    open_premerge_changes = len(
        {
            str((row["payload"].get("change") or {}).get("change_number"))
            for row in open_premerge_rows
        }
    )
    auto_applied_path_count = sum(
        bool(candidate.get("auto_applied"))
        for candidate in implementation_path_candidates
    )

    latest_run = db.latest_run()
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "latest_run": latest_run,
        "summary": {
            "console_records": len(trials),
            "active_trials": len(active_trials),
            "runtime_declarations": len(declaration_rows),
            "release_runtime_declarations": len(release_declaration_rows),
            "active_trials_with_runtime_mapping": len(active_trials)
            - len(missing_runtime),
            "active_trials_with_any_tracked_runtime_mapping": len(active_trials)
            - len(missing_any_tracked_runtime),
            "active_trials_with_explicit_paths": len(active_trials)
            - len(missing_explicit_paths),
            "active_trials_with_implementation_paths": len(active_trials)
            - len(missing_implementation_paths),
            "implementation_path_candidates": len(implementation_path_candidates),
            "auto_applied_implementation_paths": auto_applied_path_count,
            "open_premerge_changes": open_premerge_changes,
            "pre_registration_candidates": len(candidates),
            "preserved_candidate_rejections": rejection_count,
            "recent_events": len(events),
        },
        "inventory": inventory,
        "candidates": candidates,
        "implementation_path_candidates": implementation_path_candidates,
        "recent_events": events,
        "coverage_unknowns": {
            "active_without_runtime_declaration": missing_runtime,
            "active_without_any_tracked_runtime_declaration": (
                missing_any_tracked_runtime
            ),
            "active_without_feature_detail": missing_feature,
            "active_without_explicit_paths": missing_explicit_paths,
            "active_without_implementation_paths": missing_implementation_paths,
            "cross_source_contract_mismatches": contract_mismatches,
            "implementation_monitoring": (
                "Gerrit alias matching covers every active trial. Curated or merged-CL "
                "inferred source paths cover name-less implementation changes for all "
                "currently active trials."
                if not missing_implementation_paths
                else "Gerrit aliases cover every active trial, but name-less implementation "
                "changes remain incomplete for the trials listed as missing implementation "
                "paths."
            ),
            "candidate_interpretation": (
                "A candidate is an integration signal, not proof that a public origin trial "
                "will launch or that the code is vulnerable."
            ),
        },
    }


def render_markdown(data: dict[str, Any]) -> str:
    summary = data["summary"]
    latest = data.get("latest_run") or {}
    lines = [
        "# Chrome Origin Trial Tracker",
        "",
        f"Generated: `{data['generated_at']}`",
        "",
        "## 상태",
        "",
        f"- 최근 실행: `{latest.get('status', 'none')}` (run `{latest.get('id', '—')}`)",
        f"- 공개 OT 레코드: **{summary['console_records']}**, 활성: **{summary['active_trials']}**",
        (
            "- Chromium main RuntimeEnabledFeature 매핑: "
            f"**{summary['active_trials_with_runtime_mapping']}/{summary['active_trials']}**"
        ),
        (
            "- main + Stable/Beta RuntimeEnabledFeature 매핑: "
            f"**{summary['active_trials_with_any_tracked_runtime_mapping']}/"
            f"{summary['active_trials']}** "
            f"(배포 채널 선언 {summary['release_runtime_declarations']}개)"
        ),
        (
            "- Chromium 구현 경로 매핑: "
            f"**{summary['active_trials_with_implementation_paths']}/"
            f"{summary['active_trials']}** "
            f"(자동 추론 {summary['auto_applied_implementation_paths']}개)"
        ),
        f"- 병합 전 추적 중인 Gerrit CL: **{summary['open_premerge_changes']}개**",
        f"- 공식 등록 전 후보: **{summary['pre_registration_candidates']}**",
        f"- 보존된 기각/저신뢰 신호: **{summary['preserved_candidate_rejections']}**",
        "",
        "## 활성 OT 인벤토리",
        "",
        "| OT | Trial code | Type | Milestones | 3P | Runtime features | Code flags | Component |",
        "|---|---|---:|---:|:---:|---|---|---|",
    ]
    for item in data["inventory"]:
        trial = item["trial"]
        feature = item.get("feature") or {}
        name = _markdown(trial.get("display_name"))
        url = trial.get("chromestatus_url")
        linked_name = f"[{name}]({url})" if url else name
        components = ", ".join(feature.get("blink_components") or []) or "—"
        runtime = ", ".join(f"`{name}`" for name in item["runtime_features"])
        if not runtime:
            release_runtime = item.get("release_runtime_declarations") or []
            runtime = ", ".join(
                f"{entry.get('channel')} M{entry.get('milestone')}: "
                f"`{entry.get('runtime_feature_name')}`"
                for entry in release_runtime
            ) or "unknown"
        code_flags: set[str] = set(item.get("classifications") or [])
        for declaration in item.get("runtime_declarations") or []:
            fields = declaration.get("fields") or {}
            if fields.get("origin_trial_allows_third_party"):
                code_flags.add("3P")
            if fields.get("origin_trial_allows_insecure"):
                code_flags.add("insecure")
            if fields.get("origin_trial_type"):
                code_flags.add(str(fields["origin_trial_type"]))
            operating_systems = fields.get("origin_trial_os") or []
            if isinstance(operating_systems, list) and operating_systems:
                code_flags.add(
                    "os=" + ",".join(str(os_name) for os_name in operating_systems)
                )
        flags = ", ".join(sorted(code_flags)) or "—"
        lines.append(
            "| "
            + " | ".join(
                (
                    linked_name,
                    f"`{_markdown(trial.get('trial_name'))}`",
                    _markdown(trial.get("type")),
                    _milestone_range(trial),
                    "yes" if trial.get("allow_third_party_origins") else "no",
                    runtime,
                    _markdown(flags),
                    _markdown(components),
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 공식 등록 전 후보",
            "",
            "| Score | Candidate | Signal | Disposition | Evidence |",
            "|---:|---|---|---|---|",
        ]
    )
    visible_candidates = [
        candidate
        for candidate in data["candidates"]
        if candidate.get("disposition") != "rejected"
    ]
    if not visible_candidates:
        lines.append("| — | 현재 열린 후보 없음 | — | — | — |")
    for candidate in visible_candidates:
        change = candidate.get("change") or {}
        candidate_name = candidate.get("trial_name") or change.get("subject") or candidate.get(
            "candidate_key"
        )
        evidence = "; ".join(candidate.get("reasons") or [])
        lines.append(
            "| "
            + " | ".join(
                (
                    str(candidate.get("score", "—")),
                    _markdown(candidate_name),
                    _markdown(candidate.get("signal_type")),
                    _markdown(candidate.get("disposition")),
                    _markdown(evidence),
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 자동 구현 경로 후보",
            "",
            "| Trial | Score | Path | CL status | Applied |",
            "|---|---:|---|:---:|:---:|",
        ]
    )
    path_candidates = data.get("implementation_path_candidates") or []
    if not path_candidates:
        lines.append("| — | — | 현재 경로 후보 없음 | — | — |")
    for candidate in path_candidates:
        path = candidate.get("path")
        change_url = candidate.get("change_url")
        linked_path = (
            f"[{_markdown(path)}]({change_url})"
            if change_url
            else _markdown(path)
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{_markdown(candidate.get('trial_name'))}`",
                    str(candidate.get("score", "—")),
                    linked_path,
                    _markdown(candidate.get("change_status")),
                    "yes" if candidate.get("auto_applied") else "no",
                )
            )
            + " |"
        )

    unknowns = data["coverage_unknowns"]
    lines.extend(["", "## Coverage / unknowns", ""])
    lines.append(
        "- 활성 OT 중 Chromium main runtime 선언을 찾지 못한 항목: "
        + (", ".join(f"`{name}`" for name in unknowns["active_without_runtime_declaration"])
           or "없음")
    )
    lines.append(
        "- main과 Stable/Beta 모두에서 runtime 선언을 찾지 못한 항목: "
        + (
            ", ".join(
                f"`{name}`"
                for name in unknowns[
                    "active_without_any_tracked_runtime_declaration"
                ]
            )
            or "없음"
        )
    )
    lines.append(
        "- Chrome Status 상세정보를 가져오지 못한 항목: "
        + (", ".join(f"`{name}`" for name in unknowns["active_without_feature_detail"])
           or "없음")
    )
    lines.append(
        "- 명시적 Chromium 구현 경로가 없는 활성 OT: "
        + (", ".join(f"`{name}`" for name in unknowns["active_without_explicit_paths"])
           or "없음")
    )
    lines.append(
        "- 명시적/자동 추론 구현 경로가 모두 없는 활성 OT: "
        + (
            ", ".join(
                f"`{name}`" for name in unknowns["active_without_implementation_paths"]
            )
            or "없음"
        )
    )
    mismatches = unknowns["cross_source_contract_mismatches"]
    lines.append(
        "- Chrome Status ↔ Chromium 계약 불일치: "
        + (
            "; ".join(
                f"`{item['trial_name']}` {item['field']} "
                f"(console={item['console']}, chromium={item['chromium']})"
                for item in mismatches
            )
            or "없음"
        )
    )
    lines.append(f"- 구현 변경 커버리지: {unknowns['implementation_monitoring']}")
    lines.append(f"- 후보 해석: {unknowns['candidate_interpretation']}")

    lines.extend(
        [
            "",
            f"## 최근 이벤트 ({summary['recent_events']})",
            "",
            "| Time | Severity | Category | Entity | Evidence |",
            "|---|:---:|---|---|---|",
        ]
    )
    if not data["recent_events"]:
        lines.append("| — | — | baseline only | — | — |")
    for event in data["recent_events"][:100]:
        evidence = event.get("evidence") or {}
        changes = evidence.get("changes")
        if changes:
            detail = ", ".join(str(change.get("path")) for change in changes[:6])
            if len(changes) > 6:
                detail += f" (+{len(changes) - 6})"
        else:
            detail = evidence.get("change_url") or evidence.get("trial_name") or "—"
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown(event.get("observed_at")),
                    _markdown(event.get("severity")),
                    f"`{_markdown(event.get('category'))}`",
                    f"`{_markdown(event.get('entity_key'))}`",
                    _markdown(detail),
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_reports(config: TrackerConfig) -> dict[str, Path]:
    with TrackerDB(config.database_path) as db:
        data = build_report_data(config, db)
    json_path = config.reports_dir / "latest.json"
    markdown_path = config.reports_dir / "latest.md"
    _atomic_write(
        json_path,
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(markdown_path, render_markdown(data))
    return {"json": json_path, "markdown": markdown_path}
