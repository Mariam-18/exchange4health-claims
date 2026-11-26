"""Rule changes: the rules in force for the app, proposed changes, their impact, and approvals.

A proposed change is a dict with:
    rule_type: "filing_limit" | "weekend_modifier" | "whole_hour_units"
    payer: "PY01" | "PY02" | "PY03"
    days (filing_limit), service_code and modifier (weekend_modifier)
    effective_from, effective_to: ISO dates or None
    source: where the rule comes from (bulletin name, or "Inferred from ...")
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from app.activity import STATE_DIR, now
from checker.claim_checker import load_rules, replay_all, rules_in_force

APP_RULES_FILE = STATE_DIR / "rule_settings.json"
CHANGES_FILE = STATE_DIR / "rule_changes.jsonl"
BULLETIN_STATUS_FILE = STATE_DIR / "bulletin_status.json"
PASTED_DIR = STATE_DIR / "bulletins"

RULE_TYPES = {
    "filing_limit": "Claims must be received within N calendar days of the date of service, for dates of service in a period.",
    "weekend_modifier": "A service delivered on a Saturday or Sunday must carry a specific modifier, for dates of service in a period.",
    "whole_hour_units": "Units must be a multiple of 4 (whole hours), for dates of service in a period.",
}
PAYERS = {"PY01": "Northwind Health Plan", "PY02": "Harbor Medicaid Plan", "PY03": "Summit Care Partners"}


# ---------- rules in force ----------

def current_rules() -> dict:
    """Rules the app applies: the approved app copy if one exists, else the repository default."""
    if APP_RULES_FILE.exists():
        return json.loads(APP_RULES_FILE.read_text(encoding="utf-8"))
    return load_rules()


def save_rules(rules: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    APP_RULES_FILE.write_text(json.dumps(rules, indent=2), encoding="utf-8")


def reset_rules() -> None:
    """Go back to the repository's rule settings and forget demo state."""
    for path in (APP_RULES_FILE, BULLETIN_STATUS_FILE):
        if path.exists():
            path.unlink()


def remove_rules(rules: dict, section: str, **match) -> dict:
    """Return a copy of the rules without the entries in a section that match all given fields."""
    updated = copy.deepcopy(rules)
    updated[section] = [e for e in updated.get(section, []) if any(e.get(k) != v for k, v in match.items())]
    return updated


def describe_rule(section: str, entry: dict) -> str:
    """Plain-language description of one rule entry."""
    payer = PAYERS.get(entry["payer"], entry["payer"])
    period = describe_period(entry.get("from"), entry.get("to"))
    if section == "filing_limits":
        return f"{payer}: claims within {entry['days']} days of the date of service{period}."
    if section == "weekend_modifiers":
        return f"{payer}: {entry['service_code']} on Saturdays and Sundays needs modifier {entry['modifier']}{period}."
    return f"{payer}: units must be whole hours (multiples of 4){period}."


def describe_period(start: str | None, end: str | None) -> str:
    if start and end:
        return f", for dates of service {start} to {end}"
    if start:
        return f", for dates of service from {start}"
    if end:
        return f", for dates of service up to {end}"
    return ""


RULE_SECTIONS = {
    "filing_limit": "filing_limits",
    "weekend_modifier": "weekend_modifiers",
    "whole_hour_units": "whole_hour_units",
}


def rule_entry(change: dict) -> dict:
    """Convert a proposed change to a stored rule."""
    entry = {"payer": change["payer"], "from": change.get("effective_from"),
             "to": change.get("effective_to"), "source": change.get("source", "")}
    if change["rule_type"] == "filing_limit":
        entry["days"] = change.get("days")
    elif change["rule_type"] == "weekend_modifier":
        entry.update(service_code=change.get("service_code"), modifier=change.get("modifier"))
    return entry


def describe_change(change: dict) -> str:
    return describe_rule(RULE_SECTIONS[change["rule_type"]], rule_entry(change))


# ---------- applying a change ----------

