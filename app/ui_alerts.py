"""Alerts page: new payer bulletins and unexpected denials for the rules team; rule-change notices for agencies."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from app import ai, monitoring, rules_admin
from app.analysis import unexpected_groups
from checker.claim_checker import read_rows

PAYER_SHORT = {"PY01": "Northwind", "PY02": "Harbor", "PY03": "Summit"}


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


def rules_team_alerts(data_dir: Path, rules: dict, frame) -> bool:
    """Bulletins and unexpected denials awaiting a decision. Returns True if rules changed."""
    agencies = {r["claim_id"]: r["agency_id"] for r in read_rows(data_dir / "claims_to_check.csv")}
    bulletins = rules_admin.list_bulletins(data_dir)
    new_bulletins = [b for b in bulletins if b["status"] == "new"]
    handled = {c["title"] for c in rules_admin.read_changes()
               if c["kind"] == "unexpected" and c["decision"] in ("dismissed", "asked payer")}
    groups = [g for g in unexpected_groups(frame) if g["key"] not in handled]

    m = st.columns(3)
    m[0].metric("New bulletins", len(new_bulletins))
    m[1].metric("Unexpected denial groups", len(groups))
    m[2].metric("Decisions logged", len(rules_admin.read_changes()))

    tabs = st.tabs([f"Bulletins ({len(new_bulletins)} new)", f"Unexpected denials ({len(groups)})",
                    "Rules in force", "Decision log", "Demo controls"])
    with tabs[0]:
        changed = bulletin_section(data_dir, rules, bulletins, agencies)
    with tabs[1]:
        changed = unexpected_section(data_dir, rules, frame, groups) or changed
    with tabs[2]:
        rules_section(rules)
    with tabs[3]:
        log = rules_admin.read_changes()
        if log:
            st.dataframe(pd.DataFrame([{k: c[k] for k in ("timestamp", "kind", "title", "decision", "summary", "note")}
                                       for c in reversed(log)]), hide_index=True, width="stretch")
        else:
            st.markdown("No decisions yet.")
    with tabs[4]:
        changed = demo_controls(rules) or changed
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
    for b in sorted(bulletins, key=lambda b: order.get(b["status"], 3)):
        with st.expander(f"{b['name']} · {b['status']}", expanded=b["status"] == "new"):
            left, right = st.columns([2, 3])
            with left:
                st.markdown(b["text"])
            with right:
                if b["status"] == "new":
                    if bulletin_proposal(data_dir, rules, b, agencies):
                        return True
                else:
                    st.markdown(f"Status: **{b['status']}**.")
    return False


def bulletin_proposal(data_dir, rules, bulletin, agencies) -> bool:
    key = f"bulletin_{bulletin['name']}"
    if st.button("Read with Claude", key=f"read_{key}", type="primary") or st.session_state.get(key):
        try:
            with st.spinner("Claude is reading the bulletin…"):
                out = ai.read_bulletin(bulletin["name"], bulletin["text"], rules,
                                       refresh=st.session_state.pop(f"refresh_{key}", False))
        except Exception as error:
            st.error(f"Couldn't read the bulletin: {error}")
            return False
        st.session_state[key] = True
    else:
        st.markdown("Claude reads the bulletin and proposes a rule change. Nothing changes until you approve it.")
        return False

    reading = out["result"]
    st.markdown(f"**Claude's reading:** {md(reading['summary'])}")
    st.caption(f"{'Saved result' if out['from_cache'] else 'New result'} from {out['created'][:16].replace('T', ' ')} · {out['model']}")
    if st.button("Read again", key=f"again_{key}"):
        st.session_state[f"refresh_{key}"] = True
        st.rerun()
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
        title = next((line.lstrip("# ").strip() for line in bulletin["text"].splitlines() if line.startswith("#")),
                     bulletin["name"].removesuffix(".md"))
        change["source"] = title
        st.divider()
        st.markdown(f"**Proposed rule:** {rules_admin.describe_change(change)}")
        st.markdown(md(proposal["explanation"]))
        st.markdown("**From the bulletin:**")
        for quote in proposal["evidence_quotes"]:
            st.markdown(f"> {md(quote)}")
        problems = rules_admin.validate_change(change, bulletin["text"], proposal["evidence_quotes"])
        if problems:
            st.error("Can't approve: " + " ".join(problems))
            continue
        if approval_block(data_dir, rules, change, agencies, f"{key}_{n}", "bulletin", bulletin["name"]):
            rules_admin.set_bulletin_status(bulletin["name"], "approved")
            return True
    if st.button("Reject", key=f"reject_{key}"):
        rules_admin.record_change("bulletin", bulletin["name"], {}, {}, "rejected")
        rules_admin.set_bulletin_status(bulletin["name"], "rejected")
        st.rerun()
    return False


def approval_block(data_dir, rules, change, agencies, key, kind, title, note: str = "") -> bool:
    """Show the impact of a change, let the reviewer set an end date, and approve it. Returns True on approval."""
    cols = st.columns([1, 1])
    set_end = cols[0].checkbox("Set an end date", key=f"end_on_{key}")
    default_end = date.fromisoformat(change.get("effective_to") or "2026-12-31")
    end = cols[1].date_input("Rule ends after", value=default_end, key=f"end_{key}") if set_end else None
    final = {**change, "effective_to": end.isoformat() if end else change.get("effective_to")}
    problems = rules_admin.validate_change(final)
    if problems:
        st.error("Can't approve: " + " ".join(problems))
        return False

    overrides = rules_admin.overridden_rules(rules, final)
    if overrides:
        section = rules_admin.RULE_SECTIONS[final["rule_type"]]
        st.markdown("**Existing rules this would override**")
        for item in overrides:
            old = item["rule"]
            period = rules_admin.describe_period(item["from"], item["to"])
            st.markdown(f"- {old.get('source') or 'Existing rule'} · "
                        f"{rules_admin.describe_rule(section, old)} Replaced{period or ' for all service dates'}.")

    impact = cached_impact(data_dir, rules, final, agencies)
    m = st.columns(3)
    m[0].metric("Past claims predicted correctly",
                f"{impact['history_correct_after']:,}",
                delta=f"{impact['history_correct_after'] - impact['history_correct_before']:+,}")
    m[1].metric("Past predictions changed", len(impact["history_changes"]),
                help="Of these, how many now match what the payer did is shown below.")
    m[2].metric("Pending claims affected", len(impact["pending_changes"]))
    st.markdown(
        f"{impact['history_changes_matching_payer']} of the {len(impact['history_changes'])} changed past predictions "
        f"now match the payer's actual decision. Pending claims affected by agency: "
        + (", ".join(f"{a} {n}" for a, n in sorted(impact["pending_by_agency"].items())) or "none") + "."
    )
    with st.expander("Claims whose prediction changes"):
        st.dataframe(pd.DataFrame(impact["history_changes"]), hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame(impact["pending_changes"]), hide_index=True, width="stretch")
    if st.button("Approve rule change", key=f"approve_{key}", type="primary"):
        rules_admin.approve(kind, title, final, impact, note)
        return True
    return False


def cached_impact(data_dir, rules, change, agencies):
    key = ("impact", repr(sorted(change.items())), repr(rules))
    if key not in st.session_state:
        with st.spinner("Replaying past and pending claims with the proposed rule…"):
            st.session_state[key] = rules_admin.impact(data_dir, rules, rules_admin.apply_change(rules, change), agencies)
    return st.session_state[key]


# ---------- unexpected denials ----------

def unexpected_section(data_dir, rules, frame, groups) -> bool:
    st.markdown(
        "Denials the payer made with a code the checker didn't predict, grouped by payer, code and remark "
        "(at least 5 claims). A growing group can mean a payer rule we don't know about."
    )
    if not groups:
        st.markdown("No unexpected denial groups.")
        return False
    for g in groups:
        title = (f"{PAYER_SHORT.get(g['payer'], g['payer'])} · {g['denial_code']} · “{g['remark']}” · "
                 f"{g['claims']} claims · ${g['billed_amount']:,.2f}")
        with st.expander(title):
            st.markdown(f"Dates of service {g['first_service_date']} to {g['last_service_date']}. The checker predicted: "
                        + ", ".join(f"{k} {v}" for k, v in g["predicted"].items()) + ".")
            key = f"investigate_{g['key']}"
            if st.button("Investigate with Claude", key=f"btn_{key}", type="primary") or st.session_state.get(key):
                st.session_state[key] = True
                try:
                    with st.spinner("Claude is working through the claims history…"):
                        out = monitoring.investigate(data_dir, rules, frame, g,
                                                     refresh=st.session_state.pop(f"refresh_{key}", False))
                except Exception as error:
                    st.error(f"Investigation failed: {error}")
                    continue
                if show_investigation(data_dir, rules, g, out, key):
                    return True
    return False


def show_investigation(data_dir, rules, group, out, key) -> bool:
    result = out["result"]
    outcome = {"rule_proposed": "Proposed rule", "no_pattern": "No pattern found", "ask_payer": "Ask the payer"}
    st.markdown(f"**{outcome[result['outcome']]}.** {md(result['interpretation'])}")
    st.markdown("**Why, from the data:**")
    for reason in result["why"]:
        st.markdown(f"- {md(reason)}")
    check = out["number_check"]
    if check["unverified"]:
        st.warning(f"{len(check['unverified'])} of {check['stated']} numbers Claude stated weren't found in the data "
                   f"it retrieved: {md(', '.join(check['unverified']))}. Check these before acting.")
    else:
        st.markdown(f"All {check['stated']} numbers Claude stated were found in the data it retrieved.")
    st.markdown(f"**Suggestion:** {md(result['suggestion'])}")
    st.caption(f"{'Saved result' if out['from_cache'] else 'New result'} from {out['created'][:16].replace('T', ' ')} · "
               f"{out['model']} · {len(out['tool_calls'])} tool calls")
    with st.expander("How Claude worked this out"):
        for n, call in enumerate(out["tool_calls"], 1):
            st.markdown(f"**{n}. {call['name']}** `{call['input']}`")
            st.json(call["output"], expanded=False)
    if st.button("Investigate again", key=f"again_{key}"):
        st.session_state[f"refresh_{key}"] = True
        st.rerun()

    proposal = result.get("proposed_change")
    if result["outcome"] == "rule_proposed" and proposal:
        change = {k: proposal[k] for k in ("rule_type", "payer", "days", "service_code", "modifier",
                                           "effective_from", "effective_to")}
        change["source"] = f"Inferred from denial remarks: {group['remark']}"
        st.divider()
        st.markdown(f"**Proposed rule (inferred):** {rules_admin.describe_change(change)}")
        bt = out.get("backtest", {})
        if bt and "error" not in bt:
            st.markdown(f"Python's backtest: explains **{bt['group_claims_explained']} of {bt['group_claims']}** claims "
                        f"in this group and would wrongly flag **{bt['paid_claims_wrongly_flagged']}** paid claims.")
        problems = rules_admin.validate_change(change)
        if problems:
            st.error("Can't approve: " + " ".join(problems))
        else:
            agencies = {r["claim_id"]: r["agency_id"] for r in read_rows(data_dir / "claims_to_check.csv")}
            if approval_block(data_dir, rules, change, agencies, key, "unexpected", group["key"],
                              note="Inferred rule; confirm with the payer."):
                return True
    cols = st.columns(2)
    if cols[0].button("Ask the payer", key=f"payer_{key}"):
        rules_admin.record_change("unexpected", group["key"], {}, {}, "asked payer", result["suggestion"])
        st.rerun()
    if cols[1].button("Dismiss", key=f"dismiss_{key}"):
        rules_admin.record_change("unexpected", group["key"], {}, {}, "dismissed", result["interpretation"])
        st.rerun()
    return False


# ---------- rules list and demo controls ----------

def rules_section(rules: dict) -> None:
    st.markdown("Dated payer rules the checker reads from its settings. Other rules (EVV, units, authorizations, "
                "duplicates, Harbor's daily cap) are fixed in the checker code. The plain-language flowcharts are in "
                "[checker/README.md](https://github.com/Mariam-18/exchange4health-claims/blob/main/checker/README.md).")
    rows = []
    for section in ("filing_limits", "weekend_modifiers", "whole_hour_units"):
        for entry in rules.get(section, []):
            rows.append({"Rule": rules_admin.describe_rule(section, entry), "Source": entry["source"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def demo_controls(rules: dict) -> bool:
    st.markdown("Set up the demo scenarios. These change the app's rules, not the repository's.")
    if st.button("Treat the Harbor weekend-modifier bulletin as new"):
        rules_admin.save_rules(rules_admin.remove_rules(rules, "weekend_modifiers", payer="PY02"))
        rules_admin.set_bulletin_status("PY02_harbor_bulletin_2026-08.md", "new")
        return True
    if st.button("Remove the inferred Summit whole-hour rule"):
        rules_admin.save_rules(rules_admin.remove_rules(rules, "whole_hour_units", payer="PY03"))
        return True
    if st.button("Restore the repository's rules"):
        rules_admin.reset_rules()
        return True
    if st.button("Clear the decision log", help="Agencies' rule-change notices are cleared too."):
        rules_admin.clear_changes()
        return True
    return False
