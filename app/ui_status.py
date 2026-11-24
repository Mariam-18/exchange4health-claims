"""Check claim status: submitted claims, the paid/denied split, and what to do about each denial."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from app import activity, status
from app.review import ISSUE_LABELS, SERVICE_NAMES, contact_message, long_date
from app.ui_common import ask_box, filter_claims, md, worklist
from checker.claim_checker import parse_date

GREEN, RED = "#2e7d32", "#c62828"


def ribbon(paid: int, denied: int, paid_amount: float, denied_amount: float) -> None:
    """A green/red bar sized by the share of claims paid and denied."""
    total = paid + denied
    if not total:
        return
    p = 100 * paid / total
    st.markdown(f"""
<div style="display:flex;height:64px;border-radius:8px;overflow:hidden;font-family:sans-serif;color:white;margin:8px 0 4px 0">
  <div style="width:{p:.1f}%;background:{GREEN};display:flex;flex-direction:column;justify-content:center;padding-left:16px">
    <div style="font-size:1.25rem;font-weight:600">{paid:,} paid · {p:.0f}%</div>
    <div style="font-size:0.85rem">${paid_amount:,.2f}</div>
  </div>
  <div style="width:{100 - p:.1f}%;background:{RED};display:flex;flex-direction:column;justify-content:center;padding-left:16px">
    <div style="font-size:1.25rem;font-weight:600">{denied:,} denied · {100 - p:.0f}%</div>
    <div style="font-size:0.85rem">${denied_amount:,.2f}</div>
  </div>
