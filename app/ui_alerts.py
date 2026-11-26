"""Payer bulletin approvals and agency rule-change notices."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from app import ai, rules_admin
from checker.claim_checker import read_rows

RULE_LABELS = {"filing_limit": "Filing deadline", "weekend_modifier": "Modifier change",
               "whole_hour_units": "Whole-hour billing"}


def agency_alerts(agency: str) -> None:
    """Rule changes approved by the rules team, with how many of this agency's pending claims each affects."""
    approved = [c for c in rules_admin.read_changes() if c["decision"] == "approved"]
    if not approved:
        st.markdown("No rule changes yet. When Exchange4Health approves a payer rule change, it appears here.")
        return
    for change in reversed(approved):
        affected = change.get("pending_by_agency", {}).get(agency, 0)
        with st.container(border=True):
            st.markdown(f"**Rule changed:** {change['summary']}")
            st.markdown(
                f"Source: {change['change'].get('source', change['title'])} · approved {change['timestamp'][:16].replace('T', ' ')}  \n"
                + (f"**{affected} of your pending claims** are affected. Their checks on the Check claims page now use this rule."
                   if affected else "None of your pending claims are affected.")
            )


def rules_team_alerts(data_dir: Path, rules: dict) -> bool:
    """Show the bulletin inbox, active rules, and past decisions."""
    agencies = {r["claim_id"]: r["agency_id"] for r in read_rows(data_dir / "claims_to_check.csv")}
    bulletins = rules_admin.list_bulletins(data_dir)
    inbox, active, decisions = st.tabs(["Bulletins", "Rules in force", "Decision log"])
    with inbox:
        changed = bulletin_section(data_dir, rules, bulletins, agencies)
    with active:
        rules_section(rules)
    with decisions:
        log = rules_admin.read_changes()
        if log:
            columns = ("timestamp", "kind", "title", "decision", "summary", "note")
            st.dataframe(pd.DataFrame([{k: c[k] for k in columns} for c in reversed(log)]),
                         hide_index=True, width="stretch")
        else:
            st.markdown("No decisions yet.")
    return changed


def md(text: str) -> str:
    """Escape dollar signs so Streamlit doesn't render amounts as maths."""
    return str(text).replace("$", "\\$")


# ---------- bulletins ----------

def bulletin_section(data_dir, rules, bulletins, agencies) -> bool:
    with st.expander("Paste a new bulletin"):
        with st.form("paste_bulletin", clear_on_submit=True):
            title = st.text_input("Title", placeholder="e.g. Summit Network Notice 2026-11")
            text = st.text_area("Bulletin text", height=160)
            if st.form_submit_button("Add to inbox") and title and text:
                rules_admin.save_pasted_bulletin(title, f"# {title}\n\n{text}")
                st.rerun()
    if not bulletins:
        st.markdown("No bulletins.")
        return False
    order = {"new": 0, "approved": 1, "in rules": 2}
    for bulletin in sorted(bulletins, key=lambda b: order.get(b["status"], 3)):
        with st.container(border=True):
            st.markdown(f"### {md(bulletin['title'])}")
            st.caption(bulletin["status"].capitalize())
            with st.expander("Original bulletin"):
                st.markdown(bulletin["text"])
            if bulletin["status"] == "new" and bulletin_proposal(data_dir, rules, bulletin, agencies):
                return True
    return False


def bulletin_proposal(data_dir, rules, bulletin, agencies) -> bool:
    key = f"bulletin_{bulletin['name']}"
    reading = st.session_state.get(key)
    if not isinstance(reading, dict):
        reading = None
    action = "Read again" if reading else "Read with Claude"
    if st.button(action, key=f"read_{key}", type="primary" if not reading else "secondary"):
        try:
            with st.spinner("Claude is reading the bulletin…"):
                out = ai.read_bulletin(bulletin["name"], bulletin["text"], rules, refresh=bool(reading))
        except Exception as error:
            st.error(f"Couldn't read the bulletin: {error}")
            return False
        reading = st.session_state[key] = out["result"]
    if not reading:
        return False

    if reading["outcome"] != "rule_change":
        st.warning(("Needs a developer: " if reading["outcome"] == "needs_developer" else "No rule change: ")
                   + md(reading["reason_if_no_change"]))
        cols = st.columns(2)
        if reading["outcome"] == "needs_developer" and cols[0].button("Send to developers", key=f"dev_{key}"):
            rules_admin.record_change("bulletin", bulletin["name"], {}, {}, "sent to developers", reading["reason_if_no_change"])
            rules_admin.set_bulletin_status(bulletin["name"], "sent to developers")
            st.rerun()
        if cols[1].button("Mark as read", key=f"read_done_{key}"):
            rules_admin.record_change("bulletin", bulletin["name"], {}, {}, "no change")
            rules_admin.set_bulletin_status(bulletin["name"], "no change")
            st.rerun()
        return False

    for n, proposal in enumerate(reading["changes"]):
        change = {k: proposal[k] for k in ("rule_type", "payer", "days", "service_code", "modifier",
                                           "effective_from", "effective_to")}
        change["source"] = bulletin["title"]
        problems = rules_admin.validate_change(change, bulletin["text"], proposal["evidence_quotes"])
        if problems:
            st.error("Can't approve: " + " ".join(problems))
            continue
        if approval_block(data_dir, rules, change, agencies, f"{key}_{n}", bulletin["name"]):
            rules_admin.set_bulletin_status(bulletin["name"], "approved")
            return True
    if st.button("Reject", key=f"reject_{key}"):
        rules_admin.record_change("bulletin", bulletin["name"], {}, {}, "rejected")
        rules_admin.set_bulletin_status(bulletin["name"], "rejected")
        st.rerun()
    return False


