"""Exchange4Health claim review prototype.

Run from the repository root:
    streamlit run app/streamlit_app.py

Two pages share one review workspace design:
- Check claims: the agency's pending claims (claims_to_check.csv), checked against claims history.
- Draft claims: claims built from verified EVV visits, checked on their own as if they replaced
  hand-written claims.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import activity  # noqa: E402
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
from app import rules_admin, ui_alerts, ui_reports, ui_status  # noqa: E402
from app.ui_common import days_label, filter_claims, worklist  # noqa: E402
from app.analysis import build_frame  # noqa: E402
from checker.claim_checker import parse_date, read_rows, replay_all, rules_in_force, set_rules  # noqa: E402
from drafter.claim_drafter import DUPLICATE_DRAFT_PREFIX  # noqa: E402

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
def run_checker(rows: tuple, with_history: bool, rules_json: str) -> list[dict]:
    """Run the checker on the workspace rows under the given rules. Cached on rows and rules."""
    with rules_in_force(json.loads(rules_json)):
        return assess(Path(data_dir), [dict(r) for r in rows], with_history=with_history)


def draft_rows(agency: str, last_date: date, rules_json: str):
    with rules_in_force(json.loads(rules_json)):
        return build_drafts(Path(data_dir), agency, last_date)


@st.cache_data(show_spinner="Analysing past claims…")
def history_analysis(rules_json: str):
    """Past claims with derived fields and the checker's predictions and evidence under the given rules."""
    with rules_in_force(json.loads(rules_json)):
        history, _, _ = replay_all(Path(data_dir), include_details="all")
    frame = build_frame(Path(data_dir), history)
    return frame, {r["claim_id"]: r["assessment"] for r in history}


@st.cache_data(show_spinner="Drafting claims for statistics…")
def all_drafts(rules_json: str) -> list[dict]:
    """Every claim the drafter would build, for all agencies, with whether it needs review."""
    rows = []
    with rules_in_force(json.loads(rules_json)):
        for ag in sorted({r["agency_id"] for r in read_rows(Path(data_dir) / "claims_history.csv")}):
            drafted, _ = build_drafts(Path(data_dir), ag, LAST_SERVICE_DATE, block_duplicates=False)
            if drafted:
                checked = {a["claim_id"]: a for a in assess(Path(data_dir), drafted, with_history=False)}
                rows += [{"agency": ag, "service_date": d["date_of_service"], "billed": float(d["billed_amount"]),
                          "needs_review": checked[d["claim_id"]]["status"] == "needs_review"} for d in drafted]
    return rows


def rules_key() -> str:
    return json.dumps(rules_admin.current_rules(), sort_keys=True)


@st.cache_data
def load_reference():
    """Return EVV visits grouped by patient/service/date, and authorizations by ID."""
    visits = defaultdict(list)
    for row in read_rows(Path(data_dir) / "evv_visits.csv"):
        visits[row["patient_id"], row["service_code"].strip().upper(), row["clock_in"][:10]].append(row)
    auths = {row["auth_id"]: row for row in read_rows(Path(data_dir) / "authorizations.csv")}
    return dict(visits), auths


def queue_for(ws: dict) -> list[ReviewItem]:
    assessments = run_checker(tuple(tuple(r.items()) for r in ws["rows"]), ws["with_history"], rules_key())
    return build_queue(assessments, {r["claim_id"]: r for r in ws["rows"]})


def log(ws: dict, name: str, item: ReviewItem, event: str, codes: list[str], action: str,
        follow_up: str = "", after: ReviewItem | None = None, removed: bool = False) -> None:
    """Record a reviewer action in the workspace log and the saved activity log.

    event: "fix", "choice" or "contact". after: the claim as re-checked, if it was edited.
    """
    result = "removed" if removed else (
        "" if after is None else ("ready" if not after.needs_review else "needs review"))
    ws["log"].append({"claim": item.claim_id, "action": action, "follow-up": follow_up, "result": result})
    activity.append([{
        "workspace": name, "agency": item.row["agency_id"], "claim_id": item.claim_id,
        "payer": item.assessment["payer"], "event": event, "codes": codes, "action": action,
        "follow_up": follow_up, "result": result,
        "remaining_codes": [] if after is None else [f.code for f in after.findings],
        "billed_amount": item.billed_amount,
    }])


