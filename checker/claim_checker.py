"""Replay the Exchange4Health history with the supplied payer rules.

Run: python -m checker.claim_checker /path/to/data-pack
Optional: --audit history_audit.csv --predictions predictions.csv

The replay uses earlier recorded payments plus earlier pending claims forecast
to pay when reconstructing authorization balances and Harbor daily totals.
History is scored; pending claims get predictions.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import Optional

PAYER_ALIASES = {
    "NORTHWIND HP": "PY01",
    "NORTHWIND HEALTH PLAN": "PY01",
    "HARBOR MEDICAID": "PY02",
    "HARBOR MEDICAID PLAN": "PY02",
    "SUMMIT CARE": "PY03",
    "SUMMIT CARE PARTNERS": "PY03",
}
PAYER_IDS = {"PY01", "PY02", "PY03"}
DENIAL_PRIORITY_BY_PAYER = {
    "PY01": ("D01", "D09", "D02", "D07", "D08", "D03", "D05"),
    "PY02": ("D01", "D05", "D08", "D09", "D04", "D07", "D06", "D02"),
    "PY03": ("D01", "D07", "D08", "D10", "D05", "D09", "D02", "D03"),
}


@dataclass(frozen=True)
class EffectivePeriod:
    start: date
    end: Optional[date] = None

    def includes(self, service_date):
        """Both endpoints are inclusive. A missing end means open-ended."""
        return self.start <= service_date and (self.end is None or service_date <= self.end)


NORTHWIND_FILING_BULLETIN = EffectivePeriod(start=date(2026, 8, 1))
HARBOR_WEEKEND_BULLETIN = EffectivePeriod(start=date(2026, 9, 1))
SUMMIT_WHOLE_HOUR_PERIOD = EffectivePeriod(start=date(2026, 7, 1))


def parse_date(value: str) -> date:
    """Parse a service or submission date from a CSV field."""
    if "/" in value:
        return datetime.strptime(value, "%m/%d/%Y").date()
    return date.fromisoformat(value)


def read_rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV file and return rows keyed by column name."""
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


@dataclass(frozen=True)
class Claim:
    claim_id: str
    payer: str
    patient_id: str
    service_code: str
    service_date: date
    submission_date: date
    units: int
    auth_id: str
    modifier: str
    frequency_code: str
    original_claim_id: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Claim":
        """Create a claim from its normalized CSV field values."""
        payer = row["payer"].strip().upper()
        return cls(
            row["claim_id"], PAYER_ALIASES.get(payer, payer), row["patient_id"],
            row["service_code"].strip().upper(), parse_date(row["date_of_service"]),
            parse_date(row["submitted_date"]), int(row["units"]), row["auth_id"].strip(),
            row["modifier"].strip().upper(), row["frequency_code"],
            row["original_claim_id"],
        )

    @property
    def visit_key(self):
        return self.patient_id, self.service_code, self.service_date

    @property
    def duplicate_key(self):
        return self.payer, *self.visit_key

    @property
    def daily_key(self):
        return self.payer, self.patient_id, self.service_date

    @property
    def authorization_key(self):
        return self.payer, self.auth_id


@dataclass(frozen=True)
class Authorization:
    payer: str
    patient_id: str
    service_code: str
    start_date: date
    end_date: date
    units: int

    def covers(self, claim: Claim) -> bool:
        """Return whether this authorization covers the claim and service date."""
        return (
            self.payer == claim.payer
            and self.patient_id == claim.patient_id
            and self.service_code == claim.service_code
            and self.start_date <= claim.service_date <= self.end_date
        )


class History:
    def __init__(self):
        self.claim_ids = set()
        self.submissions = Counter()
        self.submission_records = defaultdict(list)
        self.authorization_units = Counter()
        self.projected_authorization_units = Counter()
        self.projected_daily_units = Counter()
        self.daily_units = Counter()

    def snapshot(self) -> History:
        """Copy the ledger so a same-date file can advance independently."""
        state = History()
        state.claim_ids = self.claim_ids.copy()
        state.submissions = self.submissions.copy()
        state.submission_records.update({
            key: records.copy() for key, records in self.submission_records.items()
        })
        state.authorization_units = self.authorization_units.copy()
        state.projected_authorization_units = self.projected_authorization_units.copy()
        state.projected_daily_units = self.projected_daily_units.copy()
        state.daily_units = self.daily_units.copy()
        return state

    def add(self, claim: Claim, *, count_units: bool, recorded_status: str | None = None) -> None:
        """Record a submission and add its units to usage when requested."""
        self.claim_ids.add(claim.claim_id)
        self.submissions[claim.duplicate_key] += 1
        self.submission_records[claim.duplicate_key].append((claim, recorded_status))
        if count_units:
            self.authorization_units[claim.authorization_key] += claim.units
            self.daily_units[claim.daily_key] += claim.units


