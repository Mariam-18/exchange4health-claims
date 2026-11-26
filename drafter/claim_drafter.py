"""Build claims from verified EVV and replay the resulting submission stream."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from checker.claim_checker import (
    Claim,
    PAYER_IDS,
    expected_modifier,
    whole_hour_rule,
    History,
    check_claim,
    load_evidence,
    read_rows,
)

# The synthetic export consistently implies these rates. Production should
# get them, and the patient-to-agency assignment, from master data.
SERVICE_RATES = {
    "T1019": Decimal("6.10"),
    "S5125": Decimal("5.40"),
    "T1005": Decimal("4.80"),
}
DUPLICATE_DRAFT_PREFIX = "Duplicate: "
PENDING_DRAFT_PREFIX = "Pending: "
HARBOR_DAILY_CAP = 32
READY = "ready to submit"
NEEDS_REVIEW = "needs review"
CONFIRM_CAP_REDUCTION = "confirm Harbor reduction"
CHECK_AUTHORIZATION = "check remaining authorization balance"
RESOLVE_CHECKS = "resolve blocking checks"


@dataclass
class Draft:
    claim: Claim
    agency: str
    visit_ids: tuple
    verified_seconds: int
    billed_amount: Decimal


@dataclass
class Decision:
    status: str
    action: str
    failures: tuple
    review: tuple
    remaining_auth: int
    remaining_harbor: int | None
    proposed_units: int | None
    released_units: int | None
    reduction_failures: tuple
    reduction_review: tuple


def construct(data_dir, authorizations, verified_seconds, agencies, *, submitted_claims=(), pending_claims=()):
    """Build drafts and report visit groups skipped, including matches to existing claims."""
    submitted_by_key = {}
    for claim in submitted_claims:
        submitted_by_key.setdefault(claim.duplicate_key, claim.claim_id)
    pending_by_key = {}
    for claim in pending_claims:
        pending_by_key.setdefault(claim.duplicate_key, claim.claim_id)

    coverage = defaultdict(list)
    for auth_id, auth in authorizations.items():
        coverage[auth.patient_id, auth.service_code].append((auth_id, auth))

    visits = defaultdict(list)
    for row in read_rows(data_dir / "evv_visits.csv"):
        if row["verification_status"] != "verified":
            continue
        start = datetime.fromisoformat(row["clock_in"])
        end = datetime.fromisoformat(row["clock_out"])
        key = row["patient_id"], row["service_code"].strip().upper(), start.date()
        visits[key].append((row["visit_id"], end))

    drafts = []
    skipped = []
    summit_adjustments = 0
    for (patient, service, service_date), records in sorted(visits.items()):
        key = patient, service, service_date
        assignments = agencies.get(patient, ())
        agency = next(iter(assignments)) if len(assignments) == 1 else ""
        if not agency:
            skipped.append((key, agency, "missing or ambiguous agency assignment"))
            continue

        candidates = [
            (auth_id, auth)
            for auth_id, auth in coverage[patient, service]
            if auth.start_date <= service_date <= auth.end_date
        ]
        if len(candidates) != 1:
            skipped.append((key, agency, "missing or ambiguous authorization"))
            continue
        auth_id, auth = candidates[0]
        if auth.payer not in PAYER_IDS or service not in SERVICE_RATES:
            skipped.append((key, agency, "missing payer rules or service rate"))
            continue


        seconds = verified_seconds[key]
        units, remainder = divmod(seconds, 900)
        if auth.payer == "PY01" and remainder >= 480:
            units += 1
        if whole_hour_rule(auth.payer, service_date):
            summit_adjustments += int(units % 4 != 0)
            units -= units % 4
        if units <= 0:
            skipped.append((key, agency, "no billable units"))
            continue

        prior_id = submitted_by_key.get((auth.payer, *key))
        if prior_id is not None:
            skipped.append((key, agency, f"{DUPLICATE_DRAFT_PREFIX}claim {prior_id} was already submitted"))
            continue

        pending_id = pending_by_key.get((auth.payer, *key))
        if pending_id is not None:
            skipped.append((key, agency, f"{PENDING_DRAFT_PREFIX}claim {pending_id} is already waiting to be submitted"))
            continue

        modifier = expected_modifier(auth.payer, service, service_date) or ""

        # Simulate a daily run after the last visit clock-out. The export does
        # not contain verification timestamps or actual filing times.
        submission = max(end for _, end in records).date() + timedelta(days=1)
        claim = Claim(
            f"EVV-{len(drafts) + 1:05d}", auth.payer, patient, service,
            service_date, submission, units, auth_id, modifier, "1", "",
        )
        drafts.append(Draft(
            claim, agency, tuple(v for v, _ in records),
            seconds, SERVICE_RATES[service] * units,
        ))
    return drafts, skipped, summit_adjustments


def simulate(
    drafts, authorizations, verified_seconds, *,
    existing=(), confirm_reductions=False, confirmed_claim_ids=(),
):
    """Check drafts in order using the shared checker and track released units."""
    earlier = History()
    recorded_by_date = defaultdict(list)
    for claim, row in existing:
        recorded_by_date[claim.submission_date].append((claim, row["status"] == "paid"))
    recorded_days = iter(sorted(recorded_by_date))
    recorded_day = next(recorded_days, None)

    for draft in sorted(drafts, key=lambda d: (
        d.claim.submission_date, d.claim.service_date, d.claim.patient_id,
        d.claim.service_code, d.claim.claim_id,
    )):
        claim = draft.claim
        while recorded_day is not None and recorded_day < claim.submission_date:
            for recorded_claim, paid in recorded_by_date[recorded_day]:
                earlier.add(recorded_claim, count_units=paid)
            recorded_day = next(recorded_days, None)

        same_day = History()
        same_day.add(claim, count_units=True)
        for recorded_claim, _ in recorded_by_date.get(claim.submission_date, ()):
            same_day.add(recorded_claim, count_units=True)

        failures, review = check_claim(
            claim, authorizations, verified_seconds, earlier, same_day,
        )
        remaining_auth = (
            authorizations[claim.auth_id].units
            - earlier.authorization_units[claim.authorization_key]
        )
        remaining_harbor = None
        proposed_units = None
        released = None
        reduction_failures, reduction_review = (), ()
        status = NEEDS_REVIEW
        action = CHECK_AUTHORIZATION if "D02" in failures else RESOLVE_CHECKS

        if claim.payer == "PY02":
            remaining_harbor = max(0, HARBOR_DAILY_CAP - earlier.daily_units[claim.daily_key])

        if not failures and not review:
            status, action, released = READY, "", claim
        elif "D06" in failures and remaining_harbor:
            proposed_units = remaining_harbor
            reduced = replace(claim, units=proposed_units)
            same_day.authorization_units[claim.authorization_key] -= claim.units - proposed_units
            same_day.daily_units[claim.daily_key] -= claim.units - proposed_units
            reduction_failures, reduction_review = check_claim(
                reduced, authorizations, verified_seconds, earlier, same_day,
            )
            if not reduction_failures and not reduction_review:
                action = CONFIRM_CAP_REDUCTION
                if confirm_reductions or claim.claim_id in confirmed_claim_ids:
                    status, action, released = READY, "", reduced

        if released is not None:
            earlier.add(released, count_units=True)
        yield draft, Decision(
            status, action, failures, review, remaining_auth, remaining_harbor,
            proposed_units, released.units if released is not None else None,
            reduction_failures, reduction_review,
        )


def write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def experiment(data_dir, output_dir, *, check_history=False, confirmed_claim_ids=()):
    history = {r["claim_id"]: r for r in read_rows(data_dir / "claims_history.csv")}
    by_key = defaultdict(list)
    agencies = defaultdict(set)
    for row in history.values():
        claim = Claim.from_row(row)
        by_key[claim.duplicate_key].append((claim, row))
        agencies[claim.patient_id].add(row["agency_id"])

    pending_keys = {
        Claim.from_row(row).duplicate_key
        for row in read_rows(data_dir / "claims_to_check.csv")
    }
    authorizations, verified_seconds = load_evidence(data_dir)
    existing = [entry for entries in by_key.values() for entry in entries] if check_history else ()
    drafts, skipped, summit_adjustments = construct(
        data_dir, authorizations, verified_seconds, agencies,
        submitted_claims=(claim for claim, _ in existing),
    )
    generated = {draft.claim.duplicate_key for draft in drafts}
    if len(generated) != len(drafts):
        raise ValueError("Multiple generated claims share a payer/patient/service/date key")

    results = []
    counts = Counter()
    actions_by_scenario = defaultdict(Counter)
    base_replay = list(simulate(drafts, authorizations, verified_seconds, existing=existing))
    confirmed_replay = list(simulate(
        drafts, authorizations, verified_seconds, existing=existing,
        confirm_reductions=not confirmed_claim_ids, confirmed_claim_ids=confirmed_claim_ids,
    ))
    if len(base_replay) != len(confirmed_replay):
        raise ValueError("Replay scenarios produced different numbers of drafts")
    for (draft, decision), (confirmed_draft, confirmed) in zip(base_replay, confirmed_replay):
        c = draft.claim
        assert c.claim_id == confirmed_draft.claim.claim_id
        observed = by_key.get(c.duplicate_key, ())
        statuses = {row["status"] for _, row in observed}
        if not statuses <= {"paid", "denied"}:
            raise ValueError(f"Unknown historical outcome for {c.duplicate_key}")
        verdict = "paid" if "paid" in statuses else "denied" if statuses else "no verdict"
        counts["mixed history"] += len(statuses) > 1
        same_terms_paid = [
            old.claim_id for old, row in observed
            if row["status"] == "paid" and old.units == c.units and old.auth_id == c.auth_id
            and old.modifier.strip().upper() == c.modifier
        ]
        counts["same terms paid"] += bool(same_terms_paid)
        counts["no verdict but pending"] += verdict == "no verdict" and c.duplicate_key in pending_keys
        result = {
            "claim_id": c.claim_id, "agency_id": draft.agency, "payer": c.payer,
            "patient_id": c.patient_id, "service_code": c.service_code,
            "date_of_service": c.service_date, "submitted_date": c.submission_date,
            "units": c.units, "auth_id": c.auth_id, "modifier": c.modifier,
            "frequency_code": c.frequency_code, "original_claim_id": c.original_claim_id,
            "billed_amount": format(draft.billed_amount, ".2f"),
            "visit_ids": "|".join(draft.visit_ids), "verified_minutes": draft.verified_seconds / 60,
            "historical_verdict": verdict,
            "historical_statuses": "|".join(sorted(statuses)),
            "historical_claim_ids": "|".join(old.claim_id for old, _ in observed),
            "exact_terms_paid_claim_ids": "|".join(same_terms_paid),
            "pending_claim_exists": c.duplicate_key in pending_keys,
        }
        for scenario, outcome in (("before_confirmation", decision), ("if_confirmed", confirmed)):
            counts[scenario, verdict, outcome.status] += 1
            if outcome.action:
                actions_by_scenario[scenario][outcome.action] += 1
            result.update({
                f"{scenario}_status": outcome.status,
                f"{scenario}_action": outcome.action,
                f"{scenario}_blocking_checks": "|".join(outcome.failures),
                f"{scenario}_review_reasons": "|".join(outcome.review),
                f"{scenario}_remaining_auth": outcome.remaining_auth,
                f"{scenario}_remaining_harbor": outcome.remaining_harbor,
                f"{scenario}_proposed_units": outcome.proposed_units,
                f"{scenario}_proposed_amount": (
                    format(SERVICE_RATES[c.service_code] * outcome.proposed_units, ".2f")
                    if outcome.proposed_units is not None else None
                ),
                f"{scenario}_reduction_blocking_checks": "|".join(outcome.reduction_failures),
                f"{scenario}_reduction_review_reasons": "|".join(outcome.reduction_review),
                f"{scenario}_authorization_shortfall": (
                    max(0, c.units - max(0, outcome.remaining_auth)) if "D02" in outcome.failures else 0
                ),
                f"{scenario}_harbor_excess_units": (
                    c.units - outcome.remaining_harbor if "D06" in outcome.failures else 0
                ),
                f"{scenario}_released_units": outcome.released_units,
                f"{scenario}_released_amount": (
                    format(SERVICE_RATES[c.service_code] * outcome.released_units, ".2f")
                    if outcome.released_units is not None else None
                ),
            })
        results.append(result)

    missed_paid = []
    paid_keys = set()
    for key, observed in by_key.items():
        for claim, row in observed:
            if row["status"] != "paid":
                continue
            paid_keys.add(key)
            if key not in generated:
                missed_paid.append({**row, "reason": (
                    "no verified EVV" if claim.visit_key not in verified_seconds
                    else "verified EVV found, but no draft for this payer"
                )})
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "constructed_claims.csv", list(results[0]), results)
    write_csv(
        output_dir / "drafting_exceptions.csv",
        ["patient_id", "service_code", "date_of_service", "agency_id", "reason"],
        [
            dict(patient_id=patient, service_code=service, date_of_service=date, agency_id=agency, reason=reason)
            for (patient, service, date), agency, reason in skipped
        ],
    )
    write_csv(output_dir / "historical_paid_without_generated.csv", [*next(iter(history.values())), "reason"], missed_paid)
    lines = [
        f"Verified patient/service/date groups: {len(verified_seconds):,}",
        f"Generated drafts: {len(drafts):,}",
        f"Skipped groups: {dict(Counter(reason for _, _, reason in skipped))}",
        f"Summit drafts adjusted to complete hours: {summit_adjustments:,}",
        "",
        "Historical service-key verdict | ready to submit | needs review | total",
    ]
    verdicts = ("paid", "denied", "no verdict")
    for verdict in verdicts:
        n = [counts["before_confirmation", verdict, status] for status in (READY, NEEDS_REVIEW)]
        lines.append(f"{verdict} | " + " | ".join(f"{x:,}" for x in [*n, sum(n)]))
    confirmation_label = "confirm selected eligible reductions" if confirmed_claim_ids else "confirm every eligible Harbor reduction immediately"
    lines.extend(["", f"Scenario: {confirmation_label}",
                  "Historical service-key verdict | ready to submit | needs review | total"])
    for verdict in verdicts:
        n = [counts["if_confirmed", verdict, status] for status in (READY, NEEDS_REVIEW)]
        lines.append(f"{verdict} | " + " | ".join(f"{x:,}" for x in [*n, sum(n)]))
    lines.extend([
        "",
        f"Generated keys with both paid and denied history: {counts['mixed history']:,}",
        f"Generated drafts with a paid historical claim matching units, authorization and modifier: {counts['same terms paid']:,}",
        f"No-verdict drafts with a pending claim for the same key: {counts['no verdict but pending']:,}",
        f"Historical paid claim IDs with no generated draft: {len(missed_paid):,}",
        f"Historical paid service keys with no generated draft: {len(paid_keys - generated):,} of {len(paid_keys):,}",
        f"Review actions before confirmations: {dict(actions_by_scenario['before_confirmation'])}",
        f"Review actions with immediate confirmations: {dict(actions_by_scenario['if_confirmed'])}",
    ])
    summary = "\n".join(lines) + "\n"
    (output_dir / "results.txt").write_text(summary, encoding="utf-8")
    print(summary, end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--check-history", action="store_true", help="Block drafts for recorded submissions and include their usage in checks.")
    parser.add_argument("--confirm-claim", action="append", default=[], help="Model confirming the reduction for this draft ID.")
    args = parser.parse_args()
    experiment(args.data_dir, args.output_dir, check_history=args.check_history, confirmed_claim_ids=set(args.confirm_claim))