def edit_claims(ws: dict, name: str, changes: dict[str, dict], removals: set[str] = frozenset(),
                actions: list[tuple] = ()) -> None:
    """Apply claim edits and removals, re-run the checker, log the actions and record what changed.

    actions: (claim item, event, codes, action text, follow-up) for each claim acted on.
    """
    before = {i.claim_id: i.needs_review for i in queue_for(ws)}
    ws["rows"] = [
        apply_changes(r, changes[r["claim_id"]]) if r["claim_id"] in changes else r
        for r in ws["rows"] if r["claim_id"] not in removals
    ]
    after = {i.claim_id: i for i in queue_for(ws)}
    for item, event, codes, action, follow_up in actions:
        log(ws, name, item, event, codes, action, follow_up,
            after=after.get(item.claim_id), removed=item.claim_id in removals)
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


def apply_auto(ws: dict, name: str, items: list[ReviewItem], code: str | None = None) -> None:
    """Apply one-click fixes on the given claims: every quick fix, or only the given issue's fix."""
    changes, actions = {}, []
    for item in items:
        fixes = [f for f in item.findings if f.auto and (code is None or f.code == code)]
        changes[item.claim_id] = merge_changes(fixes)
        actions.append((item, "fix", [f.code for f in fixes], " ".join(f.fix_text for f in fixes),
                        " ".join(f.next_step for f in fixes if f.next_step)))
    edit_claims(ws, name, changes, actions=actions)


# ---------- display helpers ----------

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
                    edit_claims(ws, name, {item.claim_id: f.change},
                                actions=[(item, "fix", [f.code], f.fix_text, f.next_step)])
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
                        action = [(item, "choice", [f.code], choice.label, "")]
                        if choice.remove:
                            edit_claims(ws, name, {}, {item.claim_id}, actions=action)
                        else:
                            edit_claims(ws, name, {item.claim_id: choice.change}, actions=action)
                        st.rerun()
            if f.contact:
                who = item.payer_name if f.contact == "payer" else "EVV coordinator"
                with st.popover(f"{'Contact' if f.contact == 'payer' else 'Message'} {who}"):
                    st.code(contact_message(item, f), language=None)
                    if st.button("Mark as sent", key=f"sent_{key}"):
                        log(ws, name, item, "contact", [f.code], f"Contacted {who} about {f.label.lower()}")
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
    """Below the list: the clicked claim (or the last one opened), else a prompt to click one."""
    slot = f"{name}_{slot}"
    if clicked:
        st.session_state[slot] = clicked.claim_id
    if not items:
        st.markdown(empty_text)
        return
    item = clicked or {i.claim_id: i for i in items}.get(st.session_state.get(slot))
    if item:
        claim_panel(ws, name, item)
    else:
        st.markdown("Click on any claim to see its details.")


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
    activity.record_flagged(name, {i.claim_id: {
        "agency": i.row["agency_id"], "codes": [f.code for f in i.findings],
        "auto": [f.code for f in i.findings if f.auto]} for i in review})
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
        shown = filter_claims(shown, f"{name}_manual", load_reference()[0])
        shown.sort(key=lambda i: sort_key(i, sort_by))
        clicked, _ = worklist(shown, f"{name}_manual_list", person_columns)
        show_panel(ws, name, shown, clicked, "manual", "No claims need a person.")

    with tabs[1]:
        issue = issue_picker(one_click, f"{name}_issue_auto", auto=True)
        group = [i for i in one_click if issue is None or any(f.code == issue and f.auto for f in i.findings)]
        if issue:
            issue_heading(group, issue)
        group = filter_claims(group, f"{name}_auto", load_reference()[0])
        group.sort(key=lambda i: sort_key(i, "deadline"))
        scope = f"the {ISSUE_LABELS[issue].lower()} fix" if issue else "every quick fix"
        st.markdown(f"Tick claims to apply {scope} to them, or apply it to the whole list.")
        clicked, ticked = worklist(group, f"{name}_auto_list", quick_columns, ticks=True)
        bar = st.columns([1, 1, 4])
        if bar[0].button(f"Apply to {len(ticked)} ticked", disabled=not ticked, type="primary",
                         key=f"{name}_apply_ticked"):
            apply_auto(ws, name, ticked, issue)
            st.rerun()
        if bar[1].button(f"Apply to all {len(group)}", disabled=not group, key=f"{name}_apply_all"):
            apply_auto(ws, name, group, issue)
            st.rerun()
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

        ready = filter_claims(ready, f"{name}_ready", load_reference()[0])
        clicked, _ = worklist(ready, f"{name}_ready_list", ready_columns)
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