def validate_change(change: dict, source_text: str | None = None, quotes: list[str] = ()) -> list[str]:
    """Return problems with a proposed change (empty list if it can be applied).

    If source_text is given, every quote must appear in it (ignoring case, spacing and markdown).
    """
    problems = []
    if change.get("rule_type") not in RULE_TYPES:
        problems.append(f"Unknown rule type: {change.get('rule_type')}")
    if change.get("payer") not in PAYERS:
        problems.append(f"Unknown payer: {change.get('payer')}")
    for key in ("effective_from", "effective_to"):
        if change.get(key):
            try:
                date.fromisoformat(change[key])
            except ValueError:
                problems.append(f"{key} is not a date: {change[key]}")
    start, end = change.get("effective_from"), change.get("effective_to")
    if not problems and start and end and date.fromisoformat(end) < date.fromisoformat(start):
        problems.append("The last service date must be on or after the first service date.")
    if change.get("rule_type") == "filing_limit" and not (isinstance(change.get("days"), int) and 0 < change["days"] <= 365):
        problems.append("Filing limit days must be a whole number from 1 to 365.")
    if change.get("rule_type") == "weekend_modifier":
        mod = change.get("modifier") or ""
        if not (len(mod) == 2 and mod.isalnum() and mod.isupper()):
            problems.append(f"Modifier must be two capital letters or digits: {mod!r}")
        if (change.get("payer"), change.get("service_code")) != ("PY02", "T1019"):
            problems.append("Weekend modifier rules are supported for Harbor personal care (T1019).")
    if change.get("rule_type") == "whole_hour_units" and change.get("payer") != "PY03":
        problems.append("Whole-hour rules are supported for Summit.")
    if source_text is not None:
        haystack = normalise(source_text)
        if not quotes:
            problems.append("No supporting quote from the bulletin.")
        for quote in quotes:
            if normalise(quote) not in haystack:
                problems.append(f"Quote not found in the bulletin: \"{quote}\"")
    return problems


def normalise(text: str) -> str:
    return " ".join(text.replace("*", "").replace("_", " ").lower().split())


def rule_dates(entry: dict) -> tuple[date, date]:
    """Return the inclusive bounds of a rule's service dates."""
    start, end = entry.get("from"), entry.get("to")
    return date.fromisoformat(start) if start else date.min, date.fromisoformat(end) if end else date.max


def overridden_rules(rules: dict, change: dict) -> list[dict]:
    """Return existing rules that the proposal replaces within the overlapping dates."""
    section = RULE_SECTIONS[change["rule_type"]]
    start, end = rule_dates(rule_entry(change))
    replaced = []
    for old in rules.get(section, []):
        if old["payer"] != change["payer"]:
            continue
        if section == "weekend_modifiers" and old["service_code"] != change.get("service_code"):
            continue
        old_start, old_end = rule_dates(old)
        overlap_start, overlap_end = max(start, old_start), min(end, old_end)
        if overlap_start <= overlap_end:
            replaced.append({
                "rule": old,
                "from": overlap_start.isoformat() if overlap_start != date.min else None,
                "to": overlap_end.isoformat() if overlap_end != date.max else None,
            })
    return replaced


def apply_change(rules: dict, change: dict) -> dict:
    """Apply a rule on its service dates and retain existing rules outside that period."""
    section = RULE_SECTIONS[change["rule_type"]]
    new_entry = rule_entry(change)
    start, end = rule_dates(new_entry)
    replaced = [item["rule"] for item in overridden_rules(rules, change)]
    updated = copy.deepcopy(rules)
    entries = []
    for old in updated.get(section, []):
        if old not in replaced:
            entries.append(old)
            continue
        old_start, old_end = rule_dates(old)
        if old_start < start:
            entries.append({**old, "to": (start - timedelta(days=1)).isoformat()})
        if end < old_end:
            entries.append({**old, "from": (end + timedelta(days=1)).isoformat()})
    updated[section] = [*entries, new_entry]
    return updated


# ---------- impact ----------

def replay_with(data_dir: Path, rules: dict):
    """Replay history and pending claims under the given rules."""
    with rules_in_force(rules):
        history, pending, _ = replay_all(data_dir)
    return {r["claim_id"]: r for r in history}, {r["claim_id"]: r for r in pending}


