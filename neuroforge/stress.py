"""Observation-gated conference stress test for NeuroForge.

The doctor keeps the pen. This module never diagnoses, prescribes, or scores
clinical truth. It only checks whether the presenter looked, named a limit,
and avoided closing the discussion from a selected-image preview.
"""
from __future__ import annotations

import re
from typing import Any

ERROR_MODES = {
    "observation_skip": "Looked past the image",
    "premature_closure": "Closed the discussion too soon",
    "sequence_mismatch": "Wrong sequence or timepoint",
    "uncertainty_skip": "Did not name the limit",
    "anchoring": "Stuck on the first story",
}

DEFAULT_CONFERENCE_PATH = {
    "required_observation_tokens": ["interval", "change", "flair", "sequence", "limit"],
    "expert_hypothesis": (
        "Interval imaging change that still needs sequence concordance and named "
        "uncertainty before any differential talk."
    ),
    "expert_move": (
        "Show what changed, on which sequence, and what a selected-image preview "
        "cannot settle. Keep a diagnosis off the slide."
    ),
}

_CLINICAL_ACTION = re.compile(
    r"\b(?:prescribe|administer|dose|dosing|tpa|tenecteplase|thrombolysis|start treatment)\b",
    re.IGNORECASE,
)
_CLOSURE = re.compile(
    r"\b(?:definitive|proven|certainly|diagnosis is|this is diagnostic|close the case)\b",
    re.IGNORECASE,
)
_INTERVAL = re.compile(r"\b(?:interval|change|follow-?up|serial|baseline|timepoint)\b", re.IGNORECASE)
_LIMIT = re.compile(r"\b(?:limit|uncertain|missing|incomplete|selected-image)\b", re.IGNORECASE)


def conference_path_for(case: dict[str, Any]) -> dict[str, Any]:
    authored = case.get("conference_path")
    if isinstance(authored, dict) and authored.get("expert_move"):
        tokens = authored.get("required_observation_tokens") or DEFAULT_CONFERENCE_PATH["required_observation_tokens"]
        return {
            "required_observation_tokens": [str(item).lower() for item in tokens if str(item).strip()],
            "expert_hypothesis": str(authored.get("expert_hypothesis") or DEFAULT_CONFERENCE_PATH["expert_hypothesis"]),
            "expert_move": str(authored["expert_move"]),
        }
    return dict(DEFAULT_CONFERENCE_PATH)


def empty_stress_state() -> dict[str, Any]:
    return {
        "schema": "neuroforge_stress_test_v1",
        "observations": [],
        "hypothesis": "",
        "conference_move": "",
        "confidence": None,
        "committed_at": None,
        "runs": [],
    }


def ensure_stress_state(case: dict[str, Any]) -> dict[str, Any]:
    state = case.get("stress_test")
    if not isinstance(state, dict):
        state = empty_stress_state()
        case["stress_test"] = state
        return state
    state.setdefault("schema", "neuroforge_stress_test_v1")
    state.setdefault("observations", [])
    state.setdefault("hypothesis", "")
    state.setdefault("conference_move", "")
    state.setdefault("runs", [])
    return state


def observations_complete(case: dict[str, Any]) -> bool:
    state = case.get("stress_test") or {}
    rows = state.get("observations") or []
    return any(isinstance(item, dict) and str(item.get("finding") or "").strip() for item in rows)


def refuse_clinical_action(text: str) -> bool:
    return bool(_CLINICAL_ACTION.search(text or ""))


def evaluate_commit(
    case: dict[str, Any],
    *,
    hypothesis: str,
    conference_move: str,
    confidence: int,
) -> dict[str, Any]:
    state = ensure_stress_state(case)
    observations = [
        item for item in state.get("observations") or []
        if isinstance(item, dict) and str(item.get("finding") or "").strip()
    ]
    path = conference_path_for(case)
    finding_blob = " ".join(str(item.get("finding") or "") for item in observations)
    modes: list[str] = []
    if not observations:
        modes.append("observation_skip")
    if observations and not _INTERVAL.search(finding_blob):
        modes.append("sequence_mismatch")
    if _CLOSURE.search(hypothesis) or _CLOSURE.search(conference_move):
        modes.append("premature_closure")
    if not _LIMIT.search(f"{conference_move} {hypothesis}"):
        modes.append("uncertainty_skip")
    seen: set[str] = set()
    ordered = []
    for mode in modes:
        if mode in ERROR_MODES and mode not in seen:
            seen.add(mode)
            ordered.append({"error_mode": mode, "error_mode_label": ERROR_MODES[mode]})
    token_hits = [token for token in path["required_observation_tokens"] if token in finding_blob.lower()]
    matched = not ordered
    return {
        "schema": "neuroforge_stress_debrief_v1",
        "matched": matched,
        "overconfident": (not matched) and confidence >= 70,
        "error_modes": ordered,
        "observation_count": len(observations),
        "observation_token_hits": token_hits,
        "first_try_choice": hypothesis,
        "expert_hypothesis": path["expert_hypothesis"],
        "expert_move": path["expert_move"],
        "clinical_use_enabled": False,
    }


def public_stress(case: dict[str, Any]) -> dict[str, Any]:
    state = ensure_stress_state(case)
    last = state["runs"][-1] if state.get("runs") else None
    return {
        "schema": "neuroforge_stress_test_v1",
        "observations": list(state.get("observations") or []),
        "hypothesis": state.get("hypothesis") or "",
        "conference_move": state.get("conference_move") or "",
        "confidence": state.get("confidence"),
        "committed_at": state.get("committed_at"),
        "observation_count": len(state.get("observations") or []),
        "last_debrief": last,
        "clinical_use_enabled": False,
    }