set_rules(rules_admin.current_rules())
RULES_TEAM = "Exchange4Health rules team"

# Mock sign-in: an agency billing team sees only its own claims; the rules team sees alerts and all agencies.
if not st.session_state.get("user"):
    st.title("Exchange4Health · Claim review")
    with st.form("sign_in", border=True):
        st.markdown("Sign in to your workspace.")
        agencies = sorted({r["agency_id"] for r in workspaces["check"]["rows"]})
        who = st.selectbox("Account", agencies + [RULES_TEAM],
                           format_func=lambda a: a if a == RULES_TEAM else f"Agency {a}")
        password = st.text_input("Password", type="password", help="Prototype: any password works.")
        if st.form_submit_button("Sign in", type="primary"):
            if password:
                st.session_state.user = who
                workspaces.pop("draft", None)
                st.rerun()
            st.error("Enter a password.")
    st.stop()

user = st.session_state.user
is_rules_team = user == RULES_TEAM
agency = None if is_rules_team else user
with st.sidebar:
    st.markdown(f"Signed in as **{user}**")
    if st.button("Sign out"):
        st.session_state.user = None
        st.rerun()
    if st.button("Reset all changes", help="Restores the original claims and clears saved activity."):
        st.session_state.workspaces = {}
        activity.clear()
        st.rerun()

HOME = "Home"
if st.session_state.get("page_for") != user:
    st.session_state.page_for, st.session_state.page = user, HOME
page = st.session_state.page


def go(target: str) -> None:
    st.session_state.page = target
    st.rerun()


def tile(column, title: str, figure: str, text: str, target: str) -> None:
    with column.container(border=True, height=230):
        st.markdown(f"### {title}")
        st.markdown(f"**{figure}**")
        st.markdown(text)
        if st.button("Open", key=f"tile_{target}", type="primary", use_container_width=True):
            go(target)


