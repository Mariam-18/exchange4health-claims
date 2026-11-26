"""Unexpected denials: find them, backtest candidate rules, and package Claude's investigation."""

from __future__ import annotations

from pathlib import Path

from app import ai, analysis
from app.rules_admin import apply_change, impact, validate_change
from checker.claim_checker import read_rows, replay_all, rules_in_force


def history_frame(data_dir: Path, rules: dict):
    """The analysis table of past claims, with predictions under the given rules."""
    with rules_in_force(rules):
        history, _, _ = replay_all(data_dir)
    return analysis.build_frame(data_dir, history)


def backtest(data_dir: Path, rules: dict, group: dict, change: dict) -> dict:
    """Test a candidate rule: how much of the group it explains and what else it changes.

    Output: group claims now predicted with the payer's code, paid claims it would wrongly flag,
    and history accuracy before and after.
    """
    change = {"source": "candidate", **change}
    problems = validate_change(change)
    if problems:
        return {"error": "; ".join(problems)}
    agencies = {r["claim_id"]: r["agency_id"] for r in read_rows(data_dir / "claims_to_check.csv")}
    result = impact(data_dir, rules, apply_change(rules, change), agencies)
    group_ids = set(group["claim_ids"])
    changed = {c["claim_id"]: c for c in result["history_changes"]}
    explained = sum(1 for cid in group_ids if cid in changed and changed[cid]["after"] == group["denial_code"])
    wrongly_flagged = sum(1 for c in result["history_changes"] if c["payer_said"] == "paid" and c["after"] != "paid")
    return {
        "group_claims": len(group_ids), "group_claims_explained": explained,
        "paid_claims_wrongly_flagged": wrongly_flagged,
        "other_predictions_changed": len(result["history_changes"]) - explained,
        "history_correct_before": result["history_correct_before"],
        "history_correct_after": result["history_correct_after"],
        "code_correct_before": result["code_correct_before"], "code_correct_after": result["code_correct_after"],
        "pending_claims_affected": len(result["pending_changes"]),
        "pending_by_agency": result["pending_by_agency"],
    }


def investigate(data_dir: Path, rules: dict, frame, group: dict, refresh: bool = False) -> dict:
    """Claude's investigation of a group, plus Python's own backtest of any rule it proposes."""
    out = ai.investigate(group, data_dir, frame, rules,
                         lambda args: backtest(data_dir, rules, group, args), refresh)
    proposal = out["result"].get("proposed_change")
    if proposal:
        out["backtest"] = backtest(data_dir, rules, group, {k: v for k, v in proposal.items() if k != "explanation"})
    return out
