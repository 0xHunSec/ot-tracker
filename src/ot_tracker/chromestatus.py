from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .config import TrackerConfig
from .http import FetchError, HttpClient


OT_STAGE_TYPES = {150, 151, 250, 251, 450, 451}
SHIPPING_STAGE_TYPES = {160, 260, 460}
TRACKED_STAGE_TYPES = OT_STAGE_TYPES | SHIPPING_STAGE_TYPES


def _sorted_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value if item is not None)


def _milestone(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def feature_id_from_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    match = re.search(r"/feature/(\d+)(?:[/?#]|$)", url)
    return match.group(1) if match else None


def canonical_trial(raw: dict[str, Any]) -> dict[str, Any]:
    extensions = []
    for item in raw.get("trial_extensions") or []:
        if not isinstance(item, dict):
            continue
        extensions.append(
            {
                "end_milestone": _milestone(
                    item.get("endMilestone", item.get("end_milestone"))
                ),
                "end_time": item.get("endTime", item.get("end_time")),
                "intent_url": item.get(
                    "extensionIntentUrl", item.get("extension_intent_url")
                ),
            }
        )
    extensions.sort(
        key=lambda item: (
            item.get("end_milestone") or -1,
            item.get("end_time") or "",
            item.get("intent_url") or "",
        )
    )
    chromestatus_url = raw.get("chromestatus_url")
    return {
        "id": str(raw.get("id")),
        "display_name": raw.get("display_name"),
        "description": raw.get("description"),
        "trial_name": raw.get("origin_trial_feature_name"),
        "enabled": bool(raw.get("enabled")),
        "status": raw.get("status"),
        "type": raw.get("type"),
        "allow_third_party_origins": bool(raw.get("allow_third_party_origins")),
        "start_milestone": _milestone(raw.get("start_milestone")),
        "end_milestone": _milestone(raw.get("end_milestone")),
        "original_end_milestone": _milestone(raw.get("original_end_milestone")),
        "end_time": raw.get("end_time"),
        "chromestatus_url": chromestatus_url,
        "chromestatus_feature_id": feature_id_from_url(chromestatus_url),
        "documentation_url": raw.get("documentation_url"),
        "feedback_url": raw.get("feedback_url"),
        "intent_to_experiment_url": raw.get("intent_to_experiment_url"),
        "trial_extensions": extensions,
    }


def canonical_stage(raw: dict[str, Any]) -> dict[str, Any]:
    extensions = raw.get("extensions") or []
    if not isinstance(extensions, list):
        extensions = []
    return {
        "id": str(raw.get("id")),
        "stage_type": raw.get("stage_type"),
        "display_name": raw.get("display_name"),
        "intent_stage": raw.get("intent_stage"),
        "intent_thread_url": raw.get("intent_thread_url"),
        "experiment_goals": raw.get("experiment_goals"),
        "experiment_risks": raw.get("experiment_risks"),
        "experiment_extension_reason": raw.get("experiment_extension_reason"),
        "origin_trial_id": raw.get("origin_trial_id"),
        "origin_trial_feedback_url": raw.get("origin_trial_feedback_url"),
        "ot_chromium_trial_name": raw.get("ot_chromium_trial_name"),
        "ot_display_name": raw.get("ot_display_name"),
        "ot_description": raw.get("ot_description"),
        "ot_documentation_url": raw.get("ot_documentation_url"),
        "ot_feedback_submission_url": raw.get("ot_feedback_submission_url"),
        "ot_has_third_party_support": bool(raw.get("ot_has_third_party_support")),
        "ot_is_critical_trial": bool(raw.get("ot_is_critical_trial")),
        "ot_is_deprecation_trial": bool(raw.get("ot_is_deprecation_trial")),
        "ot_require_approvals": bool(raw.get("ot_require_approvals")),
        "ot_setup_status": raw.get("ot_setup_status"),
        "desktop_first": _milestone(raw.get("desktop_first")),
        "desktop_last": _milestone(raw.get("desktop_last")),
        "android_first": _milestone(raw.get("android_first")),
        "android_last": _milestone(raw.get("android_last")),
        "ios_first": _milestone(raw.get("ios_first")),
        "ios_last": _milestone(raw.get("ios_last")),
        "webview_first": _milestone(raw.get("webview_first")),
        "webview_last": _milestone(raw.get("webview_last")),
        "extensions": extensions,
    }


def canonical_feature(raw: dict[str, Any]) -> dict[str, Any]:
    browsers = raw.get("browsers") or {}
    chrome = browsers.get("chrome") or {}
    resources = raw.get("resources") or {}
    stages = [
        canonical_stage(stage)
        for stage in raw.get("stages") or []
        if isinstance(stage, dict) and stage.get("stage_type") in TRACKED_STAGE_TYPES
    ]
    stages.sort(key=lambda stage: (stage.get("stage_type") or 0, stage["id"]))

    browser_views: dict[str, Any] = {}
    for browser_name in ("ff", "safari", "webdev"):
        view = (browsers.get(browser_name) or {}).get("view") or {}
        browser_views[browser_name] = {
            "text": view.get("text"),
            "url": view.get("url"),
            "notes": view.get("notes"),
        }

    return {
        "id": str(raw.get("id")),
        "name": raw.get("name"),
        "summary": raw.get("summary"),
        "feature_notes": raw.get("feature_notes"),
        "motivation": raw.get("motivation"),
        "category": raw.get("category"),
        "feature_type": raw.get("feature_type"),
        "feature_type_int": raw.get("feature_type_int"),
        "blink_components": _sorted_strings(raw.get("blink_components")),
        "owners": _sorted_strings(chrome.get("owners")),
        "devrel": _sorted_strings(chrome.get("devrel")),
        "chrome_status": chrome.get("status"),
        "chrome_origintrial_flag": chrome.get("origintrial"),
        "bug_url": raw.get("bug_url") or chrome.get("bug"),
        "launch_bug_url": raw.get("launch_bug_url"),
        "flag_name": raw.get("flag_name"),
        "finch_name": raw.get("finch_name"),
        "spec_link": raw.get("spec_link"),
        "api_spec": bool(raw.get("api_spec")),
        "explainer_links": _sorted_strings(raw.get("explainer_links")),
        "doc_links": _sorted_strings(raw.get("doc_links")),
        "sample_links": _sorted_strings(raw.get("sample_links")),
        "resources": {
            "docs": _sorted_strings(resources.get("docs")),
            "samples": _sorted_strings(resources.get("samples")),
        },
        "initial_public_proposal_url": raw.get("initial_public_proposal_url"),
        "standards_positions": browser_views,
        "tags": _sorted_strings(raw.get("tags")),
        "search_tags": _sorted_strings(raw.get("search_tags")),
        "stages": stages,
    }


@dataclass(frozen=True)
class ChromeStatusSnapshot:
    trials: list[dict[str, Any]]
    active_trials: list[dict[str, Any]]
    features: dict[str, dict[str, Any]]
    feature_errors: dict[str, str]


class ChromeStatusClient:
    def __init__(self, config: TrackerConfig, http: HttpClient):
        self.config = config
        self.http = http

    def fetch_trials(self) -> list[dict[str, Any]]:
        response = self.http.get_json(
            f"{self.config.chromestatus_base_url}/origintrials"
        )
        trials = response.get("origin_trials") if isinstance(response, dict) else None
        if not isinstance(trials, list):
            raise FetchError("Chrome Status origintrials response has no origin_trials list")
        canonical = [canonical_trial(item) for item in trials if isinstance(item, dict)]
        canonical.sort(key=lambda item: item["id"])
        return canonical

    def fetch_feature(self, feature_id: str) -> dict[str, Any]:
        response = self.http.get_json(
            f"{self.config.chromestatus_base_url}/features/{feature_id}"
        )
        if not isinstance(response, dict):
            raise FetchError(f"feature {feature_id} response is not an object")
        return canonical_feature(response)

    def fetch_snapshot(self) -> ChromeStatusSnapshot:
        trials = self.fetch_trials()
        active_trials = [trial for trial in trials if trial.get("status") == "ACTIVE"]
        feature_ids = sorted(
            {
                str(trial["chromestatus_feature_id"])
                for trial in active_trials
                if trial.get("chromestatus_feature_id")
            }
        )
        features: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(
            max_workers=self.config.feature_fetch_workers,
            thread_name_prefix="chromestatus",
        ) as executor:
            futures = {
                executor.submit(self.fetch_feature, feature_id): feature_id
                for feature_id in feature_ids
            }
            for future in as_completed(futures):
                feature_id = futures[future]
                try:
                    features[feature_id] = future.result()
                except Exception as exc:  # Keep the remaining inventory usable.
                    errors[feature_id] = str(exc)
        return ChromeStatusSnapshot(
            trials=trials,
            active_trials=active_trials,
            features=features,
            feature_errors=errors,
        )