if page == HOME:
    st.title("Exchange4Health · Rules team" if is_rules_team else f"Exchange4Health · {agency}")
    if is_rules_team:
        new_bulletins = sum(b["status"] == "new" for b in rules_admin.list_bulletins(Path(data_dir)))
        cols = st.columns(2)
        tile(cols[0], "News & approvals", f"{new_bulletins} new bulletin(s)",
             "Payer bulletins and denials the rules didn't predict, read and investigated by Claude for your approval.",
             "News")
        tile(cols[1], "Statistics", "All agencies", "Standard statistics by period and agency, with Claude's review.",
             "Statistics")
    else:
        frame, _ = history_analysis(rules_key())
        mine = frame[frame["agency_id"] == agency]
        paid_pct = 100 * (mine["status"] == "paid").mean() if len(mine) else 0
        queue = [i for i in queue_for(workspaces["check"]) if i.row["agency_id"] == agency and i.needs_review]
        patients = set(mine["patient_id"])
        visit_count = sum(len([v for v in vs if v["verification_status"] == "verified"])
                          for (pt, _, _), vs in load_reference()[0].items() if pt in patients)
        notices = sum(1 for c in rules_admin.read_changes() if c["decision"] == "approved")
        top = st.columns(3)
        tile(top[0], "Draft claims", f"{visit_count:,} verified visits",
             "Build claims straight from verified EVV visits, with payer rules applied.", "Draft claims")
        tile(top[1], "Review unsubmitted claims",
             f"{sum(not i.auto_only for i in queue)} need a person · {sum(i.auto_only for i in queue)} one-click",
             "Check claims before they go to the payer and fix what has one right answer.", "Review unsubmitted claims")
        tile(top[2], "Check claim status", f"{paid_pct:.0f}% paid · {100 - paid_pct:.0f}% denied",
             "See payer decisions, fix and resubmit denials, and chase the ones nobody can explain.", "Check claim status")
        bottom = st.columns(3)
        tile(bottom[0], "Statistics", "Reports by period",
             "Where claims get stuck and why, with Claude's review and a place to ask questions.", "Statistics")
        tile(bottom[1], "News", f"{notices} rule change(s)", "Payer rule changes approved by Exchange4Health "
             "and how they affect your claims.", "News")
    st.stop()

head = st.columns([1, 6])
if head[0].button("← Home"):
    go(HOME)
head[1].title(page)

if page == "Review unsubmitted claims":
    ws = workspaces["check"]
    mine = [i for i in queue_for(ws) if i.row["agency_id"] == agency]
    review_workspace(ws, "check", mine, "ready-to-submit claims", f"ready_to_submit_{agency}.csv")

elif page == "Draft claims":
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
        with st.spinner("Drafting claims from EVV visits…"):
            rows, failed = draft_rows(agency, end, rules_key())
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
    duplicate_count = sum(reason.startswith(DUPLICATE_DRAFT_PREFIX) for _, _, _, reason in failed)
    if duplicate_count:
        label = "claim was" if duplicate_count == 1 else "claims were"
        st.caption(f"{duplicate_count} {label} not created because a matching claim was already submitted.")
    if failed:
        with st.expander(f"Skipped visit groups ({len(failed)})"):
            st.dataframe(pd.DataFrame(failed, columns=["Patient", "Service", "Date of service", "Reason"]),
                         hide_index=True, width="stretch")
    review_workspace(ws, "draft", in_range, "drafted claims ready to submit", f"drafted_claims_{agency}.csv")

elif page == "Check claim status":
    frame, assessments = history_analysis(rules_key())
    visits, auths = load_reference()
    if st.session_state.get("status_message"):
        st.success(st.session_state.pop("status_message"))
    ui_status.status_page(Path(data_dir), agency, frame, assessments, visits, auths)

elif page == "Statistics":
    frame, assessments = history_analysis(rules_key())
    patient_agency = dict(zip(frame["patient_id"], frame["agency_id"]))
    ui_reports.reports_page(Path(data_dir), frame, assessments, queue_for(workspaces["check"]), patient_agency,
                            agency, sorted(frame["agency_id"].unique()), all_drafts(rules_key()))

elif page in ("News", "News & approvals"):
    if is_rules_team:
        frame, _ = history_analysis(rules_key())
        if ui_alerts.rules_team_alerts(Path(data_dir), rules_admin.current_rules(), frame):
            st.rerun()
    else:
        ui_alerts.agency_alerts(agency)

with st.sidebar:
    st.divider()
    predictions = pd.DataFrame([{
        "claim_id": i.claim_id,
        "status": "denied" if i.assessment["failed_checks"] else "paid",
        "denial_code": i.assessment["predicted_denial_code"] or "",
    } for i in queue_for(workspaces["check"])])
    st.download_button("predictions.csv (all agencies)", predictions.to_csv(index=False),
                       "predictions.csv", "text/csv")
