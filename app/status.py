"""Submitted claims and their outcomes: sorting denials into groups, and preparing corrected claims.

"Today" is the last submission date in the claims history, because the data has no current date.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app import activity
from app.review import ReviewItem, apply_changes, assess, build_queue, filing_deadline, merge_changes
from checker.claim_checker import Claim, read_rows

RESUBMISSIONS_FILE = activity.STATE_DIR / "resubmissions.jsonl"
CLAIM_COLUMNS = ["claim_id", "agency_id", "payer", "patient_id", "service_code", "date_of_service",
                 "submitted_date", "units", "auth_id", "modifier", "frequency_code", "original_claim_id",
                 "billed_amount"]
GROUPS = {
    "quick": "Quick fix & resubmit",
    "person": "Needs someone",
    "duplicate": "Duplicates",
    "unexplained": "Unexplained denials",
}


@dataclass
class DeniedClaim:
    item: ReviewItem
    group: str
    payer_code: str
    remark: str

    @property
    def claim_id(self) -> str:
        return self.item.claim_id

    @property
    def row(self) -> dict:
        return self.item.row


def history_rows(data_dir: Path) -> dict[str, dict]:
    """Past claims by ID (duplicate export rows removed)."""
    rows = {}
    for row in read_rows(data_dir / "claims_history.csv"):
        rows.setdefault(row["claim_id"], row)
    return rows


def today(rows: dict[str, dict]) -> date:
    """The latest submission date in the history, used as today."""
    return max(Claim.from_row(r).submission_date for r in rows.values())


def denied_claims(rows: dict[str, dict], assessments: dict, unexpected: set[str], agency: str) -> list[DeniedClaim]:
    """The agency's denied claims, each in one group.

    Order of precedence: duplicates (payer said D05), unexplained (the checker didn't predict the payer's code),
    quick fix (every issue the checker found has a one-click fix), needs someone (the rest).
    """
    denied = {cid: r for cid, r in rows.items() if r["agency_id"] == agency and r["status"] == "denied"}
    items = build_queue([assessments[cid] for cid in denied], denied)
    out = []
    for item in items:
        r = item.row
        if r["denial_code"] == "D05":
            group = "duplicate"
        elif item.claim_id in unexpected:
            group = "unexplained"
        elif item.auto_only:
            group = "quick"
        else:
            group = "person"
        out.append(DeniedClaim(item, group, r["denial_code"], r["remark"]))
    return out


def correction(item: ReviewItem, as_of: date) -> dict:
    """A corrected claim (frequency code 7) with every one-click fix applied, submitted on as_of."""
    fixed = apply_changes({k: item.row[k] for k in CLAIM_COLUMNS}, merge_changes(f for f in item.findings if f.auto))
    return {**fixed, "claim_id": f"{item.claim_id}-R", "frequency_code": "7",
            "original_claim_id": item.claim_id, "submitted_date": as_of.isoformat()}


def deadline_status(item: ReviewItem, as_of: date) -> tuple[str, bool]:
    """Plain-language filing deadline for a resubmission on as_of, and whether it has passed."""
    deadline = filing_deadline(Claim.from_row(item.row))
    if deadline is None:
        return "No filing limit", False
    passed = as_of > deadline
    return (f"Deadline passed {deadline.isoformat()}" if passed else f"Resubmit by {deadline.isoformat()}"), passed


def check_corrections(data_dir: Path, corrections: list[dict]) -> dict[str, dict]:
    """Run the checker on corrected claims against the full history. Returns assessments by corrected claim ID."""
    if not corrections:
        return {}
    return {a["claim_id"]: a for a in assess(data_dir, corrections)}


def save_resubmissions(entries: list[dict]) -> None:
    activity.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with RESUBMISSIONS_FILE.open("a", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(json.dumps({"timestamp": activity.now(), **entry}) + "\n")


def read_resubmissions(agency: str | None = None) -> list[dict]:
    if not RESUBMISSIONS_FILE.exists():
        return []
    with RESUBMISSIONS_FILE.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    return [r for r in rows if agency is None or r["claim"]["agency_id"] == agency]


def handled(agency: str) -> dict[str, str]:
    """Denied claims already acted on from this page: {claim_id: "resubmitted" | "resolved"}."""
    done = {}
    for e in activity.read_log():
        if e.get("workspace") == "status" and e["agency"] == agency and e["event"] in ("resubmit", "resolved"):
            done[e["claim_id"]] = "resubmitted" if e["event"] == "resubmit" else "resolved"
    return done