def load_evidence(data_dir: Path, *, visit_records: dict | None = None):
    """Return authorizations by ID and verified seconds by patient, service and date."""
    authorizations = {}
    for row in read_rows(data_dir / "authorizations.csv"):
        authorizations[row["auth_id"]] = Authorization(
            row["payer_id"], row["patient_id"], row["service_code"].strip().upper(),
            parse_date(row["start_date"]), parse_date(row["end_date"]),
            int(row["units_authorized"]),
        )

    visit_seconds = defaultdict(int)
    for row in read_rows(data_dir / "evv_visits.csv"):
        status = row["verification_status"]
        if status != "verified" and visit_records is None:
            continue
        clock_in = datetime.fromisoformat(row["clock_in"])
        key = row["patient_id"], row["service_code"].strip().upper(), clock_in.date()
        if visit_records is not None:
            visit_records.setdefault(key, []).append({
                "visit_id": row["visit_id"], "verification_status": status,
            })
        if status == "verified":
            clock_out = datetime.fromisoformat(row["clock_out"])
            visit_seconds[key] += int((clock_out - clock_in).total_seconds())
    return authorizations, visit_seconds


CheckFlags = tuple[tuple[str, ...], tuple[str, ...]]
RULE_REFERENCES = {
    "PY01": {
        "evv": "Northwind provider manual, §4.2",
        "units": "Northwind provider manual, §4.3",
        "auth": "Northwind provider manual, §4.5",
        "duplicate": "Northwind provider manual, §6.4",
    },
    "PY02": {
        "evv": "Harbor provider guide, Visit verification",
        "units": "Harbor provider guide, Visit verification",
        "auth": "Harbor provider guide, Authorizations",
        "duplicate": "Harbor provider guide, Duplicates",
    },
    "PY03": {
        "evv": "Summit provider handbook, §9.1",
        "units": "Summit provider handbook, §9.1",
        "auth": "Summit provider handbook, §9.2",
        "duplicate": "Summit provider handbook, §9.4",
    },
}


def finding(details, code, reference, action, **evidence):
    """Record supporting facts and the suggested action for a failed rule."""
    if details is not None:
        details[code] = {
            "evidence": evidence, "rule_reference": reference, "suggested_action": action,
        }


def record_filing(details, claim, days, reference):
    """Record the filing deadline and the number of days past it."""
    deadline = claim.service_date + timedelta(days=days)
    finding(
        details, "D03", reference, "review_late_filing_options",
        filing_limit_days=days, filing_deadline=deadline.isoformat(),
        days_late=(claim.submission_date - deadline).days,
    )


def date_gap(service_date, authorization):
    """Return the signed gap from inclusive coverage, or None for an unknown ID."""
    if authorization is None:
        return None
    if service_date < authorization.start_date:
        return (service_date - authorization.start_date).days
    if service_date > authorization.end_date:
        return (service_date - authorization.end_date).days
    return 0


def record_auth_match(details, claim, authorizations, submitted):
    """Record why an authorization failed and any covering replacement."""
    if details is None:
        return
    candidates = {
        auth_id: auth for auth_id, auth in authorizations.items()
        if auth_id != claim.auth_id and auth.covers(claim)
    }
    ids = sorted(candidates)
    reference = RULE_REFERENCES[claim.payer]["auth"]
    action = "resolve_authorization_match"
    if not claim.auth_id:
        if len(ids) == 1:
            action = "apply_matching_authorization"
        finding(details, "D07", reference, action, candidate_auth_ids=ids)
        return

    gap = date_gap(claim.service_date, submitted)
    if submitted is None:
        mismatches = ["unknown_id"]
    else:
        mismatches = [
            f"wrong_{field}" for field in ("payer", "patient_id", "service_code")
            if getattr(claim, field) != getattr(submitted, field)
        ]
        if gap:
            mismatches.append("date_outside_coverage")
    unique_coverage = None
    if len(ids) == 1:
        candidate = candidates[ids[0]]
        unique_coverage = {
            "start_date": candidate.start_date.isoformat(),
            "end_date": candidate.end_date.isoformat(),
        }
        action = "replace_authorization"
    finding(
        details, "D08", reference, action, submitted_auth_id=claim.auth_id,
        mismatches=mismatches,
        authorization_start_date=submitted.start_date.isoformat() if submitted else None,
        authorization_end_date=submitted.end_date.isoformat() if submitted else None,
        coverage_date_gap_days=gap, candidate_auth_ids=ids,
        unique_candidate_coverage=unique_coverage,
    )