def rule_detail(change: dict) -> str:
    """Describe the check without repeating the payer or service dates."""
    if change["rule_type"] == "weekend_modifier":
        return f"Require modifier {change['modifier']} for {change['service_code']} on Saturdays and Sundays."
    if change["rule_type"] == "filing_limit":
        return f"Submit within {change['days']} calendar days of the date of service."
    return "Bill in whole hours, in multiples of 4 units."


def approval_block(data_dir, rules, change, agencies, key, title) -> bool:
    """Display a proposed rule and preview the pending claims it affects."""
    st.markdown("#### Proposed rule")
    st.markdown(f"**Applies to**  {rules_admin.PAYERS[change['payer']]}  \n"
                f"**Rule**  {RULE_LABELS[change['rule_type']]}  \n"
                f"**Detail**  {rule_detail(change)}")
    st.markdown("**Period covered**")
    dates = {}
    cols = st.columns(2)
    for column, field, label in zip(cols, ("effective_from", "effective_to"), ("Start", "End")):
        suggested = change[field]
        value = date.fromisoformat(suggested) if suggested else None
        origin = f"Suggested by Claude: {value:%d %b %Y}." if value else f"{label} date unspecified in bulletin."
        edited = column.date_input(f"{label} date", value=value, min_value=date(1900, 1, 1),
                                   max_value=date(2100, 12, 31), key=f"{key}_{field}_{suggested}",
                                   help=f"{origin} Edit or clear the date.")
        if edited is None:
            column.caption(f"{label} date unspecified")
        dates[field] = edited.isoformat() if edited else None
    final = {**change, **dates}
    problems = rules_admin.validate_change(final)
    if problems:
        st.error("Can't approve: " + " ".join(problems))
        return False

    overrides = rules_admin.overridden_rules(rules, final)
    st.markdown("**Overrides**")
    if overrides:
        for item in overrides:
            source = item["rule"].get("source") or "Existing rule"
            period = rules_admin.describe_period(item["from"], item["to"])
            st.markdown(f"- {md(source)}{period or ', for all service dates'}")
    elif final["rule_type"] == "weekend_modifier":
        st.write("Harbor’s standard U2 modifier for the covered weekends.")
    else:
        st.write("None")

    impact = cached_impact(data_dir, rules, final, agencies)
    pending = impact["pending_changes"]
    st.markdown(f"**Pending claims affected**  {len(pending):,}")
    if pending:
        with st.expander("View affected claims"):
            st.dataframe(pd.DataFrame(pending).rename(columns={
                "claim_id": "Claim", "agency": "Agency", "before": "Before", "after": "After",
            }), hide_index=True, width="stretch")
    if st.button("Approve rule change", key=f"approve_{key}", type="primary"):
        rules_admin.approve("bulletin", title, final, impact, data_dir=data_dir)
        return True
    return False


def cached_impact(data_dir, rules, change, agencies):
    key = ("impact", repr(sorted(change.items())), repr(rules))
    if key not in st.session_state:
        with st.spinner("Checking pending claims…"):
            st.session_state[key] = rules_admin.impact(data_dir, rules, rules_admin.apply_change(rules, change), agencies)
    return st.session_state[key]


def rules_section(rules: dict) -> None:
    st.markdown("Dated payer rules the checker reads from its settings. Other rules (EVV, units, authorizations, "
                "duplicates, Harbor's daily cap) are fixed in the checker code. The plain-language flowcharts are in "
                "[checker/README.md](https://github.com/Mariam-18/exchange4health-claims/blob/main/checker/README.md).")
    rows = []
    for section in ("filing_limits", "weekend_modifiers", "whole_hour_units"):
        for entry in rules.get(section, []):
            rows.append({"Rule": rules_admin.describe_rule(section, entry), "Source": entry["source"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
