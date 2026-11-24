"""Analysis over past claims: one table with derived fields, and the query tools Claude can call.

Every tool returns plain data computed by Python. Claude can only report what these return.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from checker.claim_checker import Claim, read_rows

# Fields Claude may filter or group by, with a short description it sees.
FIELDS = {
    "agency_id": "agency", "payer": "payer ID (PY01 Northwind, PY02 Harbor, PY03 Summit)",
    "patient_id": "patient", "service_code": "T1019 personal care, T1005 respite, S5125 attendant care",
    "service_month": "month of the date of service (YYYY-MM)", "submitted_month": "month submitted (YYYY-MM)",
    "weekday": "day of the week of the date of service", "units": "billed units",
    "units_mod_4": "billed units modulo 4 (0 = whole hours)", "modifier": "modifier on the claim",
    "frequency_code": "1 new claim, 7 corrected claim", "has_auth_id": "whether an authorization ID was given",
    "visit_status": "EVV status for the date: verified, rejected, manual_edit_pending, none",
    "days_to_submit_bucket": "days from service to submission, bucketed",
    "gap_bucket": "billed minus verified minutes, bucketed", "status": "paid or denied",
    "denial_code": "payer's denial code", "remark": "payer's remark",
    "predicted_code": "checker's predicted code ('' = predicted paid)", "unexpected": "denied with a code the checker didn't predict",
}
NUMERIC = {"units", "verified_minutes", "billed_minus_verified_minutes", "days_to_submit", "billed_amount"}


def build_frame(data_dir: Path, history_results: list[dict]) -> pd.DataFrame:
    """One row per unique historical claim with derived fields and the checker's prediction.

    history_results: rows from checker.replay_all for the claims history.
    """
    predicted = {r["claim_id"]: r for r in history_results}
    visits = defaultdict(list)
    for v in read_rows(data_dir / "evv_visits.csv"):
        visits[v["patient_id"], v["service_code"].strip().upper(), v["clock_in"][:10]].append(v)

    rows = []
    for row in {tuple(r.items()): r for r in read_rows(data_dir / "claims_history.csv")}.values():
        c = Claim.from_row(row)
        day = visits.get((c.patient_id, c.service_code, c.service_date.isoformat()), [])
        verified = [v for v in day if v["verification_status"] == "verified"]
        minutes = sum((datetime.fromisoformat(v["clock_out"]) - datetime.fromisoformat(v["clock_in"])).total_seconds()
                      for v in verified) / 60 if verified else None
        statuses = {v["verification_status"] for v in day}
        p = predicted.get(c.claim_id, {})
        days = (c.submission_date - c.service_date).days
        gap = c.units * 15 - minutes if minutes is not None else None
        rows.append({
            "claim_id": c.claim_id, "agency_id": row["agency_id"], "payer": c.payer, "patient_id": c.patient_id,
            "service_code": c.service_code, "service_date": c.service_date.isoformat(),
            "submitted_date": c.submission_date.isoformat(), "service_month": c.service_date.isoformat()[:7],
            "submitted_month": c.submission_date.isoformat()[:7], "weekday": c.service_date.strftime("%A"),
            "units": c.units, "units_mod_4": c.units % 4, "modifier": c.modifier or "(none)",
            "frequency_code": c.frequency_code, "original_claim_id": c.original_claim_id,
            "has_auth_id": bool(c.auth_id), "auth_id": c.auth_id,
            "billed_amount": float(row["billed_amount"] or 0),
            "visit_status": "verified" if verified else (sorted(statuses)[0] if statuses else "none"),
            "caregivers": "|".join(sorted({v["caregiver_id"] for v in day})),
            "verified_minutes": minutes, "billed_minus_verified_minutes": gap,
            "gap_bucket": bucket_gap(gap), "days_to_submit": days, "days_to_submit_bucket": bucket_days(days),
            "status": row["status"], "denial_code": row["denial_code"], "remark": row["remark"],
            "predicted_status": p.get("predicted_status", ""), "predicted_code": p.get("predicted_denial_code", ""),
            "failed_checks": p.get("failed_checks", ""),
        })
    frame = pd.DataFrame(rows)
    frame["unexpected"] = (frame["status"] == "denied") & (frame["denial_code"] != frame["predicted_code"])
    return frame


def bucket_gap(gap: float | None) -> str:
    if gap is None:
        return "no verified visit"
    if gap <= 0:
        return "within verified time"
    if gap < 15:
        return "0-15 min over"
    if gap < 30:
        return "15-30 min over"
    if gap < 60:
        return "30-60 min over"
    return "60+ min over"


def bucket_days(days: int) -> str:
    for limit in (15, 30, 45, 60, 90):
        if days <= limit:
            return f"<= {limit} days"
    return "> 90 days"


# ---------- tools ----------

def apply_filters(frame: pd.DataFrame, filters: dict | None) -> pd.DataFrame:
    """Filter by field values (a value or a list), date ranges and remark text.

    Supported keys: any field in FIELDS, service_date_from/to, submitted_date_from/to, remark_contains.
    """
    out = frame
    for key, value in (filters or {}).items():
        if value in (None, "", []):
            continue
        if key.endswith("_from") or key.endswith("_to"):
            column = key.rsplit("_", 1)[0]
            out = out[out[column] >= value] if key.endswith("_from") else out[out[column] <= value]
        elif key == "remark_contains":
            out = out[out["remark"].str.contains(str(value), case=False, na=False)]
        elif key in out.columns:
            values = value if isinstance(value, list) else [value]
            column = out[key]
            if column.dtype == bool:
                values = [v in (True, "true", "True", 1) for v in values]
            elif pd.api.types.is_numeric_dtype(column):
                values = [float(v) for v in values]
            out = out[column.isin(values)]
        else:
            raise ValueError(f"Unknown filter field: {key}")
    return out


def breakdown(frame: pd.DataFrame, filters: dict | None = None, group_by: list[str] = ()) -> dict:
    """Count claims, paid, denied (by code) and billed amount, overall or per group (up to 2 fields)."""
    data = apply_filters(frame, filters)
    for field in group_by:
        if field not in FIELDS:
            raise ValueError(f"Can't group by {field}")
    if not group_by:
        return {"filters": filters or {}, "total": summarise(data)}
    groups = []
    for key, part in data.groupby(list(group_by)):
        key = key if isinstance(key, tuple) else (key,)
        groups.append({**dict(zip(group_by, [str(k) for k in key])), **summarise(part)})
    groups.sort(key=lambda g: -g["claims"])
    return {"filters": filters or {}, "group_by": list(group_by), "groups": groups[:60],
            "groups_shown": min(len(groups), 60), "groups_total": len(groups)}


def summarise(part: pd.DataFrame) -> dict:
    denied = part[part["status"] == "denied"]
    return {
        "claims": int(len(part)), "paid": int((part["status"] == "paid").sum()), "denied": int(len(denied)),
        "denial_rate_pct": round(100 * len(denied) / len(part), 1) if len(part) else None,
        "denied_by_code": {k: int(v) for k, v in denied["denial_code"].value_counts().items()},
        "unexpected_denials": int(part["unexpected"].sum()),
        "billed_amount": round(float(part["billed_amount"].sum()), 2),
    }


def sample_claims(frame: pd.DataFrame, filters: dict | None = None, n: int = 10) -> dict:
    """Return up to 15 matching claims with their key fields."""
    data = apply_filters(frame, filters)
    columns = ["claim_id", "agency_id", "payer", "patient_id", "service_code", "service_date", "submitted_date",
               "weekday", "units", "modifier", "frequency_code", "verified_minutes", "visit_status", "caregivers",
               "status", "denial_code", "remark", "predicted_code"]
    sample = data.sort_values("service_date").head(min(int(n), 15))[columns]
    return {"matching": int(len(data)), "claims": json.loads(sample.to_json(orient="records"))}


def distribution(frame: pd.DataFrame, field: str, filters: dict | None = None) -> dict:
    """Quantiles and the most common values of a numeric field for matching claims."""
    if field not in NUMERIC:
        raise ValueError(f"Distribution needs one of {sorted(NUMERIC)}")
    values = apply_filters(frame, filters)[field].dropna()
    if values.empty:
        return {"field": field, "count": 0}
    return {
        "field": field, "count": int(len(values)),
        "quantiles": {q: round(float(values.quantile(q / 100)), 1) for q in (0, 10, 25, 50, 75, 90, 100)},
        "most_common": {str(k): int(v) for k, v in values.round(0).value_counts().head(10).items()},
    }


def timeline(data_dir: Path, frame: pd.DataFrame, kind: str, identifier: str) -> dict:
    """Events in date order for a patient, caregiver or claim: visits, claims and their outcomes."""
    if kind == "claim":
        match = frame[frame["claim_id"] == identifier]
        if match.empty:
            return {"error": f"No claim {identifier}"}
        kind, identifier = "patient", match.iloc[0]["patient_id"]
    if kind not in ("patient", "caregiver"):
        raise ValueError("kind must be patient, caregiver or claim")
    events = []
    for v in read_rows(data_dir / "evv_visits.csv"):
        if (kind == "patient" and v["patient_id"] == identifier) or (kind == "caregiver" and v["caregiver_id"] == identifier):
            events.append({"date": v["clock_in"][:10], "event": "visit", "visit_id": v["visit_id"],
                           "patient": v["patient_id"], "caregiver": v["caregiver_id"], "service": v["service_code"],
                           "clock_in": v["clock_in"][11:], "clock_out": v["clock_out"][11:],
                           "status": v["verification_status"]})
    claims = frame[frame["patient_id"] == identifier] if kind == "patient" else \
        frame[frame["caregivers"].str.split("|").apply(lambda cg: identifier in cg)]
    for c in claims.itertuples():
        events.append({"date": c.submitted_date, "event": "claim submitted", "claim_id": c.claim_id,
                       "for_service_date": c.service_date, "service": c.service_code, "units": c.units,
                       "frequency_code": c.frequency_code, "original_claim_id": c.original_claim_id,
                       "outcome": c.status, "denial_code": c.denial_code, "remark": c.remark})
    events.sort(key=lambda e: (e["date"], e["event"]))
    return {"kind": kind, "id": identifier, "events": events[:80], "events_total": len(events)}


def unexpected_groups(frame: pd.DataFrame, minimum: int = 5) -> list[dict]:
    """Denials the checker didn't predict, grouped by payer, payer's code and remark."""
    data = frame[frame["unexpected"]]
    groups = []
    for (payer, code, remark), part in data.groupby(["payer", "denial_code", "remark"]):
        if len(part) >= minimum:
            groups.append({
                "key": f"{payer}|{code}|{remark}", "payer": payer, "denial_code": code, "remark": remark,
                "claims": int(len(part)), "billed_amount": round(float(part["billed_amount"].sum()), 2),
                "predicted": {k or "paid": int(v) for k, v in part["predicted_code"].value_counts().items()},
                "first_service_date": part["service_date"].min(), "last_service_date": part["service_date"].max(),
                "claim_ids": part["claim_id"].tolist(),
            })
    groups.sort(key=lambda g: -g["claims"])
    return groups