def impact(data_dir: Path, current: dict, proposed: dict, pending_agencies: dict[str, str]) -> dict:
    """Compare predictions under the current and proposed rules.

    Output: history accuracy before/after, changed historical and pending predictions,
    and the number of changed pending claims per agency.
    """
    h0, p0 = replay_with(data_dir, current)
    h1, p1 = replay_with(data_dir, proposed)

    def outcome(r):
        return r["predicted_status"], r["predicted_denial_code"]

    def correct(h):
        return sum(r["actual_status"] == r["predicted_status"] for r in h.values())

    def code_correct(h):
        return sum(r["actual_status"] == "denied" and r["actual_denial_code"] == r["predicted_denial_code"]
                   for r in h.values())

    history_changes = [
        {"claim_id": k, "before": h0[k]["predicted_denial_code"] or "paid",
         "after": h1[k]["predicted_denial_code"] or "paid",
         "payer_said": h0[k]["actual_denial_code"] or h0[k]["actual_status"]}
        for k in h0 if outcome(h0[k]) != outcome(h1[k])
    ]
    pending_changes = [
        {"claim_id": k, "agency": pending_agencies.get(k, ""), "before": p0[k]["predicted_denial_code"] or "paid",
         "after": p1[k]["predicted_denial_code"] or "paid"}
        for k in p0 if outcome(p0[k]) != outcome(p1[k])
    ]
    return {
        "history_total": len(h0),
        "history_correct_before": correct(h0), "history_correct_after": correct(h1),
        "code_correct_before": code_correct(h0), "code_correct_after": code_correct(h1),
        "history_changes": history_changes,
        "history_changes_matching_payer": sum(c["after"] == c["payer_said"] for c in history_changes),
        "pending_changes": pending_changes,
        "pending_by_agency": dict(Counter(c["agency"] for c in pending_changes)),
    }


# ---------- approvals and notifications ----------

def record_change(kind: str, title: str, change: dict, result: dict, decision: str, note: str = "") -> None:
    """Log a decision on a proposed rule change (approved, rejected, dismissed or sent to the payer)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with CHANGES_FILE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "timestamp": now(), "kind": kind, "title": title, "decision": decision, "note": note,
            "change": change, "summary": describe_change(change) if change else "",
            "pending_by_agency": result.get("pending_by_agency", {}),
        }) + "\n")


def read_changes() -> list[dict]:
    if not CHANGES_FILE.exists():
        return []
    with CHANGES_FILE.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def clear_changes() -> None:
    if CHANGES_FILE.exists():
        CHANGES_FILE.unlink()


def approve(kind: str, title: str, change: dict, result: dict, note: str = "") -> None:
    """Apply an approved change to the app's rules and log it."""
    save_rules(apply_change(current_rules(), change))
    record_change(kind, title, change, result, "approved", note)


# ---------- bulletins ----------

def list_bulletins(data_dir: Path) -> list[dict]:
    """Bulletins from the data pack plus any pasted in the app, with their review status."""
    status = bulletin_status()
    found = []
    for path in sorted((data_dir / "payers").glob("*bulletin*.md")) + sorted(PASTED_DIR.glob("*.md")):
        found.append({"name": path.name, "path": path, "text": path.read_text(encoding="utf-8"),
                      "status": status.get(path.name, "in rules" if path.parent.name == "payers" else "new")})
    return found


def bulletin_status() -> dict:
    if not BULLETIN_STATUS_FILE.exists():
        return {}
    return json.loads(BULLETIN_STATUS_FILE.read_text(encoding="utf-8"))


def set_bulletin_status(name: str, status: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BULLETIN_STATUS_FILE.write_text(json.dumps({**bulletin_status(), name: status}, indent=1), encoding="utf-8")


def save_pasted_bulletin(title: str, text: str) -> str:
    PASTED_DIR.mkdir(parents=True, exist_ok=True)
    slug = "".join(ch if ch.isalnum() else "_" for ch in title.lower()).strip("_")[:60] or "bulletin"
    name = f"pasted_{slug}.md"
    (PASTED_DIR / name).write_text(text, encoding="utf-8")
    return name
