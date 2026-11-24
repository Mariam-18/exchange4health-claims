"""Exchange4Health claim review prototype.

Run from the repository root:
    streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
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
    build_queue,
    contact_message,
    load_pending,
    long_date,
    merge_changes,
    sort_key,
)
from checker.claim_checker import parse_date, read_rows  # noqa: E402

DEFAULT_DATA = ROOT / "Resonance_assessment_day__confirmation_and_preparation" / "Resonance - graduate preparation data"

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


# ---------- data ----------

@st.cache_data(show_spinner="Checking claims…")
def run_checker(data_dir: str, rows: tuple) -> list[dict]:
    """Run the checker on the current pending rows. Cached on the row contents."""
    return assess(Path(data_dir), [dict(r) for r in rows])


@st.cache_data
def load_reference(data_dir: str):
    """Return EVV visits grouped by patient/service/date, and authorizations by ID."""
    visits = defaultdict(list)
    for row in read_rows(Path(data_dir) / "evv_visits.csv"):
        visits[row["patient_id"], row["service_code"].strip().upper(), row["clock_in"][:10]].append(row)
    auths = {row["auth_id"]: row for row in read_rows(Path(data_dir) / "authorizations.csv")}
    return dict(visits), auths


def current_queue(data_dir: str) -> list[ReviewItem]:
    rows = st.session_state.rows
    assessments = run_checker(data_dir, tuple(tuple(r.items()) for r in rows))
    return build_queue(assessments, {r["claim_id"]: r for r in rows})


# ---------- actions ----------

def log(item: ReviewItem, action: str, note: str = "") -> None:
    st.session_state.log.append({
        "claim": item.claim_id, "agency": item.row["agency_id"], "action": action, "notify agency": note,
    })


def edit_claims(data_dir: str, changes: dict[str, dict], removals: set[str] = frozenset()) -> None:
    """Apply claim edits and removals, re-run the checker and record what changed."""
    before = {i.claim_id: i.needs_review for i in current_queue(data_dir)}
    st.session_state.rows = [
        apply_changes(r, changes[r["claim_id"]]) if r["claim_id"] in changes else r
        for r in st.session_state.rows if r["claim_id"] not in removals
    ]
    after = {i.claim_id: i for i in current_queue(data_dir)}
    st.session_state.last_result = {
        "cleared": [c for c in changes if not after[c].needs_review],
        "still": [c for c in changes if after[c].needs_review],
        "knock_on": [
            c for c, was_review in before.items()
            if c not in changes and c not in removals and not was_review and after[c].needs_review
        ],
        "removed": sorted(removals),
    }
    st.session_state.version += 1


def apply_auto(data_dir: str, items: list[ReviewItem], code: str | None = None) -> None:
    """Apply one-click fixes on the given claims: every quick fix, or only the given issue's fix."""
    changes = {}
    for item in items:
        fixes = [f for f in item.findings if f.auto and (code is None or f.code == code)]
        log(item, " ".join(f.fix_text for f in fixes), " ".join(f.next_step for f in fixes if f.next_step))
        changes[item.claim_id] = merge_changes(fixes)
    edit_claims(data_dir, changes)


# ---------- display helpers ----------

def days_label(days: int | None) -> str:
    if days is None:
        return "No limit"
    return f"{-days} days late" if days < 0 else f"{days} days"


def service_text(row: dict) -> str:
    return SERVICE_NAMES.get(row["service_code"].strip().upper(), row["service_code"]).capitalize()


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


def ready_columns(i: ReviewItem) -> dict:
    return {**base_columns(i), "Units": i.row["units"],
            "Changes made": changes_made(st.session_state.original[i.claim_id], i.row)}


def changes_made(original: dict, current: dict) -> str:
    """Describe edited fields, e.g. 'units 20 → 16; billed_amount 122.0 → 97.60'."""
    return "; ".join(
        f"{key} {original[key] or 'blank'} → {current[key] or 'blank'}"
        for key in original if key != "billed_amount" and original[key] != current[key]
    )


def result_banner() -> None:
    result = st.session_state.last_result
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