def check_submission(claim: Claim, earlier: History, same_day: History, *, details=None) -> CheckFlags:
    """Check submission type against prior and same-day claims.

    Return a pair of tuples containing denial codes and review reasons.
    """
    if claim.frequency_code == "1":
        if earlier.submissions[claim.duplicate_key]:
            if details is not None:
                matches = [
                    {"claim_id": old.claim_id, "submission_date": old.submission_date.isoformat(),
                     "recorded_status": status}
                    for old, status in earlier.submission_records[claim.duplicate_key]
                ]
                finding(
                    details, "D05", RULE_REFERENCES[claim.payer]["duplicate"],
                    "review_prior_submission", frequency_code=claim.frequency_code,
                    matching_submissions=matches,
                )
            return ("D05",), ()
        if same_day.submissions[claim.duplicate_key] > 1:
            return (), ("Same-day duplicate order",)
    elif claim.frequency_code == "7":
        if not claim.original_claim_id or claim.original_claim_id not in earlier.claim_ids:
            return (), ("Original claim unavailable before submission date",)
    else:
        return (), ("Unknown frequency code",)
    return (), ()


def check_visit(claim: Claim, verified_seconds: int | None, *, details=None, matching_visits=()) -> CheckFlags:
    """Compare billed units with verified visit time. Return codes and review reasons."""
    if verified_seconds is None:
        statuses = {row["verification_status"] for row in matching_visits}
        if not statuses:
            action = "locate_missing_visit"
        elif statuses == {"rejected"}:
            action = "review_rejected_visit"
        elif statuses == {"manual_edit_pending"}:
            action = "resolve_pending_verification"
        else:
            action = "review_visit_statuses"
        finding(details, "D01", RULE_REFERENCES[claim.payer]["evv"], action,
                matching_visits=list(matching_visits))
        return ("D01",), ()

    supported_units, remainder = divmod(verified_seconds, 15 * 60)
    if claim.payer == "PY01" and remainder >= 8 * 60:
        supported_units += 1
    if claim.units > supported_units:
        excess = claim.units - supported_units
        gap = claim.units * 15 - verified_seconds / 60
        action = "check_rounding" if gap < 30 else "reconcile_visit_and_billed_units"
        finding(
            details, "D09", RULE_REFERENCES[claim.payer]["units"], action,
            verified_minutes=verified_seconds / 60, billed_units=claim.units,
            supported_units=supported_units, excess_units=excess,
            excess_billable_minutes=excess * 15, gap_vs_evv_minutes=gap,
        )
        return ("D09",), ()
    return (), ()


def check_authorization(
    claim: Claim, authorization: Authorization | None, earlier: History, same_day: History,
    *, details=None, authorizations=None,
) -> CheckFlags:
    """Check the supplied authorization and available units. Return codes and review reasons."""
    if not claim.auth_id:
        record_auth_match(details, claim, authorizations or {}, authorization)
        return ("D07",), ()
    if authorization is None or not authorization.covers(claim):
        record_auth_match(details, claim, authorizations or {}, authorization)
        return ("D08",), ()

    key = claim.authorization_key
    remaining = (
        authorization.units
        - earlier.authorization_units[key]
        - earlier.projected_authorization_units[key]
    )
    if claim.units > remaining:
        finding(
            details, "D02", RULE_REFERENCES[claim.payer]["auth"],
            "review_authorization_balance", auth_id=claim.auth_id,
            authorized_units=authorization.units, recorded_paid_units=earlier.authorization_units[key],
            projected_paid_units=earlier.projected_authorization_units[key],
            remaining_units=remaining, requested_units=claim.units,
            shortfall_units=claim.units - max(0, remaining),
        )
        return ("D02",), ()
    if same_day.authorization_units[key] > remaining:
        return (), ("Same-day authorization balance",)
    return (), ()


