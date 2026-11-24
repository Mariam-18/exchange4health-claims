"""Exchange4Health claim review prototype.

Run from the repository root:
    streamlit run app/streamlit_app.py

Two pages share one review workspace design:
- Check claims: the agency's pending claims (claims_to_check.csv), checked against claims history.
- Draft claims: claims built from verified EVV visits, checked on their own as if they replaced
  hand-written claims.
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.review import (  # noqa: E402
    ISSUE_HELP,
    ISSUE_LABELS,
    SERVICE_NAMES,
    VISIT_STATUS_FILTERS,
    ReviewItem,
    apply_changes,
    assess,
    build_drafts,
    build_queue,
    contact_message,
    load_pending,
    long_date,
    merge_changes,
    sort_key,
)
from checker.claim_checker import parse_date, read_rows  # noqa: E402

DEFAULT_DATA = ROOT / "Resonance_assessment_day__confirmation_and_preparation" / "Resonance - graduate preparation data"
FIRST_SERVICE_DATE, LAST_SERVICE_DATE = date(2026, 6, 1), date(2026, 11, 30)

st.set_page_config(page_title="Exchange4Health · Claim review", layout="wide")
st.markdown("""
<style>
  #MainMenu, footer, [data-testid="stDeployButton"] {visibility: hidden;}
  .block-container {padding-top: 2rem; padding-bottom: 2rem; max-width: 1500px;}
  [data-testid="stMetric"] {background: #f4f6f9; border-radius: 8px; padding: 12px 16px;}
  [data-testid="stMetricValue"] {font-size: 1.7rem;}
  h1 {font-size: 1.6rem !important; margin-bottom: 0 !important;}
</style>
""", unsafe_allow_html=True)

data_dir = os.environ.get("E4H_DATA_DIR", str(DEFAULT_DATA))


# ---------- workspaces ----------
# A workspace holds a set of claim rows being reviewed: the rows as loaded, the rows as edited,
# the activity log and the result of the last change.

def new_workspace(rows: list[dict], *, with_history: bool, **extra) -> dict:
    return {
        "rows": rows, "original": {r["claim_id"]: dict(r) for r in rows}, "log": [],
        "last_result": None, "with_history": with_history, **extra,
    }


@st.cache_data(show_spinner="Checking claims…")
def run_checker(rows: tuple, with_history: bool) -> list[dict]:
    """Run the checker on the workspace rows. Cached on the row contents."""
    return assess(Path(data_dir), [dict(r) for r in rows], with_history=with_history)


@st.cache_data(show_spinner="Drafting claims from EVV visits…")
def draft_rows(agency: str, last_date: date):
    return build_drafts(Path(data_dir), agency, last_date)


@st.cache_data
def load_reference():
    """Return EVV visits grouped by patient/service/date, and authorizations by ID."""
    visits = defaultdict(list)
    for row in read_rows(Path(data_dir) / "evv_visits.csv"):
        visits[row["patient_id"], row["service_code"].strip().upper(), row["clock_in"][:10]].append(row)
    auths = {row["auth_id"]: row for row in read_rows(Path(data_dir) / "authorizations.csv")}
    return dict(visits), auths


def queue_for(ws: dict) -> list[ReviewItem]:
    assessments = run_checker(tuple(tuple(r.items()) for r in ws["rows"]), ws["with_history"])
    return build_queue(assessments, {r["claim_id"]: r for r in ws["rows"]})


def log(ws: dict, item: ReviewItem, action: str, follow_up: str = "") -> None:
    ws["log"].append({"claim": item.claim_id, "action": action, "follow-up": follow_up})


def edit_claims(ws: dict, changes: dict[str, dict], removals: set[str] = frozenset()) -> None:
    """Apply claim edits and removals, re-run the checker and record what changed."""
    before = {i.claim_id: i.needs_review for i in queue_for(ws)}
    ws["rows"] = [
        apply_changes(r, changes[r["claim_id"]]) if r["claim_id"] in changes else r
        for r in ws["rows"] if r["claim_id"] not in removals
    ]
    after = {i.claim_id: i for i in queue_for(ws)}
    ws["last_result"] = {
        "cleared": [c for c in changes if not after[c].needs_review],
        "still": [c for c in changes if after[c].needs_review],
        "knock_on": [
            c for c, was_review in before.items()
            if c not in changes and c not in removals and not was_review and after[c].needs_review
        ],
        "removed": sorted(removals),
    }
    st.session_state.version += 1


def apply_auto(ws: dict, items: list[ReviewItem], code: str | None = None) -> None:
    """Apply one-click fixes on the given claims: every quick fix, or only the given issue's fix."""
    changes = {}
    for item in items:
        fixes = [f for f in item.findings if f.auto and (code is None or f.code == code)]
        log(ws, item, " ".join(f.fix_text for f in fixes), " ".join(f.next_step for f in fixes if f.next_step))
        changes[item.claim_id] = merge_changes(fixes)
    edit_claims(ws, changes)


# ---------- display helpers ----------

def days_label(days: int | None) -> str:
    if days is None:
        return "No limit"
    return f"{-days} days late" if days < 0 else f"{days} days"


def service_text(row: dict) -> str:
    return SERVICE_NAMES.get(row["service_code"].strip().upper(), row["service_code"]).capitalize()


def changes_made(original: dict, current: dict) -> str:
    """Describe edited fields, e.g. 'units 20 → 16'."""
    return "; ".join(
        f"{key} {original[key] or 'blank'} → {current[key] or 'blank'}"
        for key in original if key != "billed_amount" and original[key] != current[key]
    )


def labels(findings) -> str:
    return ", ".join(f.label for f in findings)


def base_columns(i: ReviewItem) -> dict:
    return {
        "Days to deadline": days_label(i.days_to_deadline),
        "Amount": i.billed_amount,
        "Date of service": parse_date(i.row["date_of_service"]).isoformat(),
        "Payer": i.payer_name,
        "Claim": i.claim_id,
        "Patient": i.row["patient_id"],
    }


def person_columns(i: ReviewItem) -> dict:
    return {"Needs a person": labels(f for f in i.findings if not f.auto), **base_columns(i),
            "Other issues": labels(f for f in i.findings if f.auto)}


def quick_columns(i: ReviewItem) -> dict:
    fixes = [f for f in i.findings if f.auto]
    return {"Quick fixes": labels(fixes), "Suggested fix": " ".join(f.fix_text for f in fixes),
            **base_columns(i), "Other issues": labels(f for f in i.findings if not f.auto)}


def worklist(items: list[ReviewItem], key: str, columns, *, ticks: bool = False):
    """Show a claim list. Clicking any cell opens that claim; ticks select rows for bulk actions.

    Inputs: claims, a widget key, and a function mapping a claim to its row of display columns.
    Output: (clicked claim or None, ticked claims).
    """
    frame = pd.DataFrame([columns(i) for i in items])
    event = st.dataframe(
        frame, hide_index=True, width="stretch", height=min(600, 38 + 35 * max(len(items), 1)),
        on_select="rerun", selection_mode=["multi-row", "single-cell"] if ticks else "single-cell",
        key=f"{key}_{st.session_state.version}",
        column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
    )
    cells = event.selection.get("cells", [])
    clicked = items[cells[-1][0]] if cells else None
    ticked = [items[r] for r in event.selection.get("rows", [])]
    return clicked, ticked


def result_banner(ws: dict) -> None:
    result = ws["last_result"]
    if not result:
        return
    parts = []
    if result["removed"]:
        parts.append(f"Removed {', '.join(result['removed'])}.")
    if result["cleared"]:
        parts.append(f"{len(result['cleared'])} changed claim(s) are now ready to submit.")
    if result["still"]:
        parts.append(f"{len(result['still'])} still need review: {', '.join(result['still'])}.")
    if result["knock_on"]:
        parts.append(f"{len(result['knock_on'])} other claim(s) now fail because of this change: "
                     f"{', '.join(result['knock_on'])}.")
    (st.warning if result["still"] or result["knock_on"] else st.success)("Re-checked. " + " ".join(parts))


def claim_panel(ws: dict, name: str, item: ReviewItem) -> None:
    """Detail pane: claim summary, each finding with its fix or action, and the evidence."""
    v = st.session_state.version
    r = item.row
    with st.container(border=True):
        st.markdown(f"### {item.claim_id} · {item.payer_name}")
        drafted = f"  \nDrafted from EVV visit {r['visit_ids'].replace('|', ', ')}" if r.get("visit_ids") else ""
        st.markdown(
            f"{service_text(r)} for **{r['patient_id']}** on {long_date(parse_date(r['date_of_service']).isoformat())}  \n"
            f"{r['units']} units · ${item.billed_amount:,.2f} · authorization {r['auth_id'] or 'none'} · "
            f"filing deadline: {days_label(item.days_to_deadline).lower()}{drafted}"
        )
        if not item.findings:
            st.badge("Ready to submit", color="green")
        ordered = item.findings
        if item.leading:
            ordered = [item.leading] + [f for f in item.findings if f is not item.leading]
        for n, f in enumerate(ordered):
            st.divider()
            head = st.columns([5, 2])
            head[0].markdown(f"**{'Also: ' if n else ''}{f.label}**", help=f.help or None)
            with head[1]:
                st.badge("One-click fix" if f.auto else "Needs a person", color="green" if f.auto else "red")
            st.markdown(f.message)
            key = f"{name}_{item.claim_id}_{f.code}_{v}"
            if f.auto:
                cols = st.columns([4, 1])
                cols[0].markdown(f"**{f.fix_text}**")
                if cols[1].button("Apply fix", key=f"fix_{key}", type="primary"):
                    log(ws, item, f.fix_text, f.next_step)
                    edit_claims(ws, {item.claim_id: f.change})
                    st.rerun()
                if f.next_step:
                    st.markdown(f.next_step)
            elif f.next_step and f.code != "D03":
                st.markdown(f"Next step: {f.next_step}")
            if f.choices:
                cols = st.columns(len(f.choices))
                for c, choice in enumerate(f.choices):
                    if cols[c].button(choice.label, key=f"choice_{key}_{c}",
                                      type="secondary" if choice.remove else "primary"):
                        log(ws, item, choice.label)
                        if choice.remove:
                            edit_claims(ws, {}, {item.claim_id})
                        else:
                            edit_claims(ws, {item.claim_id: choice.change})
                        st.rerun()
            if f.contact:
                who = item.payer_name if f.contact == "payer" else "EVV coordinator"
                with st.popover(f"{'Contact' if f.contact == 'payer' else 'Message'} {who}"):
                    st.code(contact_message(item, f), language=None)
                    if st.button("Mark as sent", key=f"sent_{key}"):
                        log(ws, item, f"Contacted {who} about {f.label.lower()}")
                        st.rerun()

    visits, auths = load_reference()
    visit_key = (r["patient_id"], r["service_code"].strip().upper(), parse_date(r["date_of_service"]).isoformat())
    with st.expander("EVV visits for this patient, service and date"):
        if visits.get(visit_key):
            st.dataframe(pd.DataFrame(visits[visit_key]), hide_index=True, width="stretch")
        else:
            st.markdown("None found.")
    with st.expander("Authorization on the claim"):
        if r["auth_id"] in auths:
            st.dataframe(pd.DataFrame([auths[r["auth_id"]]]), hide_index=True, width="stretch")
        else:
            st.markdown("None, or the ID isn't on file.")
    with st.expander("Claim fields and checker evidence"):
        st.dataframe(pd.DataFrame([r]), hide_index=True, width="stretch")
        for f in item.findings:
            st.markdown(f"**{f.code}** · {f.rule_reference}")
            st.json(f.evidence, expanded=False)


def show_panel(ws, name, items, clicked, slot, empty_text):
    """Show the clicked claim, else the last one shown, else the first."""
    slot = f"{name}_{slot}"
    if clicked:
        st.session_state[slot] = clicked.claim_id
    by_id = {i.claim_id: i for i in items}
    item = clicked or by_id.get(st.session_state.get(slot)) or (items[0] if items else None)
    if item:
        st.markdown(f"Showing **{item.claim_id}**. Click any row to open another claim.")
        claim_panel(ws, name, item)
    else:
        st.markdown(empty_text)


def issue_picker(items: list[ReviewItem], key: str, auto: bool) -> str | None:
    """Issue chips with counts for one kind of finding (quick fix or needs a person). Return the code or None."""
    counts = Counter(f.code for i in items for f in i.findings if f.auto == auto)
    return st.pills(
        "Issue", [c for c, _ in counts.most_common()], key=key, label_visibility="collapsed",
        format_func=lambda c: f"{ISSUE_LABELS.get(c, c)} · {counts[c]}",
    )


def issue_heading(items: list[ReviewItem], code: str) -> None:
    st.markdown(f"**{ISSUE_LABELS[code]}** · {len(items)} claims · ${sum(i.billed_amount for i in items):,.2f}",
                help=ISSUE_HELP.get(code))


def review_workspace(ws: dict, name: str, items: list[ReviewItem], ready_label: str, file_name: str) -> None:
    """Metrics, leading-issue switch and the four tabs, for the given claims of one workspace."""
    ready = [i for i in items if not i.needs_review]
    review = [i for i in items if i.needs_review]
    leading_only = st.toggle(
        "Leading issue only", value=True, key=f"{name}_leading",
        help="On: a claim with anything that needs a person is listed only there. "
             "Off: a claim with both kinds of issue is listed in both tabs.",
    )
    if leading_only:
        manual = [i for i in review if not i.auto_only]
        one_click = [i for i in review if i.auto_only]
    else:
        manual = [i for i in review if any(not f.auto for f in i.findings)]
        one_click = [i for i in review if any(f.auto for f in i.findings)]

    m = st.columns(4)
    m[0].metric("Ready to submit", f"{len(ready):,}")
    m[1].metric("Needs a person", f"{len(manual):,}")
    m[2].metric("One-click fixes", f"{len(one_click):,}")
    m[3].metric("Value at risk", f"${sum(i.billed_amount for i in review):,.2f}")
    result_banner(ws)

    tabs = st.tabs([
        f"Needs a person ({len(manual)})", f"One-click fixes ({len(one_click)})",
        f"Ready to submit ({len(ready)})", f"Activity log ({len(ws['log'])})",
    ])

    with tabs[0]:
        controls = st.columns([1, 1, 2])
        payer = controls[0].selectbox("Payer", ["All"] + sorted({i.payer_name for i in manual}), key=f"{name}_payer")
        sort_by = controls[1].selectbox(
            "Sort by", ["deadline", "amount"], key=f"{name}_sort",
            format_func=lambda s: "Days to filing deadline" if s == "deadline" else "Amount at stake",
        )
        issue = issue_picker(manual, f"{name}_issue_manual", auto=False)
        shown = [i for i in manual if payer in ("All", i.payer_name)
                 and (issue is None or any(f.code == issue and not f.auto for f in i.findings))]
        if issue:
            issue_heading(shown, issue)
        if issue == "D01":
            statuses = sorted({VISIT_STATUS_FILTERS[f.action] for i in shown for f in i.findings if f.code == "D01"})
            status = st.pills("Visit status", statuses, key=f"{name}_d01_status")
            if status:
                shown = [i for i in shown if any(
                    f.code == "D01" and VISIT_STATUS_FILTERS[f.action] == status for f in i.findings)]
        shown.sort(key=lambda i: sort_key(i, sort_by))
        left, right = st.columns([3, 2])
        with left:
            clicked, _ = worklist(shown, f"{name}_manual_list", person_columns)
        with right:
            show_panel(ws, name, shown, clicked, "manual", "No claims need a person.")

    with tabs[1]:
        issue = issue_picker(one_click, f"{name}_issue_auto", auto=True)
        group = [i for i in one_click if issue is None or any(f.code == issue and f.auto for f in i.findings)]
        group.sort(key=lambda i: sort_key(i, "deadline"))
        if issue:
            issue_heading(group, issue)
        left, right = st.columns([3, 2])
        with left:
            scope = f"the {ISSUE_LABELS[issue].lower()} fix" if issue else "every quick fix"
            st.markdown(f"Tick claims to apply {scope} to them, or apply it to the whole list.")
            clicked, ticked = worklist(group, f"{name}_auto_list", quick_columns, ticks=True)
            bar = st.columns(2)
            if bar[0].button(f"Apply to {len(ticked)} ticked", disabled=not ticked, type="primary",
                             key=f"{name}_apply_ticked"):
                apply_auto(ws, ticked, issue)
                st.rerun()
            if bar[1].button(f"Apply to all {len(group)}", disabled=not group, key=f"{name}_apply_all"):
                apply_auto(ws, group, issue)
                st.rerun()
        with right:
            show_panel(ws, name, group, clicked, "auto", "No one-click fixes left.")

    with tabs[2]:
        ready.sort(key=lambda i: sort_key(i, "deadline"))
        submission = pd.DataFrame([
            {**i.row, "changes_made": changes_made(ws["original"][i.claim_id], i.row)} for i in ready
        ])
        edited = int((submission["changes_made"] != "").sum()) if len(submission) else 0
        st.download_button(
            f"Download {len(ready)} {ready_label} ({edited} fixed)", submission.to_csv(index=False),
            file_name, "text/csv", type="primary", key=f"{name}_download",
        )

        def ready_columns(i: ReviewItem) -> dict:
            return {**base_columns(i), "Units": i.row["units"],
                    "Changes made": changes_made(ws["original"][i.claim_id], i.row)}

        left, right = st.columns([3, 2])
        with left:
            clicked, _ = worklist(ready, f"{name}_ready_list", ready_columns)
        with right:
            show_panel(ws, name, ready, clicked, "ready", "No claims are ready yet.")

    with tabs[3]:
        if ws["log"]:
            st.dataframe(pd.DataFrame(ws["log"]), hide_index=True, width="stretch")
        else:
            st.markdown("No changes yet.")


# ---------- page ----------

if not (Path(data_dir) / "claims_to_check.csv").exists():
    st.error(f"Can't find claims_to_check.csv in {data_dir}. Set E4H_DATA_DIR to the data pack folder.")
    st.stop()

if "workspaces" not in st.session_state:
    st.session_state.workspaces = {}
    st.session_state.version = 0
workspaces = st.session_state.workspaces
if "check" not in workspaces:
    workspaces["check"] = new_workspace(load_pending(Path(data_dir)), with_history=True)

# Mock sign-in: the agency logs in and sees only its own claims.
if not st.session_state.get("agency"):
    st.title("Exchange4Health · Claim review")
    with st.form("sign_in", border=True):
        st.markdown("Sign in to your agency's billing workspace.")
        agencies = sorted({r["agency_id"] for r in workspaces["check"]["rows"]})
        agency_choice = st.selectbox("Agency", agencies)
        password = st.text_input("Password", type="password", help="Prototype: any password works.")
        if st.form_submit_button("Sign in", type="primary"):
            if password:
                st.session_state.agency = agency_choice
                workspaces.pop("draft", None)
                st.rerun()
            st.error("Enter a password.")
    st.stop()

agency = st.session_state.agency
with st.sidebar:
    st.markdown(f"Signed in as **{agency}**")
    if st.button("Sign out"):
        st.session_state.agency = None
        st.rerun()
    if st.button("Reset all changes"):
        st.session_state.workspaces = {}
        st.rerun()

title_col, page_col = st.columns([3, 2])
title_col.title(f"Claim review · {agency}")
page = page_col.segmented_control("Page", ["Check claims", "Draft claims"], default="Check claims",
                                  label_visibility="collapsed") or "Check claims"

if page == "Check claims":
    ws = workspaces["check"]
    mine = [i for i in queue_for(ws) if i.row["agency_id"] == agency]
    review_workspace(ws, "check", mine, "ready-to-submit claims", f"ready_to_submit_{agency}.csv")

else:
    st.markdown(
        "Build claims straight from verified EVV visits: one claim per patient, service and date of "
        "service, with units, authorization and modifier set by the payer's rules. Each draft is then "
        "checked like any other claim."
    )
    with st.form("draft_range"):
        cols = st.columns([2, 1])
        picked = cols[0].date_input(
            "Dates of service to draft", value=(date(2026, 9, 1), LAST_SERVICE_DATE),
            min_value=FIRST_SERVICE_DATE, max_value=LAST_SERVICE_DATE,
        )
        cols[1].markdown("&nbsp;")
        generate = cols[1].form_submit_button("Generate claims", type="primary")
    if generate and len(picked) == 2:
        start, end = picked
        rows, failed = draft_rows(agency, end)
        workspaces["draft"] = new_workspace(rows, with_history=False, start=start, end=end, failed=failed)
        st.session_state.version += 1

    ws = workspaces.get("draft")
    if not ws:
        st.stop()
    in_range = [
        i for i in queue_for(ws)
        if ws["start"] <= parse_date(i.row["date_of_service"]) <= ws["end"]
    ]
    st.markdown(
        f"**{len(in_range)} claims drafted** for dates of service {long_date(ws['start'].isoformat())} to "
        f"{long_date(ws['end'].isoformat())}. Authorization balances include drafts for earlier dates."
    )
    failed = [f for f in ws["failed"] if ws["start"] <= f[2] <= ws["end"]]
    if failed:
        with st.expander(f"Couldn't draft {len(failed)} visit group(s)"):
            st.dataframe(pd.DataFrame(failed, columns=["Patient", "Service", "Date of service", "Reason"]),
                         hide_index=True, width="stretch")
    review_workspace(ws, "draft", in_range, "drafted claims ready to submit", f"drafted_claims_{agency}.csv")

with st.sidebar:
    st.divider()
    predictions = pd.DataFrame([{
        "claim_id": i.claim_id,
        "status": "denied" if i.assessment["failed_checks"] else "paid",
        "denial_code": i.assessment["predicted_denial_code"] or "",
    } for i in queue_for(workspaces["check"])])
    st.download_button("predictions.csv (all agencies)", predictions.to_csv(index=False),
                       "predictions.csv", "text/csv")
