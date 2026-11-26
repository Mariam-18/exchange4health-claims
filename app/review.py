"""Turn checker assessments into a review queue: leading issues, fixes, actions and rule help.

No interface code lives here, so the logic can be tested from the command line.
"""

from __future__ import annotations

import csv
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from checker.claim_checker import (
    filing_limit,
    whole_hour_rule,
    Claim,
    load_evidence,
    parse_date,
    predict_pending_assessments,
    read_rows,
)
from drafter.claim_drafter import SERVICE_RATES, construct

PAYER_NAMES = {"PY01": "Northwind", "PY02": "Harbor", "PY03": "Summit"}
SERVICE_NAMES = {"T1019": "personal care", "T1005": "respite", "S5125": "attendant care"}

ISSUE_LABELS = {
    "D01": "No verified visit",
    "D02": "Authorization used up",
    "D03": "Late filing",
    "D04": "Wrong modifier",
    "D05": "Duplicate claim",
    "D06": "Harbor daily cap",
    "D07": "Missing authorization number",
    "D08": "Wrong authorization",
    "D09": "Units over verified time",
    "D10": "Summit whole hours",
}

ISSUE_HELP = {
    "D01": "Payers only pay for care backed by a verified EVV visit for the same patient, service and date. A visit edited by hand stays pending until the payer's aggregator accepts it, and rejected visits don't count.",
    "D02": "Each paid claim draws its units down from the authorization, in the order claims are submitted. Once the units run out, further claims are denied until a new authorization is issued.",
    "D03": "Most payers only accept claims received within a set number of days after the date of service. Northwind allows 30 days, or 45 for dates of service from 1 August 2026. Summit allows 60. Harbor has no limit for in-state home care providers.",
    "D04": "A modifier is a two-character code added to the service code to give the payer extra detail. Harbor requires U2 on personal care, and U3 on Saturdays and Sundays for dates of service from 1 September 2026.",
    "D05": "A new claim for the same patient, service, date and payer as one already submitted is a duplicate, whatever happened to the first. To correct an earlier claim, submit a corrected claim (frequency code 7) that names the original.",
    "D06": "Harbor pays at most 32 units (8 hours) per patient per day, across all services. A single 9-hour visit is most likely an 8-hour shift with an unpaid break inside it, so it's reduced in one click. Other cases involve several visits or claims that day and need checking.",
    "D07": "The claim must quote a prior authorization for the same patient and service, in force on the date of service. When exactly one authorization on file matches, it's added in one click.",
    "D08": "The claim must quote a prior authorization for the same patient and service, in force on the date of service. When exactly one authorization on file matches, it replaces the wrong one in one click.",
    "D09": "Billed units can't exceed the verified visit time, converted to 15-minute units using the payer's rounding rule. Gaps under 30 minutes are almost always rounding, so they're corrected in one click.",
    "D10": "Summit pays home care in whole hours (multiples of 4 units) for dates of service from 1 July 2026. The fix rounds down to the largest whole number of hours that both the bill and the verified visit cover.",
}

VISIT_STATUS_FILTERS = {
    "locate_missing_visit": "No visit found",
    "review_rejected_visit": "Rejected",
    "resolve_pending_verification": "Pending manual verification",
    "review_visit_statuses": "Mixed statuses",
}

UNIT_RULES = {
    "PY01": (
        "Northwind bills in 15-minute units and follows the 8-minute rule: after the last full "
        "unit, 8 or more remaining minutes count as one more unit; 7 or fewer don't."
    ),
    "PY02": "Harbor only pays complete 15-minute units of verified time. Partial units aren't billable.",
    "PY03": "Summit only pays complete 15-minute units of verified time. Partial units aren't billable.",
}


@dataclass
class Choice:
    """A decision the biller makes, e.g. 'Submit as a correction'. remove=True drops the claim."""
    label: str
    change: dict = field(default_factory=dict)
    remove: bool = False


@dataclass
class Finding:
    code: str
    action: str
    auto: bool
    message: str
    next_step: str
    help: str
    rule_reference: str
    evidence: dict
    change: dict = field(default_factory=dict)
    choices: list[Choice] = field(default_factory=list)
    fix_text: str = ""  # e.g. "Change units from 20 to 19."
    contact: str = ""  # "payer" or "coordinator" (the agency's EVV coordinator) when someone needs contacting

    @property
    def label(self) -> str:
        return ISSUE_LABELS.get(self.code, self.code)