def check_northwind(claim: Claim, *, details=None) -> CheckFlags:
    """Apply the Northwind filing window to the claim. Return codes and review reasons."""
    elapsed = (claim.submission_date - claim.service_date).days
    filing_limit = 45 if NORTHWIND_FILING_BULLETIN.includes(claim.service_date) else 30
    if elapsed > filing_limit:
        reference = "Northwind Provider Bulletin 2026-07" if filing_limit == 45 else "Northwind provider manual, §6.1"
        record_filing(details, claim, filing_limit, reference)
        return ("D03",), ()
    return (), ()


def check_harbor(claim: Claim, earlier: History, same_day: History, *, details=None) -> CheckFlags:
    """Check the Harbor modifier and daily units. Return codes and review reasons."""
    failures = []
    review = []
    if claim.service_code == "T1019":
        is_weekend = claim.service_date.weekday() >= 5
        weekend_override = is_weekend and HARBOR_WEEKEND_BULLETIN.includes(claim.service_date)
        modifier = "U3" if weekend_override else "U2"
        if claim.modifier != modifier:
            failures.append("D04")
            reference = "Harbor Provider Notice 2026-08" if HARBOR_WEEKEND_BULLETIN.includes(claim.service_date) else "Harbor provider guide, Modifiers"
            finding(details, "D04", reference, "apply_expected_modifier",
                    submitted_modifier=claim.modifier, expected_modifier=modifier)

    key = claim.daily_key
    earlier_units = earlier.daily_units[key] + earlier.projected_daily_units[key]
    if earlier_units + claim.units > 32:
        failures.append("D06")
        available = max(0, 32 - earlier_units)
        action = "confirm_cap_reduction" if available else "review_daily_cap"
        finding(
            details, "D06", "Harbor provider guide, Daily maximum", action,
            daily_cap_units=32, already_counted_units=earlier_units, billed_units=claim.units,
            excess_units=earlier_units + claim.units - 32, available_units=available,
            proposed_units=available or None,
        )
    elif earlier_units + same_day.daily_units[key] > 32:
        review.append("Same-day Harbor daily cap")
    return tuple(failures), tuple(review)


def check_summit(claim: Claim, *, details=None, verified_seconds=None) -> CheckFlags:
    """Apply Summit filing and unit increment rules. Return codes and review reasons."""
    failures = []
    elapsed = (claim.submission_date - claim.service_date).days
    if elapsed > 60:
        failures.append("D03")
        record_filing(details, claim, 60, "Summit provider handbook, §9.3")
    if SUMMIT_WHOLE_HOUR_PERIOD.includes(claim.service_date) and claim.units % 4:
        failures.append("D10")
        finding(
            details, "D10", "Summit Amendment 3 (inferred, effective 2026-07-01)",
            "review_whole_hour_units", billed_units=claim.units, required_unit_multiple=4,
            remainder_units=claim.units % 4,
            verified_minutes=None if verified_seconds is None else verified_seconds / 60,
            evv_units_exact=None if verified_seconds is None else verified_seconds / 900,
            evv_supported_units=None if verified_seconds is None else verified_seconds // 900,
        )
    return tuple(failures), ()


def check_payer_rules(claim: Claim, earlier: History, same_day: History, *,
                      details=None, verified_seconds=None) -> CheckFlags:
    """Dispatch a supported payer's checks. Return codes and review reasons."""
    if claim.payer == "PY01":
        return check_northwind(claim, details=details)
    if claim.payer == "PY02":
        return check_harbor(claim, earlier, same_day, details=details)
    if claim.payer == "PY03":
        return check_summit(claim, details=details, verified_seconds=verified_seconds)
    return (), ("Unknown payer",)


