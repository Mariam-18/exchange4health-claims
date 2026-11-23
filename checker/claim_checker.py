"""Replay the Exchange4Health history with the supplied payer rules.

Run: python claim_checker.py /path/to/data-pack
Optional: --audit /path/to/history_audit.csv

The replay uses earlier recorded payments plus earlier pending claims forecast
to pay when reconstructing authorization balances. Only history is scored.
"""

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
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
        self.authorization_units = Counter()
        self.projected_authorization_units = Counter()
        self.daily_units = Counter()

    def add(self, claim: Claim, *, count_units: bool) -> None:
        """Record a submission and add its units to usage when requested."""
        self.claim_ids.add(claim.claim_id)
        self.submissions[claim.duplicate_key] += 1
        if count_units:
            self.authorization_units[claim.authorization_key] += claim.units
            self.daily_units[claim.daily_key] += claim.units


def load_evidence(data_dir: Path):
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
        if row["verification_status"] == "verified":
            clock_in = datetime.fromisoformat(row["clock_in"])
            clock_out = datetime.fromisoformat(row["clock_out"])
            key = row["patient_id"], row["service_code"].strip().upper(), clock_in.date()
            visit_seconds[key] += int((clock_out - clock_in).total_seconds())
    return authorizations, visit_seconds


CheckFlags = tuple[tuple[str, ...], tuple[str, ...]]


def check_submission(claim: Claim, earlier: History, same_day: History) -> CheckFlags:
    """Check submission type against prior and same-day claims.

    Return a pair of tuples containing denial codes and review reasons.
    """
    if claim.frequency_code == "1":
        if earlier.submissions[claim.duplicate_key]:
            return ("D05",), ()
        if same_day.submissions[claim.duplicate_key] > 1:
            return (), ("Same-day duplicate order",)
    elif claim.frequency_code == "7":
        if not claim.original_claim_id or claim.original_claim_id not in earlier.claim_ids:
            return (), ("Original claim unavailable before submission date",)
    else:
        return (), ("Unknown frequency code",)
    return (), ()


def check_visit(claim: Claim, verified_seconds: int | None) -> CheckFlags:
    """Compare billed units with verified visit time. Return codes and review reasons."""
    if verified_seconds is None:
        return ("D01",), ()

    supported_units, remainder = divmod(verified_seconds, 15 * 60)
    if claim.payer == "PY01" and remainder >= 8 * 60:
        supported_units += 1
    if claim.units > supported_units:
        return ("D09",), ()
    return (), ()


def check_authorization(
    claim: Claim, authorization: Authorization | None, earlier: History, same_day: History,
) -> CheckFlags:
    """Check the supplied authorization and available units. Return codes and review reasons."""
    if not claim.auth_id:
        return ("D07",), ()
    if authorization is None or not authorization.covers(claim):
        return ("D08",), ()

    key = claim.authorization_key
    remaining = (
        authorization.units
        - earlier.authorization_units[key]
        - earlier.projected_authorization_units[key]
    )
    if claim.units > remaining:
        return ("D02",), ()
    if same_day.authorization_units[key] > remaining:
        return (), ("Same-day authorization balance",)
    return (), ()


def check_northwind(claim: Claim) -> CheckFlags:
    """Apply the Northwind filing window to the claim. Return codes and review reasons."""
    elapsed = (claim.submission_date - claim.service_date).days
    filing_limit = 45 if NORTHWIND_FILING_BULLETIN.includes(claim.service_date) else 30
    if elapsed > filing_limit:
        return ("D03",), ()
    return (), ()


def check_harbor(claim: Claim, earlier: History, same_day: History) -> CheckFlags:
    """Check the Harbor modifier and daily units. Return codes and review reasons."""
    failures = []
    review = []
    if claim.service_code == "T1019":
        is_weekend = claim.service_date.weekday() >= 5
        weekend_override = is_weekend and HARBOR_WEEKEND_BULLETIN.includes(claim.service_date)
        modifier = "U3" if weekend_override else "U2"
        if claim.modifier != modifier:
            failures.append("D04")

    key = claim.daily_key
    earlier_units = earlier.daily_units[key]
    if earlier_units + claim.units > 32:
        failures.append("D06")
    elif earlier_units + same_day.daily_units[key] > 32:
        review.append("Same-day Harbor daily cap")
    return tuple(failures), tuple(review)


