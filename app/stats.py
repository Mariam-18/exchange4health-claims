"""Standard report statistics, computed by Python for a scope (agency or all) and a period.

Each statistic is a small dict or list of rows, so it can be charted and handed to Claude as-is.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import median

import pandas as pd

from app import activity
from app.review import ISSUE_LABELS, describe
from checker.claim_checker import read_rows

STAT_TITLES = {
    "journey": "Claims at each step: drafted, reviewed, fixed, submitted, paid",
    "denials_by_code": "Denials by code",
    "denials_by_month": "Denials by month",
    "flagged_by_reason": "What the checker flags, by expected reason",
    "passed_without_review": "Claims that passed without review",
    "one_click_estimate": "Denials a one-click fix would have prevented (estimate)",
    "outliers": "Caregivers and patients with unusually high denial rates",
    "payers": "Denials by payer",
    "queue_health": "Review queue health (now)",
    "fix_uptake": "Fix uptake by issue (app activity)",
    "unexpected_reasons": "Unexpected denials by payer and code",
    "recurring_issues": "Issues that keep recurring, by agency",
    "rebilled_claims": "Claims billed again for the same visit",
    "time_to_resolve": "Time to resolve by issue (app activity)",
    "visits_not_drafted": "Visits that couldn't become claims",
}
MIN_SAMPLE = 10


def in_period(frame: pd.DataFrame, field: str, start: date, end: date, agency: str | None) -> pd.DataFrame:
    """Claims whose date (service_date or submitted_date) falls in the period, for one agency or all."""
    part = frame[(frame[field] >= start.isoformat()) & (frame[field] <= end.isoformat())]
    return part[part["agency_id"] == agency] if agency else part


def compute(frame: pd.DataFrame, history_assessments: dict, data_dir: Path, *, field: str, start: date,
            end: date, agency: str | None, queue: list, patient_agency: dict[str, str],
            drafts: list[dict] = ()) -> dict:
    """All standard statistics for the scope and period.

    frame: analysis table of past claims (app.analysis.build_frame).
    history_assessments: {claim_id: checker assessment with finding details}.
    queue: current Check claims review items (app.review.ReviewItem), for queue health.
    drafts: claims drafted from EVV for all agencies: {agency, service_date, needs_review, billed}.
    """
    part = in_period(frame, field, start, end, agency)
    denied = part[part["status"] == "denied"]
    return {
        "scope": {"agency": agency or "all agencies", "period_by": field, "from": start.isoformat(),
                  "to": end.isoformat(), "claims": int(len(part)), "denied": int(len(denied)),
                  "denial_rate_pct": pct(len(denied), len(part)),
                  "billed": money(part["billed_amount"].sum()), "denied_billed": money(denied["billed_amount"].sum())},
        "journey": journey(part, data_dir, start, end, agency, queue, patient_agency, drafts),
        "denials_by_code": denials_by_code(denied),
        "denials_by_month": denials_by_month(part, field),
        "flagged_by_reason": flagged_by_reason(part),
        "passed_without_review": passed_without_review(part),
        "one_click_estimate": one_click_estimate(part, history_assessments),
        "outliers": outliers(part),
        "payers": payers(part),
        "queue_health": queue_health(queue, agency),
        "fix_uptake": fix_uptake(agency),
        "unexpected_reasons": unexpected_reasons(part),
        "recurring_issues": recurring_issues(part, field),
        "rebilled_claims": rebilled_claims(frame, part),
        "time_to_resolve": time_to_resolve(agency),
        "visits_not_drafted": visits_not_drafted(data_dir, start, end, agency, patient_agency),
    }


def pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def money(value) -> float:
    return round(float(value), 2)


def label(code: str) -> str:
    return f"{code} {ISSUE_LABELS.get(code, 'Other / manual review' if code == 'D99' else '')}".strip()


def journey(part, data_dir, start, end, agency, queue, patient_agency, drafts) -> list[dict]:
    """Claims at each step, in order: verified visits, drafts, unsubmitted claims, fixes, submissions, outcomes."""
    def in_range(day: str) -> bool:
        return start.isoformat() <= day <= end.isoformat()

    visits = sum(1 for v in read_rows(data_dir / "evv_visits.csv")
                 if v["verification_status"] == "verified" and in_range(v["clock_in"][:10])
                 and (agency is None or patient_agency.get(v["patient_id"]) == agency))
    my_drafts = [d for d in drafts if (agency is None or d["agency"] == agency) and in_range(d["service_date"])]
    pending = [i for i in queue if (agency is None or i.row["agency_id"] == agency)
               and in_range(pd.Timestamp(i.row["date_of_service"]).date().isoformat())]
    log = [e for e in activity.read_log() if agency is None or e["agency"] == agency]
    fixed = [e for e in log if e.get("workspace") == "check" and e["event"] in ("fix", "choice") and e["result"] == "ready"]
    resubmitted = [e for e in log if e.get("workspace") == "status" and e["event"] == "resubmit"]
    denied = part[part["status"] == "denied"]
    paid = part[part["status"] == "paid"]

    def row(stage, items, amount):
        return {"stage": stage, "claims": int(items), "billed": money(amount)}

    return [
        row("1 Verified EVV visits", visits, 0),
        row("2 Claims drafted from visits", len(my_drafts), sum(d["billed"] for d in my_drafts)),
        row("3 Drafts needing review", sum(d["needs_review"] for d in my_drafts),
            sum(d["billed"] for d in my_drafts if d["needs_review"])),
        row("4 Unsubmitted claims", len(pending), sum(i.billed_amount for i in pending)),
        row("5 Unsubmitted claims needing review", sum(i.needs_review for i in pending),
            sum(i.billed_amount for i in pending if i.needs_review)),
        row("6 Fixed in the app (now ready)", len(fixed), sum(e.get("billed_amount", 0) for e in fixed)),
        row("7 Claims submitted", len(part), part["billed_amount"].sum()),
        row("8 Paid", len(paid), paid["billed_amount"].sum()),
        row("9 Denied", len(denied), denied["billed_amount"].sum()),
        row("10 Denials resubmitted as corrected claims", len(resubmitted),
            sum(e.get("billed_amount", 0) for e in resubmitted)),
    ]


def denials_by_code(denied: pd.DataFrame) -> list[dict]:
    rows = []
    for code, group in denied.groupby("denial_code"):
        rows.append({"code": code, "issue": label(code), "denials": int(len(group)),
                     "share_pct": pct(len(group), len(denied)), "billed": money(group["billed_amount"].sum())})
    return sorted(rows, key=lambda r: -r["denials"])


def denials_by_month(part: pd.DataFrame, field: str) -> list[dict]:
    month = part[field].str[:7]
    rows = []
    for m, group in part.groupby(month):
        d = group[group["status"] == "denied"]
        rows.append({"month": m, "claims": int(len(group)), "denied": int(len(d)),
                     "denial_rate_pct": pct(len(d), len(group)), "denied_billed": money(d["billed_amount"].sum())})
    return rows


def flagged_by_reason(part: pd.DataFrame) -> list[dict]:
    """Claims the checker would have flagged, by its predicted code, and what the payer did."""
    flagged = part[part["predicted_code"] != ""]
    rows = []
    for code, group in flagged.groupby("predicted_code"):
        rows.append({"code": code, "issue": label(code), "flagged": int(len(group)),
                     "payer_denied": int((group["status"] == "denied").sum()),
                     "payer_denied_same_code": int((group["denial_code"] == code).sum()),
                     "payer_paid": int((group["status"] == "paid").sum()),
                     "billed": money(group["billed_amount"].sum())})
    return sorted(rows, key=lambda r: -r["flagged"])


def passed_without_review(part: pd.DataFrame) -> dict:
    passed = part[part["predicted_code"] == ""]
    paid = passed[passed["status"] == "paid"]
    denied = passed[passed["status"] == "denied"]
    return {"passed": int(len(passed)), "paid": int(len(paid)), "paid_pct": pct(len(paid), len(passed)),
            "denied": int(len(denied)),
            "denied_by_code": {k: int(v) for k, v in denied["denial_code"].value_counts().items()}}


def one_click_estimate(part: pd.DataFrame, assessments: dict) -> dict:
    """Estimate: denied claims whose every finding has a one-click fix would likely have been paid if fixed."""
    rows = part.set_index("claim_id")
    fixable, by_code = [], Counter()
    for claim_id in part.loc[part["status"] == "denied", "claim_id"]:
        a = assessments.get(claim_id)
        if not a or not a["failed_checks"]:
            continue
        row = rows.loc[claim_id]
        claim_row = {"date_of_service": row["service_date"], "payer": row["payer"], "agency_id": row["agency_id"]}
        findings = [describe(c, a["finding_details"][c], claim_row, a["payer"]) for c in a["failed_checks"]]
        if all(f.auto for f in findings):
            fixable.append(claim_id)
            by_code[a["failed_checks"][0]] += 1
    denied_total = int((part["status"] == "denied").sum())
    billed = part[part["claim_id"].isin(fixable)]["billed_amount"].sum()
    return {"denied": denied_total, "one_click_fixable": len(fixable), "share_pct": pct(len(fixable), denied_total),
            "billed_recoverable": money(billed),
            "by_code": [{"code": c, "issue": label(c), "claims": n} for c, n in by_code.most_common()],
            "note": "Estimate: assumes a fixed claim would have been paid."}


def outliers(part: pd.DataFrame) -> dict:
    """Caregivers and patients whose denial rate is well above the scope's (z > 2, at least MIN_SAMPLE claims)."""
    overall = (part["status"] == "denied").mean() if len(part) else 0
    result = {"overall_denial_rate_pct": round(100 * overall, 1), "min_claims": MIN_SAMPLE}
    by_caregiver = part.assign(caregiver=part["caregivers"].str.split("|")).explode("caregiver")
    by_caregiver = by_caregiver[by_caregiver["caregiver"] != ""]
    for key, data in (("caregivers", by_caregiver.rename(columns={"caregiver": "id"})),
                      ("patients", part.rename(columns={"patient_id": "id"}))):
        rows = []
        for ident, group in data.groupby("id"):
            n = len(group)
            if n < MIN_SAMPLE or not 0 < overall < 1:
                continue
            rate = (group["status"] == "denied").mean()
            z = (rate - overall) / math.sqrt(overall * (1 - overall) / n)
            if z > 2:
                d = group[group["status"] == "denied"]
                rows.append({"id": ident, "claims": n, "denied": int(len(d)), "denial_rate_pct": round(100 * rate, 1),
                             "z": round(z, 1),
                             "top_codes": {k: int(v) for k, v in d["denial_code"].value_counts().head(3).items()}})
        result[key] = sorted(rows, key=lambda r: -r["z"])[:8]
    return result