def check_claim(claim, authorizations, visit_seconds, earlier, same_day, *,
                details=None, matching_visits=()) -> CheckFlags:
    """Check a claim against authorization, EVV and submission lookups.

    Return all failed codes in priority order and all review reasons.
    """
    if claim.payer not in PAYER_IDS:
        return (), ("Unknown payer",)

    seconds = visit_seconds.get(claim.visit_key)
    checks = (
        check_submission(claim, earlier, same_day, details=details),
        check_visit(claim, seconds, details=details, matching_visits=matching_visits),
        check_authorization(
            claim, authorizations.get(claim.auth_id), earlier, same_day,
            details=details, authorizations=authorizations,
        ),
        check_payer_rules(claim, earlier, same_day, details=details, verified_seconds=seconds),
    )
    failures = {code for codes, _ in checks for code in codes}
    review = tuple(reason for _, reasons in checks for reason in reasons)
    priority = DENIAL_PRIORITY_BY_PAYER[claim.payer]
    return tuple(code for code in priority if code in failures), review


def replay(data_dir: Path, *, history_csv: Path | None = None):
    """Return the scored historical claims and the historical input row count."""
    results, _, raw_count = replay_all(data_dir, history_csv=history_csv)
    return results, raw_count


def predict_pending(data_dir: Path) -> list[dict]:
    """Return a prediction row for every claim in claims_to_check.csv, in file order."""
    _, pending, _ = replay_all(data_dir)
    return pending


def predict_pending_assessments(data_dir: Path) -> list[dict]:
    """Return interface-ready assessments for pending claims in file order."""
    _, pending, _ = replay_all(data_dir, include_details=True)
    return [row["assessment"] for row in pending]


def record_submission(history, claim, status, *, predicted_paid):
    """Apply a submission's recorded or projected usage to the replay ledger."""
    history.add(claim, count_units=status == "paid", recorded_status=status)
    if status is None and predicted_paid:
        history.projected_authorization_units[claim.authorization_key] += claim.units
        history.projected_daily_units[claim.daily_key] += claim.units


def replay_all(data_dir: Path, *, history_csv: Path | None = None, include_details=False):
    """Replay by date, breaking same-date ties in original row order within each CSV.

    Return (scored historical rows, pending prediction rows, historical input row count).
    """
    visit_records = {} if include_details else None
    authorizations, visit_seconds = load_evidence(data_dir, visit_records=visit_records)
    raw_rows = read_rows(history_csv if history_csv is not None else data_dir / "claims_history.csv")
    unique_rows = {tuple(row.items()): row for row in raw_rows}.values()
    entries = [(Claim.from_row(row), row) for row in unique_rows]
    entries.extend(
        (Claim.from_row(row), None)
        for row in read_rows(data_dir / "claims_to_check.csv")
    )
    if len({claim.claim_id for claim, _ in entries}) != len(entries):
        raise ValueError("Conflicting rows share a claim ID")
    entries.sort(key=lambda entry: entry[0].submission_date)

    earlier = History()
    results = []
    pending = {}
    for _, group in groupby(entries, key=lambda entry: entry[0].submission_date):
        recorded, unsubmitted = [], []
        for claim, row in group:
            (unsubmitted if row is None else recorded).append((claim, row))

        recorded_totals, pending_totals = History(), History()
        for batch, totals in ((recorded, recorded_totals), (unsubmitted, pending_totals)):
            for claim, _ in batch:
                totals.add(claim, count_units=True)

        # Each CSV sees its own earlier rows. The other file only informs review.
        recorded_history = earlier.snapshot() if recorded and unsubmitted else earlier
        deferred = []
        for batch, file_history, other_totals in (
            (recorded, recorded_history, pending_totals),
            (unsubmitted, earlier, recorded_totals),
        ):
            for claim, row in batch:
                same_day = other_totals.snapshot()
                same_day.add(claim, count_units=True)
                details = {} if include_details and row is None else None
                failures, review = check_claim(
                    claim, authorizations, visit_seconds, file_history, same_day, details=details,
                    matching_visits=visit_records.get(claim.visit_key, ()) if visit_records is not None else (),
                )
                prediction = {
                    "claim_id": claim.claim_id,
                    "payer": claim.payer,
                    "submitted_date": claim.submission_date.isoformat(),
                    "predicted_status": "denied" if failures else "paid",
                    "predicted_denial_code": failures[0] if failures else "",
                    "failed_checks": "|".join(failures),
                    "review_reasons": "|".join(review),
                    "earlier_recorded_auth_units": file_history.authorization_units[claim.authorization_key],
                    "earlier_projected_auth_units": file_history.projected_authorization_units[
                        claim.authorization_key
                    ],
                }
                status = row["status"] if row is not None else None
                record_submission(file_history, claim, status, predicted_paid=not failures)
                if file_history is not earlier:
                    deferred.append((claim, status, not failures))

                if row is None:
                    if details is not None:
                        prediction["assessment"] = {
                            "claim_id": claim.claim_id, "payer": claim.payer,
                            "service_code": claim.service_code,
                            "service_date": claim.service_date.isoformat(),
                            "submission_date": claim.submission_date.isoformat(),
                            "status": "needs_review" if failures or review else "ready_to_submit",
                            "predicted_denial_code": failures[0] if failures else None,
                            "failed_checks": list(failures),
                            "finding_details": {code: details[code] for code in failures},
                            "review_reasons": list(review),
                        }
                    pending[claim.claim_id] = prediction
                    continue
                results.append({
                    **prediction,
                    "actual_status": row["status"],
                    "actual_denial_code": row["denial_code"],
                })

        for claim, status, predicted_paid in deferred:
            record_submission(earlier, claim, status, predicted_paid=predicted_paid)

    order = [row["claim_id"] for row in read_rows(data_dir / "claims_to_check.csv")]
    return results, [pending[claim_id] for claim_id in order], len(raw_rows)