def check_summit(claim: Claim) -> CheckFlags:
    """Apply Summit filing and unit increment rules. Return codes and review reasons."""
    failures = []
    elapsed = (claim.submission_date - claim.service_date).days
    if elapsed > 60:
        failures.append("D03")
    if SUMMIT_WHOLE_HOUR_PERIOD.includes(claim.service_date) and claim.units % 4:
        failures.append("D10")
    return tuple(failures), ()


def check_payer_rules(claim: Claim, earlier: History, same_day: History) -> CheckFlags:
    """Dispatch a supported payer's checks. Return codes and review reasons."""
    if claim.payer == "PY01":
        return check_northwind(claim)
    if claim.payer == "PY02":
        return check_harbor(claim, earlier, same_day)
    if claim.payer == "PY03":
        return check_summit(claim)
    return (), ("Unknown payer",)


def check_claim(claim, authorizations, visit_seconds, earlier, same_day) -> CheckFlags:
    """Check a claim against authorization, EVV and submission lookups.

    Return all failed codes in priority order and all review reasons.
    """
    if claim.payer not in PAYER_IDS:
        return (), ("Unknown payer",)

    checks = (
        check_submission(claim, earlier, same_day),
        check_visit(claim, visit_seconds.get(claim.visit_key)),
        check_authorization(claim, authorizations.get(claim.auth_id), earlier, same_day),
        check_payer_rules(claim, earlier, same_day),
    )
    failures = {code for codes, _ in checks for code in codes}
    review = tuple(reason for _, reasons in checks for reason in reasons)
    priority = DENIAL_PRIORITY_BY_PAYER[claim.payer]
    return tuple(code for code in priority if code in failures), review


def replay(data_dir: Path, *, history_csv: Path | None = None):
    """Return the scored historical claims and the historical input row count."""
    authorizations, visit_seconds = load_evidence(data_dir)
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
    for _, group in groupby(entries, key=lambda entry: entry[0].submission_date):
        batch = list(group)
        same_day = History()
        # Count all submissions in the batch for the same-day review checks.
        for claim, _ in batch:
            same_day.add(claim, count_units=True)

        projected_payments = []
        for claim, row in batch:
            failures, review = check_claim(claim, authorizations, visit_seconds, earlier, same_day)
            if row is None:
                if not failures:
                    projected_payments.append(claim)
                continue
            results.append({
                "claim_id": claim.claim_id,
                "payer": claim.payer,
                "submitted_date": claim.submission_date.isoformat(),
                "actual_status": row["status"],
                "actual_denial_code": row["denial_code"],
                "predicted_status": "denied" if failures else "paid",
                "predicted_denial_code": failures[0] if failures else "",
                "failed_checks": "|".join(failures),
                "review_reasons": "|".join(review),
                "earlier_recorded_auth_units": earlier.authorization_units[claim.authorization_key],
                "earlier_projected_auth_units": earlier.projected_authorization_units[
                    claim.authorization_key
                ],
            })
        # Update usage after the batch so it is available to later dates.
        for claim in projected_payments:
            earlier.projected_authorization_units[claim.authorization_key] += claim.units
        for claim, row in batch:
            if row is not None:
                earlier.add(claim, count_units=row["status"] == "paid")
    return results, len(raw_rows)


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


def main() -> None:
    """Read CLI options, run the historical audit and write its CSV."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--audit", type=Path, default=Path("history_audit.csv"))
    args = parser.parse_args()
    results, raw_count = replay(args.data_dir)
    with args.audit.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    print_metrics(results, raw_count)


if __name__ == "__main__":
    main()