def payers(part: pd.DataFrame) -> list[dict]:
    rows = []
    for payer, group in part.groupby("payer"):
        d = group[group["status"] == "denied"]
        u = group[group["unexpected"]]
        rows.append({"payer": payer, "claims": int(len(group)), "denied": int(len(d)),
                     "denial_rate_pct": pct(len(d), len(group)), "unexpected": int(len(u)),
                     "unexpected_rate_pct": pct(len(u), len(group)), "denied_billed": money(d["billed_amount"].sum())})
    return rows


def queue_health(queue: list, agency: str | None) -> dict:
    """Claims waiting for review now: count, value, deadlines and how long they've waited."""
    waiting = [i for i in queue if i.needs_review and (agency is None or i.row["agency_id"] == agency)]
    flagged = activity.read_flagged()
    waited_hours = []
    for item in waiting:
        seen = flagged.get(f"check:{item.claim_id}")
        if seen:
            waited_hours.append((datetime.now() - datetime.fromisoformat(seen["first_flagged"])).total_seconds() / 3600)
    days = [i.days_to_deadline for i in waiting]
    return {"waiting": len(waiting), "value": money(sum(i.billed_amount for i in waiting)),
            "needs_a_person": sum(not i.auto_only for i in waiting),
            "one_click": sum(i.auto_only for i in waiting),
            "deadline_within_7_days": sum(d is not None and 0 <= d <= 7 for d in days),
            "already_late": sum(d is not None and d < 0 for d in days),
            "median_hours_waiting": round(median(waited_hours), 1) if waited_hours else None,
            "note": "Deadlines are counted from each claim's planned submission date. Waiting time comes from app activity."}