def print_metrics(results, raw_count):
    """Print outcome and denial-code metrics for the historical audit."""
    outcomes = Counter((r["actual_status"], r["predicted_status"]) for r in results)
    both_denied = outcomes["denied", "denied"]
    both_paid = outcomes["paid", "paid"]
    actual_denied = both_denied + outcomes["denied", "paid"]
    predicted_denied = both_denied + outcomes["paid", "denied"]
    code_matches = sum(
        r["actual_status"] == "denied"
        and r["actual_denial_code"] == r["predicted_denial_code"] for r in results
    )
    candidate_matches = sum(
        r["actual_status"] == "denied"
        and r["actual_denial_code"] in r["failed_checks"].split("|") for r in results
    )

    print(f"Historical rows: {raw_count} | Unique claims: {len(results)}")
    print("Actual / predicted       paid   denied")
    for actual in ("paid", "denied"):
        print(f"{actual:18}{outcomes[actual, 'paid']:10}{outcomes[actual, 'denied']:9}")

    def show(label, hits, total):
        rate = f"{hits / total:.2%}" if total else "n/a"
        print(f"{label}: {hits}/{total} ({rate})")

    show("Status accuracy", both_denied + both_paid, len(results))
    show("Denial recall", both_denied, actual_denied)
    show("Denial precision", both_denied, predicted_denied)
    show("Exact code among all predicted denials", code_matches, predicted_denied)
    show("Exact code when both say denied", code_matches, both_denied)
    show("Exact code among all actual denials", code_matches, actual_denied)
    show("Actual code among failed checks when both say denied", candidate_matches, both_denied)
    missed_codes = Counter(
        r["actual_denial_code"] for r in results
        if r["actual_status"] == "denied" and r["predicted_status"] == "paid"
    )
    print(f"False positives: {outcomes['paid', 'denied']}")
    print(f"False negatives: {outcomes['denied', 'paid']} {dict(sorted(missed_codes.items()))}")


def write_rows(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    """Write rows to a CSV file, keeping only the given columns if supplied."""
    fieldnames = fieldnames or list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    """Read CLI options, run the historical audit, and write the audit and prediction CSVs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--audit", type=Path, default=Path("history_audit.csv"))
    parser.add_argument(
        "--predictions", type=Path, default=Path("predictions.csv"),
        help="Where to write claim_id,status,denial_code for claims_to_check.csv.",
    )
    parser.add_argument("--details", type=Path, help="Write pending assessments as a JSON array.")
    args = parser.parse_args()
    results, pending, raw_count = replay_all(args.data_dir, include_details=args.details is not None)
    if args.details is not None:
        assessments = [row["assessment"] for row in pending]
        args.details.write_text(json.dumps(assessments, indent=2) + "\n", encoding="utf-8")
    write_rows(args.audit, results)
    write_rows(
        args.predictions,
        [{"claim_id": r["claim_id"], "status": r["predicted_status"],
          "denial_code": r["predicted_denial_code"]} for r in pending],
    )
    print_metrics(results, raw_count)
    print(f"Wrote {len(pending)} pending predictions to {args.predictions}")


if __name__ == "__main__":
    main()