</div>""", unsafe_allow_html=True)


def status_page(data_dir: Path, agency: str, frame, assessments: dict, visits: dict, auths: dict) -> None:
    rows = status.history_rows(data_dir)
    as_of = status.today(rows)
    mine = frame[frame["agency_id"] == agency]
    paid, denied_frame = mine[mine["status"] == "paid"], mine[mine["status"] == "denied"]
    st.markdown(f"Claims {agency} has submitted, and what the payers decided. Today is taken as "
                f"**{long_date(as_of.isoformat())}**, the latest submission date in the data.")
    ribbon(len(paid), len(denied_frame), paid["billed_amount"].sum(), denied_frame["billed_amount"].sum())

    ask_box("status", f"{agency}, all submitted claims", data_dir, mine)

    unexpected = set(frame.loc[frame["unexpected"], "claim_id"])
    denied = status.denied_claims(rows, assessments, unexpected, agency)
    done = status.handled(agency)
    hide_done = st.toggle("Hide claims already resubmitted or resolved", value=True, key="status_hide_done")
    open_items = [d for d in denied if not (hide_done and d.claim_id in done)]
    groups = {g: [d for d in open_items if d.group == g] for g in status.GROUPS}
    resubmissions = status.read_resubmissions(agency)
    tabs = st.tabs([f"{label} ({len(groups[g])})" for g, label in status.GROUPS.items()]
                   + [f"Resubmissions ({len(resubmissions)})"])
    intro = {
        "quick": "Every issue the checker found has a one-click fix. Prepare a corrected claim (frequency code 7, "
                 "naming the original) with the fixes applied; it's re-checked as if submitted today.",
        "person": "Denials with an issue that needs a person: a missing or rejected visit, late filing, a used-up "
                  "authorization, or a gap too large to be rounding.",
        "duplicate": "The payer denied these as duplicates: a claim for the same patient, service and date was "
                     "already submitted. Usually the first claim was paid and there's nothing to resubmit.",
        "unexplained": "The payer's reason doesn't match anything the checker found. Ask the payer, and the rules "
                       "team sees these as unexpected denials.",
    }
    for tab, (g, items) in zip(tabs, groups.items()):
        with tab:
            st.markdown(intro[g])
            group_tab(data_dir, g, items, done, as_of, visits, auths)
    with tabs[-1]:
        resubmissions_tab(resubmissions, agency)


def columns_for(group: str, as_of, done):
    def base(d):
        r = d.row
        return {"Date of service": parse_date(r["date_of_service"]).isoformat(), "Payer": d.item.payer_name,
                "Claim": d.claim_id, "Patient": r["patient_id"], "Amount": d.item.billed_amount,
                "Submitted": parse_date(r["submitted_date"]).isoformat()}

    def cols(d):
        payer_said = f"{d.payer_code} {ISSUE_LABELS.get(d.payer_code, 'Other')}"
        if group == "quick":
            extra = {"Fix": " ".join(f.fix_text for f in d.item.findings),
                     "Deadline": status.deadline_status(d.item, as_of)[0]}
        elif group == "person":
            extra = {"Needs someone": ", ".join(f.label for f in d.item.findings if not f.auto)}
        else:
            extra = {"Payer's remark": d.remark}
        return {"Payer said": payer_said, **extra, **base(d), "Status": done.get(d.claim_id, "")}
    return cols


def group_tab(data_dir, group, items, done, as_of, visits, auths) -> None:
    shown = filter_claims([d.item for d in items], f"status_{group}", visits)
    ids = {i.claim_id for i in shown}
    items = sorted([d for d in items if d.claim_id in ids], key=lambda d: d.row["date_of_service"])
    if not items:
        st.markdown("Nothing here.")
        return
    clicked, ticked = worklist(items, f"status_{group}_list", columns_for(group, as_of, done), ticks=group == "quick")
    if group == "quick":
        bar = st.columns([1, 1, 4])
        if bar[0].button(f"Prepare {len(ticked)} ticked", disabled=not ticked, type="primary", key="prep_ticked"):
            prepare(data_dir, ticked, as_of)
        if bar[1].button(f"Prepare all {len(items)}", key="prep_all"):
            prepare(data_dir, items, as_of)
    slot = f"status_{group}_open"
    if clicked:
        st.session_state[slot] = clicked.claim_id
    current = clicked or next((d for d in items if d.claim_id == st.session_state.get(slot)), None)
    if current:
        panel(data_dir, current, as_of, done, visits, auths)
    else:
        st.markdown("Click on any claim to see its details.")


def prepare(data_dir, items, as_of) -> None:
    """Build corrected claims, re-check them as of today, save them and log the resubmissions."""
    corrections = [status.correction(d.item, as_of) for d in items]
    with st.spinner("Re-checking the corrected claims…"):
        checked = status.check_corrections(data_dir, corrections)
    entries = []
    for d, claim in zip(items, corrections):
        a = checked.get(claim["claim_id"], {})
        code = a.get("predicted_denial_code") or ""
        entries.append({"claim": claim, "predicted": "denied" if code else "paid", "predicted_code": code,
                        "deadline": status.deadline_status(d.item, as_of)[0]})
    status.save_resubmissions(entries)
    activity.append([{
        "workspace": "status", "agency": d.row["agency_id"], "claim_id": d.claim_id, "payer": d.row["payer"],
        "event": "resubmit", "codes": [f.code for f in d.item.findings], "action": "Prepared corrected claim "
        + e["claim"]["claim_id"], "follow_up": "", "result": e["predicted"], "remaining_codes": [e["predicted_code"]] if e["predicted_code"] else [],
        "billed_amount": d.item.billed_amount} for d, e in zip(items, entries)])
    late = sum(e["predicted_code"] == "D03" for e in entries)
    ok = sum(e["predicted"] == "paid" for e in entries)
    st.session_state["status_message"] = (
        f"Prepared {len(entries)} corrected claim(s). Re-checked as of {as_of.isoformat()}: {ok} would be paid"
        + (f", {late} are past the filing deadline and would be denied as late" if late else "")
        + (f", {len(entries) - ok - late} still fail another check" if len(entries) - ok - late else "")
        + ". See the Resubmissions tab.")
    st.session_state.version += 1
    st.rerun()


def panel(data_dir, d, as_of, done, visits, auths) -> None:
    item, r = d.item, d.row
    key = f"status_{d.claim_id}_{st.session_state.version}"
    with st.container(border=True):
        st.markdown(f"### {d.claim_id} · {item.payer_name}")
        service = SERVICE_NAMES.get(r["service_code"].strip().upper(), r["service_code"]).capitalize()
        st.markdown(
            f"{service} for **{r['patient_id']}** on {long_date(parse_date(r['date_of_service']).isoformat())}  \n"
            f"{r['units']} units · ${item.billed_amount:,.2f} · submitted {parse_date(r['submitted_date']).isoformat()}  \n"
            f"**Payer's decision:** denied {d.payer_code} · “{md(d.remark)}”"
        )
        if d.claim_id in done:
            st.badge(done[d.claim_id].capitalize(), color="green")
        if d.group == "quick":
            for f in item.findings:
                st.markdown(f"**{f.label}:** {md(f.message)}", help=f.help or None)
                st.markdown(f"**{md(f.fix_text)}**")
            text, passed = status.deadline_status(item, as_of)
            (st.warning if passed else st.markdown)(
                f"{text}." + (" A corrected claim now would most likely be denied as late." if passed else ""))
            if st.button("Prepare corrected claim", key=f"prep_{key}", type="primary"):
                prepare(data_dir, [d], as_of)
        elif d.group == "person":
            for f in item.findings:
                st.markdown(f"**{f.label}** · {'one-click fix' if f.auto else 'needs a person'}: {md(f.message)}",
                            help=f.help or None)
                if f.next_step and not f.auto:
                    st.markdown(f"Next step: {md(f.next_step)}")
                if f.contact:
                    contact(item, f, key)
        elif d.group == "duplicate":
            ev = item.assessment["finding_details"].get("D05", {}).get("evidence", {})
            for m in ev.get("matching_submissions", []):
                st.markdown(f"Earlier claim **{m['claim_id']}**, submitted {m['submission_date']}: "
                            f"**{m['recorded_status'] or 'pending'}**.")
            st.markdown("If the earlier claim was paid, there's nothing to resubmit. If this was meant to correct "
                        "it, it should have been a corrected claim (frequency code 7) naming the original.")
        else:
            predicted = item.assessment["predicted_denial_code"]
            st.markdown("The checker found nothing that explains this denial." if not predicted else
                        f"The checker expected {predicted} ({ISSUE_LABELS.get(predicted, '')}), not {d.payer_code}.")
            with st.popover(f"Contact {item.payer_name}"):
                message = (f"To: {item.payer_name}\nRe: claim {d.claim_id}, patient {r['patient_id']}, "
                           f"{service} on {parse_date(r['date_of_service']).isoformat()}\n\n"
                           f"This claim was denied with {d.payer_code} (\"{d.remark}\"). Please tell us the reason "
                           f"so we can correct it.")
                st.code(message, language=None)
                if st.button("Mark as sent", key=f"ask_{key}"):
                    log(d, "contact", f"Asked {item.payer_name} about the denial")
                    st.rerun()
        if d.claim_id not in done and d.group != "quick" and st.button("Mark as resolved", key=f"resolve_{key}"):
            log(d, "resolved", "Marked as resolved")
            st.rerun()
    with st.expander("EVV visits for this patient, service and date"):
        day = (r["patient_id"], r["service_code"].strip().upper(), parse_date(r["date_of_service"]).isoformat())
        st.dataframe(pd.DataFrame(visits.get(day, [])), hide_index=True, width="stretch") if visits.get(day) \
            else st.markdown("None found.")
    with st.expander("Authorization on the claim"):
        st.dataframe(pd.DataFrame([auths[r["auth_id"]]]), hide_index=True, width="stretch") if r["auth_id"] in auths \
            else st.markdown("None, or the ID isn't on file.")


def contact(item, f, key) -> None:
    who = item.payer_name if f.contact == "payer" else "EVV coordinator"
    with st.popover(f"{'Contact' if f.contact == 'payer' else 'Message'} {who}"):
        st.code(contact_message(item, f), language=None)
        if st.button("Mark as sent", key=f"sent_{key}_{f.code}"):
            activity.append([{"workspace": "status", "agency": item.row["agency_id"], "claim_id": item.claim_id,
                              "payer": item.row["payer"], "event": "contact", "codes": [f.code],
                              "action": f"Contacted {who} about {f.label.lower()}", "follow_up": "", "result": "",
                              "remaining_codes": [], "billed_amount": item.billed_amount}])
            st.rerun()


def log(d, event: str, action: str) -> None:
    activity.append([{"workspace": "status", "agency": d.row["agency_id"], "claim_id": d.claim_id,
                      "payer": d.row["payer"], "event": event, "codes": [d.payer_code], "action": action,
                      "follow_up": "", "result": "", "remaining_codes": [], "billed_amount": d.item.billed_amount}])


def resubmissions_tab(entries: list[dict], agency: str) -> None:
    if not entries:
        st.markdown("No corrected claims prepared yet. Use **Quick fix & resubmit** to prepare them.")
        return
    table = pd.DataFrame([{**e["claim"], "predicted": e["predicted"], "predicted_code": e["predicted_code"],
                           "deadline": e["deadline"]} for e in entries])
    m = st.columns(3)
    m[0].metric("Corrected claims", len(table))
    m[1].metric("Would be paid", int((table["predicted"] == "paid").sum()))
    m[2].metric("Past the filing deadline", int((table["predicted_code"] == "D03").sum()))
    ready = table[table["predicted"] == "paid"]
    st.download_button(f"Download {len(ready)} corrected claims that pass", ready[status.CLAIM_COLUMNS].to_csv(index=False),
                       f"corrected_claims_{agency}.csv", "text/csv", type="primary")
    st.dataframe(table, hide_index=True, width="stretch")
