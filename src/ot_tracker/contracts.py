from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def cross_source_contract_observations(
    trials: Iterable[dict[str, Any]],
    declarations: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare public Chrome Status OT settings with Chromium declarations."""
    declarations_by_trial: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for declaration in declarations:
        trial_name = str(declaration.get("trial_name") or "")
        if trial_name:
            declarations_by_trial[trial_name.casefold()].append(declaration)

    observations: list[dict[str, Any]] = []
    for trial in trials:
        trial_name = str(trial.get("trial_name") or "")
        if not trial_name:
            continue
        matching = declarations_by_trial.get(trial_name.casefold(), [])
        if not matching:
            continue
        chrome_status_value = bool(trial.get("allow_third_party_origins"))
        chromium_value = any(
            declaration.get("fields", {}).get(
                "origin_trial_allows_third_party", False
            )
            is True
            for declaration in matching
        )
        observations.append(
            {
                "trial_name": trial_name,
                "field": "allow_third_party_origins",
                "console": chrome_status_value,
                "chromium": chromium_value,
                "runtime_features": sorted(
                    str(declaration.get("runtime_feature_name") or "")
                    for declaration in matching
                    if declaration.get("runtime_feature_name")
                ),
                "matches": chrome_status_value == chromium_value,
            }
        )

    return sorted(
        observations,
        key=lambda item: (
            str(item["trial_name"]).casefold(),
            str(item["field"]),
        ),
    )


def cross_source_contract_mismatches(
    trials: Iterable[dict[str, Any]],
    declarations: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the stable public report representation for contract mismatches."""
    return [
        {
            "trial_name": observation["trial_name"],
            "field": observation["field"],
            "console": observation["console"],
            "chromium": observation["chromium"],
        }
        for observation in cross_source_contract_observations(trials, declarations)
        if not observation["matches"]
    ]