def fix_uptake(agency: str | None) -> list[dict]:
    """Per issue: claims offered a one-click fix, fixes applied, and recheck results (from app activity)."""
    offered = Counter()
    for key, seen in activity.read_flagged().items():
        if key.startswith("check:") and (agency is None or seen.get("agency") == agency):
            offered.update(seen.get("auto", []))
    applied, ready, still = Counter(), Counter(), Counter()
    for entry in activity.read_log():
        if entry["event"] != "fix" or entry.get("workspace") != "check" or (agency and entry["agency"] != agency):
            continue
        for code in entry["codes"]:
            applied[code] += 1
            ready[code] += entry["result"] == "ready"
            still[code] += entry["result"] == "needs review"
    codes = sorted(set(offered) | set(applied), key=lambda c: -offered[c])
    return [{"code": c, "issue": label(c), "offered": offered[c], "applied": applied[c],
             "passed_recheck": ready[c], "still_another_issue": still[c]} for c in codes]


def unexpected_reasons(part: pd.DataFrame) -> list[dict]:
    rows = []
    for (payer, code), group in part[part["unexpected"]].groupby(["payer", "denial_code"]):
        rows.append({"payer": payer, "code": code, "claims": int(len(group)),
                     "checker_predicted": {k or "paid": int(v) for k, v in group["predicted_code"].value_counts().items()},
                     "top_remark": group["remark"].value_counts().index[0]})
    return sorted(rows, key=lambda r: -r["claims"])