@dataclass
class ReviewItem:
    claim_id: str
    row: dict
    assessment: dict
    findings: list[Finding]

    @property
    def needs_review(self) -> bool:
        return self.assessment["status"] == "needs_review"

    @property
    def review_reasons(self) -> list[str]:
        return self.assessment.get("review_reasons", [])

    @property
    def leading(self) -> Finding | None:
        """The first finding that needs a person, else the first finding (checker priority order)."""
        human = [f for f in self.findings if not f.auto]
        return (human or self.findings or [None])[0]

    @property
    def issue(self) -> str:
        return self.leading.code if self.leading else "review"

    @property
    def auto_only(self) -> bool:
        return not self.review_reasons and bool(self.findings) and all(f.auto for f in self.findings)

    @property
    def payer_name(self) -> str:
        return PAYER_NAMES.get(self.assessment["payer"], self.row["payer"])

    @property
    def billed_amount(self) -> float:
        return float(self.row["billed_amount"] or 0)

    @property
    def days_to_deadline(self) -> int | None:
        """Days between planned submission and the filing deadline. None if the payer has no limit."""
        claim = Claim.from_row(self.row)
        deadline = filing_deadline(claim)
        return None if deadline is None else (deadline - claim.submission_date).days

    def auto_change(self) -> dict:
        """Combine every automatic finding's field changes into one set of claim edits."""
        return merge_changes(f for f in self.findings if f.auto)


def merge_changes(findings) -> dict:
    """Merge the field changes of several findings. Where two change units, keep the lower value."""
    merged = {}
    for f in findings:
        for key, value in f.change.items():
            merged[key] = min(merged[key], value) if key == "units" and key in merged else value
    return merged


def filing_deadline(claim: Claim) -> date | None:
    """Return the payer's filing deadline for the claim, or None when no limit applies."""
    limit = filing_limit(claim.payer, claim.service_date)
    return None if limit is None else claim.service_date + timedelta(days=limit[0])


def long_date(value: str) -> str:
    """Format an ISO date as e.g. 'Sat 12 September 2026'."""
    d = date.fromisoformat(value)
    return f"{d:%a} {d.day} {d:%B %Y}"


def minutes(units: int) -> str:
    return f"{units * 15} min"