def claim_panel(item: ReviewItem, data_dir: str, *, leading_first: bool = True) -> None:
    """Detail pane: claim summary, each finding with its fix or action, and the evidence."""
    v = st.session_state.version
    r = item.row
    with st.container(border=True):
        st.markdown(f"### {item.claim_id} · {item.payer_name}")
        st.markdown(
            f"{service_text(r)} for **{r['patient_id']}** on {long_date(parse_date(r['date_of_service']).isoformat())}  \n"
            f"{r['units']} units · ${item.billed_amount:,.2f} · agency {r['agency_id']} · "
            f"filing deadline: {days_label(item.days_to_deadline).lower()}"
        )
        if not item.findings:
            st.badge("Ready to submit", color="green")
        ordered = item.findings
        if leading_first and item.leading:
            ordered = [item.leading] + [f for f in item.findings if f is not item.leading]
        for n, f in enumerate(ordered):
            st.divider()
            head = st.columns([5, 2])
            head[0].markdown(f"**{'Also: ' if n else ''}{f.label}**", help=f.help or None)
            with head[1]:
                st.badge("One-click fix" if f.auto else "Needs a person", color="green" if f.auto else "red")
            st.markdown(f.message)
            if f.auto:
                cols = st.columns([4, 1])
                cols[0].markdown(f"**{f.fix_text}**")
                if cols[1].button("Apply fix", key=f"fix_{item.claim_id}_{f.code}_{v}", type="primary"):
                    log(item, f.fix_text, f.next_step)
                    edit_claims(data_dir, {item.claim_id: f.change})
                    st.rerun()
                if f.next_step:
                    st.markdown(f.next_step)
            elif f.next_step and f.code != "D03":
                st.markdown(f"Next step: {f.next_step}")
            if f.choices:
                cols = st.columns(len(f.choices))
                for c, choice in enumerate(f.choices):
                    if cols[c].button(choice.label, key=f"choice_{item.claim_id}_{f.code}_{c}_{v}",
                                      type="secondary" if choice.remove else "primary"):
                        log(item, choice.label)
                        if choice.remove:
                            edit_claims(data_dir, {}, {item.claim_id})
                        else:
                            edit_claims(data_dir, {item.claim_id: choice.change})
                        st.rerun()
            if f.contact:
                who = item.payer_name if f.contact == "payer" else f"agency {r['agency_id']}"
                with st.popover(f"Contact {who}"):
                    st.code(contact_message(item, f), language=None)
                    if st.button("Mark as sent", key=f"sent_{item.claim_id}_{f.code}_{v}"):
                        log(item, f"Contacted {who} about {f.label.lower()}")
                        st.rerun()

    visits, auths = load_reference(data_dir)
    key = (r["patient_id"], r["service_code"].strip().upper(), parse_date(r["date_of_service"]).isoformat())
    with st.expander("EVV visits for this patient, service and date"):
        if visits.get(key):
            st.dataframe(pd.DataFrame(visits[key]), hide_index=True, width="stretch")
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


def pick_item(items: list[ReviewItem], clicked: ReviewItem | None, slot: str) -> ReviewItem | None:
    """Return the claim to show: the one just clicked, else the last one shown, else the first."""
    if clicked:
        st.session_state[slot] = clicked.claim_id
        return clicked
    by_id = {i.claim_id: i for i in items}
    return by_id.get(st.session_state.get(slot)) or (items[0] if items else None)


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


def show_panel(items, clicked, slot, data_dir, empty_text):
    item = pick_item(items, clicked, slot)
    if item:
        st.markdown(f"Showing **{item.claim_id}**. Click any row to open another claim.")
        claim_panel(item, data_dir)
    else:
        st.markdown(empty_text)


# ---------- page ----------

data_dir = os.environ.get("E4H_DATA_DIR", str(DEFAULT_DATA))
if not (Path(data_dir) / "claims_to_check.csv").exists():
    st.error(f"Can't find claims_to_check.csv in {data_dir}. Set E4H_DATA_DIR to the data pack folder.")
    st.stop()


def reset_state() -> None:
    st.session_state.rows = load_pending(Path(data_dir))
    st.session_state.original = {r["claim_id"]: r for r in st.session_state.rows}
    st.session_state.log = []
    st.session_state.last_result = None
    st.session_state.version = 0


if "rows" not in st.session_state:
    reset_state()

# Mock sign-in: the agency logs in and sees only its own claims.
if not st.session_state.get("agency"):
    st.title("Exchange4Health · Claim review")
    with st.form("sign_in", border=True):
        st.markdown("Sign in to your agency's billing workspace.")
        agencies = sorted({r["agency_id"] for r in st.session_state.rows})
        agency_choice = st.selectbox("Agency", agencies)
        password = st.text_input("Password", type="password", help="Prototype: any password works.")
        if st.form_submit_button("Sign in", type="primary"):
            if password:
                st.session_state.agency = agency_choice
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
        reset_state()
        st.rerun()