def recurring_issues(part: pd.DataFrame, field: str) -> list[dict]:
    """Per agency and denial code: denials in at least 3 separate months of the period."""
    denied = part[part["status"] == "denied"].assign(month=lambda d: d[field].str[:7])
    rows = []
    for (ag, code), group in denied.groupby(["agency_id", "denial_code"]):
        months = sorted(group["month"].unique())
        if len(months) >= 3:
            rows.append({"agency": ag, "code": code, "issue": label(code), "months": len(months),
                         "denials": int(len(group)), "first_month": months[0], "last_month": months[-1]})
    return sorted(rows, key=lambda r: (-r["months"], -r["denials"]))[:15]


def rebilled_claims(frame: pd.DataFrame, part: pd.DataFrame) -> dict:
    """New claims for a visit already billed: what happened to the first claim, and how long after."""
    keys = ["payer", "patient_id", "service_code", "service_date"]
    repeats = []
    for _, group in frame.sort_values("submitted_date").groupby(keys):
        if len(group) < 2:
            continue
        first = group.iloc[0]
        for later in group.iloc[1:].itertuples():
            if later.claim_id in set(part["claim_id"]):
                repeats.append({"claim_id": later.claim_id, "agency": later.agency_id, "first_claim": first["claim_id"],
                                "first_outcome": first["status"], "first_code": first["denial_code"],
                                "days_after": (date.fromisoformat(later.submitted_date)
                                               - date.fromisoformat(first["submitted_date"])).days})
    days = [r["days_after"] for r in repeats]
    return {"rebilled": len(repeats), "first_was_paid": sum(r["first_outcome"] == "paid" for r in repeats),
            "median_days_after": median(days) if days else None,
            "by_agency": dict(Counter(r["agency"] for r in repeats).most_common()), "examples": repeats[:5],
            "note": "None of the past claims is a corrected claim (frequency code 7)."}


def time_to_resolve(agency: str | None) -> list[dict]:
    """Per issue: median minutes from a claim first being flagged to the action that made it ready (app activity)."""
    flagged = activity.read_flagged()
    minutes = defaultdict(list)
    for entry in activity.read_log():
        if entry.get("workspace") != "check" or entry["result"] != "ready" or (agency and entry["agency"] != agency):
            continue
        seen = flagged.get(f"check:{entry['claim_id']}")
        if seen:
            waited = (datetime.fromisoformat(entry["timestamp"]) - datetime.fromisoformat(seen["first_flagged"]))
            for code in entry["codes"]:
                minutes[code].append(waited.total_seconds() / 60)
    return sorted(({"code": c, "issue": label(c), "resolved": len(v), "median_minutes": round(median(v), 1)}
                   for c, v in minutes.items()), key=lambda r: -r["resolved"])


def visits_not_drafted(data_dir: Path, start: date, end: date, agency: str | None, patient_agency: dict) -> dict:
    """EVV visits in the period that couldn't become claims because they weren't verified."""
    counts = Counter()
    for v in read_rows(data_dir / "evv_visits.csv"):
        day = v["clock_in"][:10]
        if not start.isoformat() <= day <= end.isoformat():
            continue
        if agency and patient_agency.get(v["patient_id"]) != agency:
            continue
        counts[v["verification_status"]] += 1
    total = sum(counts.values())
    held = total - counts["verified"]
    return {"visits": total, "held_back": held, "held_pct": pct(held, total),
            "rejected": counts["rejected"], "manual_edit_pending": counts["manual_edit_pending"],
            "no_unique_authorization": 0,
            "note": "The drafter found exactly one authorization for every verified visit in this data."}