def hours(units: int) -> str:
    return f"{units / 4:g} h"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe(code: str, detail: dict, row: dict, payer: str) -> Finding:
    """Build the message, fix, next step, rule help and any choices for one finding.

    Inputs: denial code, the checker's finding detail, the claim row, the payer ID.
    Output: a Finding. auto=True means the change has exactly one correct answer.
    Help text gives the general rule first, then what applies to this claim.
    """
    ev, action = detail["evidence"], detail["suggested_action"]
    payer_name = PAYER_NAMES.get(payer, payer)
    service_date = parse_date(row["date_of_service"]).isoformat()
    f = Finding(code, action, False, "", "", "", detail["rule_reference"], ev)

    if code == "D01":
        visits = ev["matching_visits"]
        ids = ", ".join(v["visit_id"] for v in visits)
        f.contact = "coordinator"
        if action == "locate_missing_visit":
            f.message = "No EVV visit was recorded for this patient, service and date."
            f.next_step = "Check with the caregiver whether the visit happened and was clocked under another date or service."
            specific = "No visit at all was found for this claim."
        elif action == "review_rejected_visit":
            f.message = f"The EVV visit for this date ({ids}) was rejected."
            f.next_step = "Ask the EVV coordinator why it was rejected and whether it can be corrected."
            specific = f"This claim's visit {ids} was rejected, so it doesn't count."
        elif action == "resolve_pending_verification":
            f.message = f"The EVV visit for this date ({ids}) was edited by hand and is awaiting verification."
            f.next_step = "Hold the claim until the payer's aggregator accepts the edit."
            specific = f"This claim's visit {ids} is still pending."
        else:
            statuses = ", ".join(f"{v['visit_id']} {v['verification_status']}" for v in visits)
            f.message = f"None of the visits for this date are verified ({statuses})."
            f.next_step = "Resolve the visit verification before submitting."
            specific = "None of this claim's visits are verified yet."
        f.help = (
            "Payers only pay for care backed by a verified EVV visit for the same patient, service "
            "and date. A visit edited by hand stays pending until the payer's aggregator accepts it, "
            f"and rejected visits don't count. {specific}"
        )

    elif code == "D02":
        remaining = max(0, ev["remaining_units"])
        f.message = (
            f"The claim bills {ev['requested_units']} units, but {ev['auth_id']} has "
            f"{plural(remaining, 'unit')} left. Shortfall: {plural(ev['shortfall_units'], 'unit')}."
        )
        f.next_step = f"Request a new or extended authorization from {payer_name}"
        f.next_step += f", or bill {remaining} units now and the rest once it's issued." if remaining else "."
        f.contact = "payer"
        f.help = (
            "Each paid claim draws its units down from the authorization, in the order claims are "
            "submitted. Once the units run out, further claims are denied until a new authorization "
            f"is issued. {ev['auth_id']} approved {ev['authorized_units']} units: "
            f"{ev['recorded_paid_units']} are already paid and {ev['projected_paid_units']} are "
            "expected to be paid on pending claims submitted before this one."
        )

    elif code == "D03":
        f.message = (
            f"Filing window: {ev['filing_limit_days']} days. Deadline: {long_date(ev['filing_deadline'])}. "
            f"Planned submission: {plural(ev['days_late'], 'day')} late."
        )
        f.next_step = f"Ask {payer_name} whether a late-filing exception applies."
        f.contact = "payer"
        if payer == "PY01" and ev["filing_limit_days"] == 45:
            specific = "For this claim, Northwind's 45-day limit applies, in force for dates of service from 1 August 2026."
        elif payer == "PY01":
            specific = "For this claim, Northwind's 30-day limit applies, for dates of service before 1 August 2026."
        else:
            specific = f"For this claim, {payer_name}'s {ev['filing_limit_days']}-day limit applies."
        f.help = (
            "Most payers only accept claims received within a set number of days after the date of "
            f"service. {specific}"
        )

    elif code == "D04":
        submitted = ev["submitted_modifier"] or "none"
        expected = ev["expected_modifier"]
        weekend = date.fromisoformat(service_date).weekday() >= 5
        f.auto, f.change = True, {"modifier": expected}
        f.message = f"The claim has modifier {submitted}; Harbor requires {expected} for this visit."
        f.fix_text = f"Change modifier from {submitted} to {expected}."
        f.help = (
            "A modifier is a two-character code added to the service code to give the payer extra "
            "detail. Harbor requires U2 on personal care, and U3 on Saturdays and Sundays for dates "
            f"of service from 1 September 2026. This visit was on {long_date(service_date)}"
            f"{', a weekend' if weekend else ''}, so it needs {expected}."
        )

    elif code == "D05":
        matches = ev["matching_submissions"]
        prior = "; ".join(
            f"{m['claim_id']}, submitted {long_date(m['submission_date'])} ({m['recorded_status'] or 'pending'})"
            for m in matches
        )
        original = matches[-1]["claim_id"]
        f.message = f"A claim for the same patient, service, date and payer already exists: {prior}."
        f.choices = [
            Choice(f"Submit as a correction of {original}",
                   {"frequency_code": "7", "original_claim_id": original}),
            Choice("Remove from batch", remove=True),
        ]
        f.help = (
            "A new claim for the same patient, service, date and payer as one already submitted is a "
            "duplicate, whatever happened to the first. To correct an earlier claim, submit a "
            f"corrected claim (frequency code 7) that names the original. Here, the original is {original}."
        )

    elif code == "D06":
        billed, proposed, already = ev["billed_units"], ev["proposed_units"], ev["already_counted_units"]
        if billed == 36 and proposed == 32 and already == 0:
            f.auto, f.change = True, {"units": 32}
            f.message = f"The claim bills 36 units ({hours(36)}); Harbor pays at most 32 ({hours(32)}) per patient per day."
            f.fix_text = "Change units from 36 to 32."
            f.next_step = "Follow-up: the 9-hour visit may include an unpaid break. Ask the caregiver to clock out for breaks."
        else:
            f.message = (
                f"The claim bills {billed} units ({hours(billed)}), and {already} units ({hours(already)}) "
                f"are already billed for this patient that day. Harbor pays at most 32 per day, so "
                f"{plural(proposed or 0, 'unit')} would be paid."
            )
            f.next_step = "Check the day's visits with the EVV coordinator before reducing."
            f.contact = "coordinator"
        f.help = (
            "Harbor pays at most 32 units (8 hours) per patient per day, across all services. "
            + ("A single 9-hour visit is most likely an 8-hour shift with an unpaid break inside it."
               if f.auto else
               "This patient has several visits or claims that day, so check which hours to bill.")
        )

    elif code in ("D07", "D08"):
        candidates = ev["candidate_auth_ids"]
        if action in ("apply_matching_authorization", "replace_authorization"):
            new = candidates[0]
            f.auto, f.change = True, {"auth_id": new}
            if code == "D07":
                f.message = f"The claim has no authorization number. {new} is the only authorization for this patient, service and date."
                f.fix_text = f"Add authorization {new}."
            else:
                cover = ev["unique_candidate_coverage"]
                f.message = (
                    f"{ev['submitted_auth_id']} covers {long_date(ev['authorization_start_date'])} to "
                    f"{long_date(ev['authorization_end_date'])}, which doesn't include the date of service. "
                    f"{new} covers {long_date(cover['start_date'])} to {long_date(cover['end_date'])}."
                )
                f.fix_text = f"Change authorization from {ev['submitted_auth_id']} to {new}."
            specific = f"{new} is the only one on file that matches."
        else:
            found = ", ".join(candidates) if candidates else "none"
            f.message = f"No single authorization matches this patient, service and date. Candidates: {found}."
            f.next_step = f"Confirm the right authorization with {payer_name}, or request one."
            f.contact = "payer"
            specific = "None on file matches uniquely."
        f.help = (
            "The claim must quote a prior authorization for the same patient and service, in force "
            f"on the date of service. {specific}"
        )

    elif code == "D09":
        verified, billed, by_rules = ev["verified_minutes"], ev["billed_units"], ev["supported_units"]
        f.message = (
            f"The verified visit totals {verified:g} minutes. By {payer_name}'s rules, that's "
            f"{plural(by_rules, 'unit')}. The claim bills {billed} units ({billed * 15} minutes), "
            f"{ev['gap_vs_evv_minutes']:g} minutes more than verified."
        )
        if action == "check_rounding":
            new_units = by_rules
            f.fix_text = f"Change units from {billed} to {by_rules}."
            if whole_hour_rule(payer, date.fromisoformat(service_date)):
                new_units = by_rules // 4 * 4
                if new_units != by_rules:
                    f.fix_text = (f"Change units from {billed} to {new_units}: {by_rules} by Summit's "
                                  "rounding, then down to whole hours.")
            f.auto, f.change = new_units > 0, {"units": new_units}
            f.next_step = "Follow-up: units were corrected to the payer's rounding rule."
        else:
            f.next_step = "The gap is too large to be rounding. Ask the EVV coordinator whether time is missing from EVV or the units are wrong."
            f.contact = "coordinator"
        f.help = (
            "Billed units can't exceed the verified visit time, converted to 15-minute units using "
            f"the payer's rounding rule. {UNIT_RULES[payer]} Gaps under 30 minutes are almost always "
            "rounding, so they're corrected automatically."
        )

    elif code == "D10":
        billed, evv_units = ev["billed_units"], ev["evv_supported_units"]
        whole = (min(billed, evv_units) // 4) * 4 if evv_units is not None else 0
        f.message = f"The claim bills {billed} units ({hours(billed)}), which isn't a whole number of hours."
        if whole > 0:
            f.auto, f.change = True, {"units": whole}
            f.fix_text = f"Change units from {billed} to {whole} ({hours(whole)})."
            f.next_step = "Follow-up: units were rounded down to whole hours."
        else:
            f.message += " The verified visit is shorter than an hour."
            f.next_step = "Confirm the visit with the EVV coordinator."
            f.contact = "coordinator"
        f.help = (
            "Summit pays home care in whole hours (multiples of 4 units) for dates of service from "
            "1 July 2026. The fix rounds down to the largest whole number of hours that both the "
            "bill and the verified visit cover."
        )

    if code in ("D05", "D10") or (code == "D06" and not f.auto):
        f.help = ""
    return f


def build_queue(assessments: list[dict], rows: dict[str, dict]) -> list[ReviewItem]:
    """Combine assessments with their claim rows and describe every finding."""
    items = []
    for a in assessments:
        row = rows[a["claim_id"]]
        findings = [describe(c, a["finding_details"][c], row, a["payer"]) for c in a["failed_checks"]]
        items.append(ReviewItem(a["claim_id"], row, a, findings))
    return items


def sort_key(item: ReviewItem, by: str):
    """Sort by 'deadline' (fewest days left first, then amount) or 'amount' (largest first)."""
    days = item.days_to_deadline
    days = days if days is not None else 10**6
    return (days, -item.billed_amount) if by == "deadline" else (-item.billed_amount, days)


def apply_changes(row: dict, change: dict) -> dict:
    """Return a copy of the claim row with the changes applied and the amount recalculated."""
    updated = dict(row)
    for key, value in change.items():
        updated[key] = str(value)
    if "units" in change:
        rate = SERVICE_RATES[updated["service_code"].strip().upper()]
        updated["billed_amount"] = f"{rate * int(updated['units']):.2f}"
    return updated


def contact_message(item: ReviewItem, finding: Finding) -> str:
    """Draft a short message to the payer or agency about one finding."""
    r = item.row
    service = SERVICE_NAMES.get(r["service_code"].strip().upper(), r["service_code"])
    to = item.payer_name if finding.contact == "payer" else "EVV coordinator"
    return (
        f"To: {to}\n"
        f"Re: claim {item.claim_id}, patient {r['patient_id']}, {service} on "
        f"{parse_date(r['date_of_service']).isoformat()}\n\n"
        f"{finding.message}\n{finding.next_step}"
    )


def assess(data_dir: Path, rows: list[dict], *, with_history: bool = True) -> list[dict]:
    """Run the checker on the given pending rows, keeping the other data files unchanged.

    with_history=False replays the rows on their own, as if they replaced the recorded claims.
    """
    if not rows:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for name in ("authorizations.csv", "evv_visits.csv"):
            shutil.copy(data_dir / name, tmp_dir / name)
        if with_history:
            shutil.copy(data_dir / "claims_history.csv", tmp_dir / "claims_history.csv")
        else:
            header = (data_dir / "claims_history.csv").read_text(encoding="utf-8-sig").splitlines()[0]
            (tmp_dir / "claims_history.csv").write_text(header + "\n", encoding="utf-8")
        with (tmp_dir / "claims_to_check.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return predict_pending_assessments(tmp_dir)


def load_pending(data_dir: Path) -> list[dict]:
    """Return the rows of claims_to_check.csv in file order."""
    return read_rows(data_dir / "claims_to_check.csv")


def build_drafts(data_dir: Path, agency: str, last_service_date: date, *, block_duplicates: bool = True):
    """Return draft rows and skipped visit groups for one agency through the given date.

    Set block_duplicates to False for the historical replacement replay.
    """
    history = read_rows(data_dir / "claims_history.csv")
    agencies = {}
    for row in history:
        agencies.setdefault(row["patient_id"], set()).add(row["agency_id"])
    authorizations, verified_seconds = load_evidence(data_dir)
    submitted = (Claim.from_row(row) for row in history) if block_duplicates else ()
    drafts, skipped, _ = construct(
        data_dir, authorizations, verified_seconds, agencies, submitted_claims=submitted,
    )
    mine = [d for d in drafts if d.agency == agency and d.claim.service_date <= last_service_date]
    mine.sort(key=lambda d: (d.claim.submission_date, d.claim.service_date, d.claim.patient_id, d.claim.service_code))
    rows = []
    for n, d in enumerate(mine, 1):
        c = d.claim
        rows.append({
            "claim_id": f"DRAFT-{n:04d}", "agency_id": d.agency, "payer": c.payer,
            "patient_id": c.patient_id, "service_code": c.service_code,
            "date_of_service": c.service_date.isoformat(), "submitted_date": c.submission_date.isoformat(),
            "units": str(c.units), "auth_id": c.auth_id, "modifier": c.modifier,
            "frequency_code": "1", "original_claim_id": "", "billed_amount": f"{d.billed_amount:.2f}",
            "visit_ids": "|".join(d.visit_ids),
        })
    failed = [
        (patient, service, service_date, reason)
        for (patient, service, service_date), owner, reason in skipped
        if owner in ("", agency) and service_date <= last_service_date
    ]
    return rows, failed