title_col, page_col = st.columns([3, 2])
title_col.title(f"Claim review · {agency}")
page = page_col.segmented_control("Page", ["Check claims", "Draft claims"], default="Check claims",
                                  label_visibility="collapsed") or "Check claims"

if page == "Draft claims":
    st.info("Coming next: generate claims from verified EVV visits for a date range.")
    st.stop()

queue = current_queue(data_dir)
mine = [i for i in queue if i.row["agency_id"] == agency]
ready = [i for i in mine if not i.needs_review]
review = [i for i in mine if i.needs_review]

leading_only = st.toggle(
    "Leading issue only", value=True,
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
result_banner()

my_log = [entry for entry in st.session_state.log if entry["agency"] == agency]
tabs = st.tabs([
    f"Needs a person ({len(manual)})", f"One-click fixes ({len(one_click)})",
    f"Ready to submit ({len(ready)})", f"Activity log ({len(my_log)})",
])

with tabs[0]:
    controls = st.columns([1, 1, 2])
    payer = controls[0].selectbox("Payer", ["All"] + sorted({i.payer_name for i in manual}))
    sort_by = controls[1].selectbox(
        "Sort by", ["deadline", "amount"],
        format_func=lambda s: "Days to filing deadline" if s == "deadline" else "Amount at stake",
    )
    issue = issue_picker(manual, "issue_manual", auto=False)
    items = [i for i in manual if payer in ("All", i.payer_name)
             and (issue is None or any(f.code == issue and not f.auto for f in i.findings))]
    if issue:
        issue_heading(items, issue)
    if issue == "D01":
        statuses = sorted({VISIT_STATUS_FILTERS[f.action] for i in items for f in i.findings if f.code == "D01"})
        status = st.pills("Visit status", statuses, key="d01_status")
        if status:
            items = [i for i in items if any(
                f.code == "D01" and VISIT_STATUS_FILTERS[f.action] == status for f in i.findings)]
    items.sort(key=lambda i: sort_key(i, sort_by))

    left, right = st.columns([3, 2])
    with left:
        clicked, _ = worklist(items, "manual_list", person_columns)
    with right:
        show_panel(items, clicked, "manual_selected", data_dir, "No claims match these filters.")

with tabs[1]:
    issue = issue_picker(one_click, "issue_auto", auto=True)
    group = [i for i in one_click if issue is None or any(f.code == issue and f.auto for f in i.findings)]
    group.sort(key=lambda i: sort_key(i, "deadline"))
    if issue:
        issue_heading(group, issue)

    left, right = st.columns([3, 2])
    with left:
        scope = f"the {ISSUE_LABELS[issue].lower()} fix" if issue else "every quick fix"
        st.markdown(f"Tick claims to apply {scope} to them, or apply it to the whole list.")
        clicked, ticked = worklist(group, "auto_list", quick_columns, ticks=True)
        bar = st.columns(2)
        if bar[0].button(f"Apply to {len(ticked)} ticked", disabled=not ticked, type="primary"):
            apply_auto(data_dir, ticked, issue)
            st.rerun()
        if bar[1].button(f"Apply to all {len(group)}", disabled=not group):
            apply_auto(data_dir, group, issue)
            st.rerun()
    with right:
        show_panel(group, clicked, "auto_selected", data_dir, "No one-click fixes left.")

with tabs[2]:
    ready.sort(key=lambda i: sort_key(i, "deadline"))
    submission = pd.DataFrame([
        {**i.row, "changes_made": changes_made(st.session_state.original[i.claim_id], i.row)} for i in ready
    ])
    edited = int((submission["changes_made"] != "").sum()) if len(submission) else 0
    st.download_button(
        f"Download {len(ready)} ready-to-submit claims ({edited} fixed)", submission.to_csv(index=False),
        f"ready_to_submit_{agency}.csv", "text/csv", type="primary",
    )
    left, right = st.columns([3, 2])
    with left:
        clicked, _ = worklist(ready, "ready_list", ready_columns)
    with right:
        show_panel(ready, clicked, "ready_selected", data_dir, "No claims are ready yet.")

with tabs[3]:
    if my_log:
        st.dataframe(pd.DataFrame(my_log).drop(columns="agency"), hide_index=True, width="stretch")
    else:
        st.markdown("No changes yet.")

with st.sidebar:
    st.divider()
    predictions = pd.DataFrame([{
        "claim_id": i.claim_id,
        "status": "denied" if i.assessment["failed_checks"] else "paid",
        "denial_code": i.assessment["predicted_denial_code"] or "",
    } for i in queue])
    st.download_button("predictions.csv (all agencies)", predictions.to_csv(index=False),
                       "predictions.csv", "text/csv")
