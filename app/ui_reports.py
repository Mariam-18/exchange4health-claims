"""Reports page: standard statistics with charts, and Claude's review of them."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from app import ai
from app.activity import STATE_DIR, now
from app.stats import STAT_TITLES, compute, in_period
from app.ui_common import ask_box

PRIMARY = "#1f4e79"
ROOT = Path(__file__).resolve().parents[1]
PROJECT_README = ROOT / "README.md"
INVESTIGATION_LOG = STATE_DIR / "investigation_log.md"
FIRST, LAST = date(2026, 6, 1), date(2027, 2, 28)



def md(text: str) -> str:
    """Escape dollar signs so Streamlit doesn't render amounts as maths."""
    return str(text).replace("$", "\\$")


# ---------- charts ----------

def bar(rows: list[dict], category: str, value: str, title: str, fmt: str = ",.0f", keep_order: bool = False) -> None:
    """Horizontal single-series bar chart with tooltips, plus a table view."""
    if not rows:
        st.markdown("Nothing in this period.")
        return
    data = pd.DataFrame(rows)
    chart = alt.Chart(data).mark_bar(color=PRIMARY, cornerRadiusEnd=4, height={"band": 0.6}).encode(
        y=alt.Y(f"{category}:N", sort=None if keep_order else "-x", title=None, axis=alt.Axis(labelLimit=320)),
        x=alt.X(f"{value}:Q", title=title, axis=alt.Axis(format=fmt, grid=True, gridColor="#e8ebf0")),
        tooltip=[alt.Tooltip(f"{category}:N"), alt.Tooltip(f"{value}:Q", format=fmt)],
    ).properties(height=max(120, 34 * len(data)))
    st.altair_chart(chart.configure_view(strokeWidth=0), use_container_width=True)


def line(rows: list[dict], x: str, y: str, title: str) -> None:
    if not rows:
        st.markdown("Nothing in this period.")
        return
    data = pd.DataFrame(rows)
    base = alt.Chart(data).encode(
        x=alt.X(f"{x}:O", title=None),
        y=alt.Y(f"{y}:Q", title=title, axis=alt.Axis(gridColor="#e8ebf0")),
        tooltip=[alt.Tooltip(f"{x}:O"), alt.Tooltip(f"{y}:Q", format=".1f"), "claims:Q", "denied:Q"],
    )
    chart = base.mark_line(color=PRIMARY, strokeWidth=2) + base.mark_point(color=PRIMARY, filled=True, size=70)
    st.altair_chart(chart.configure_view(strokeWidth=0), use_container_width=True)


def table(rows, **kwargs) -> None:
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", **kwargs)
    else:
        st.markdown("Nothing in this period.")


# ---------- one section per statistic ----------

def section(key: str, s: dict) -> None:
    v = s[key]
    if key == "journey":
        st.markdown("Claims through each step, for this scope and period. Drafts and pending claims use their "
                    "date of service; submitted claims use the period you picked.")
        bar(v, "stage", "claims", "Claims", keep_order=True)
        with st.expander("Table"):
            table(v)
    elif key == "denials_by_code":
        cols = st.columns(2)
        with cols[0]:
            bar(v, "issue", "denials", "Denials")
        with cols[1]:
            bar(v, "issue", "billed", "Billed amount denied ($)", fmt="$,.0f")
    elif key == "denials_by_month":
        line(v, "month", "denial_rate_pct", "Denial rate (%)")
        with st.expander("Table"):
            table(v)
    elif key == "flagged_by_reason":
        st.markdown("Claims the checker flags before submission, by its expected reason, and what the payer did with them.")
        table(v)
    elif key == "passed_without_review":
        m = st.columns(3)
        m[0].metric("Passed the checker", f"{v['passed']:,}")
        m[1].metric("Paid", f"{v['paid']:,}", help=f"{v['paid_pct']}% of claims that passed")
        m[2].metric("Denied anyway", f"{v['denied']:,}")
        if v["denied_by_code"]:
            st.markdown("Denied anyway, by code: " + ", ".join(f"{k} {n}" for k, n in v["denied_by_code"].items()) + ".")
    elif key == "one_click_estimate":
        m = st.columns(3)
        m[0].metric("Denied claims", f"{v['denied']:,}")
        m[1].metric("Only one-click issues", f"{v['one_click_fixable']:,}", help=f"{v['share_pct']}% of denials")
        m[2].metric("Billed amount at stake", f"${v['billed_recoverable']:,.2f}")
        st.markdown(f"{v['note']}")
        bar(v["by_code"], "issue", "claims", "Denials a one-click fix would have caught")
    elif key == "outliers":
        st.markdown(f"Denial rate across this scope: {v['overall_denial_rate_pct']}%. Listed: at least "
                    f"{v['min_claims']} claims and a rate well above it (z above 2).")
        cols = st.columns(2)
        with cols[0]:
            st.markdown("**Caregivers**")
            table(v["caregivers"])
        with cols[1]:
            st.markdown("**Patients**")
            table(v["patients"])
    elif key == "payers":
        names = {"PY01": "Northwind", "PY02": "Harbor", "PY03": "Summit"}
        rows = [{**r, "payer_name": names.get(r["payer"], r["payer"])} for r in v]
        cols = st.columns(2)
        with cols[0]:
            bar(rows, "payer_name", "denial_rate_pct", "Denial rate (%)", fmt=".1f")
        with cols[1]:
            bar(rows, "payer_name", "unexpected_rate_pct", "Unexpected denial rate (%)", fmt=".1f")
    elif key == "queue_health":
        m = st.columns(5)
        m[0].metric("Waiting for review", v["waiting"])
        m[1].metric("Value", f"${v['value']:,.2f}")
        m[2].metric("Deadline within 7 days", v["deadline_within_7_days"])
        m[3].metric("Already late", v["already_late"])
        m[4].metric("Median hours waiting", v["median_hours_waiting"] if v["median_hours_waiting"] is not None else "–")
        st.markdown(v["note"])
    elif key == "fix_uptake":
        st.markdown("From app activity: claims offered a one-click fix, fixes applied, and what the recheck found.")
        table(v)
    elif key == "unexpected_reasons":
        table(v)
    elif key == "recurring_issues":
        st.markdown("Denial codes an agency kept getting in at least 3 separate months.")
        table(v)
    elif key == "rebilled_claims":
        m = st.columns(3)
        m[0].metric("Billed again", v["rebilled"])
        m[1].metric("First claim was already paid", v["first_was_paid"])
        m[2].metric("Median days later", v["median_days_after"] if v["median_days_after"] is not None else "–")
        st.markdown(v["note"])
        table(v["examples"])
    elif key == "time_to_resolve":
        st.markdown("From app activity: minutes from a claim first being flagged to the action that made it ready.")
        table(v)
    elif key == "visits_not_drafted":
        m = st.columns(4)
        m[0].metric("Visits", f"{v['visits']:,}")
        m[1].metric("Held back", v["held_back"], help=f"{v['held_pct']}% of visits")
        m[2].metric("Rejected", v["rejected"])
        m[3].metric("Pending manual verification", v["manual_edit_pending"])
        st.markdown(v["note"])


# ---------- page ----------

def reports_page(data_dir: Path, frame, assessments: dict, queue: list, patient_agency: dict,
                 agency: str | None, all_agencies: list[str], drafts: list[dict]) -> None:
    with st.form("report_scope"):
        cols = st.columns([1, 2, 1, 1])
        field = cols[0].radio("Period by", ["service_date", "submitted_date"],
                              format_func=lambda f: "Date of service" if f == "service_date" else "Submission date")
        picked = cols[1].date_input("Period", value=(date(2026, 9, 1), date(2026, 11, 30)),
                                    min_value=FIRST, max_value=LAST)
        if agency is None:
            scope = cols[2].selectbox("Agency", ["All agencies"] + all_agencies)
        else:
            scope = agency
            cols[2].markdown(f"Agency  \n**{agency}**")
        cols[3].markdown("&nbsp;")
        go = cols[3].form_submit_button("Generate report", type="primary")
    if go and len(picked) == 2:
        st.session_state.report_params = (field, picked[0], picked[1], None if scope == "All agencies" else scope)
    params = st.session_state.get("report_params")
    if not params or (agency is not None and params[3] != agency):
        st.markdown("Pick a period and generate the report.")
        return
    field, start, end, scoped_agency = params
    s = compute(frame, assessments, data_dir, field=field, start=start, end=end, agency=scoped_agency,
                queue=queue, patient_agency=patient_agency, drafts=drafts)

    sc = s["scope"]
    st.markdown(f"### {sc['agency']} · {'dates of service' if field == 'service_date' else 'submitted'} "
                f"{start.isoformat()} to {end.isoformat()}")
    m = st.columns(4)
    m[0].metric("Claims", f"{sc['claims']:,}")
    m[1].metric("Denied", f"{sc['denied']:,}")
    m[2].metric("Denial rate", f"{sc['denial_rate_pct']}%" if sc["denial_rate_pct"] is not None else "–")
    m[3].metric("Billed amount denied", f"${sc['denied_billed']:,.2f}")

    scope_text = f"{sc['agency']}, {'dates of service' if field == 'service_date' else 'submitted'} {start} to {end}"
    ask_box("reports", scope_text, data_dir, in_period(frame, field, start, end, scoped_agency))

    review = claude_review(data_dir, frame, s, field, start, end, scoped_agency)
    highlights = (review or {}).get("result", {}).get("highlights", [])
    shown = set()
    if highlights:
        st.markdown("## Highlighted by Claude")
        for h in highlights:
            with st.container(border=True):
                st.markdown(f"### {md(h['title'])}")
                st.markdown(md(h["summary"]))
                for key in dict.fromkeys(h["stat_keys"]):
                    if key in STAT_TITLES and key not in shown:
                        shown.add(key)
                        with st.expander(STAT_TITLES[key]):
                            section(key, s)
    st.markdown("## All statistics" if highlights else "## Statistics")
    for key in STAT_TITLES:
        if key not in shown:
            with st.expander(STAT_TITLES[key], expanded=not highlights and key == "journey"):
                section(key, s)


def claude_review(data_dir, frame, s, field, start, end, agency):
    """Claude's choice of highlights and its findings, on request."""
    scope = f"{agency or 'all agencies'}; {field} from {start.isoformat()} to {end.isoformat()}"
    key = f"report_review::{scope}"
    st.markdown("## Claude's review")
    cols = st.columns([1, 1, 3])
    if cols[0].button("Review with Claude", type="primary"):
        st.session_state[key] = "run"
    if st.session_state.get(key) and cols[1].button("Review again"):
        st.session_state[key] = "refresh"
    if not st.session_state.get(key):
        st.markdown("Claude picks the statistics worth attention, digs into what looks unusual with the analysis "
                    "tools, and traces case studies. Every number it states is checked against the data.")
        return None
    part = in_period(frame, field, start, end, agency)
    findings = PROJECT_README.read_text(encoding="utf-8").partition(
        "## Findings that shaped the workflow\n"
    )[2].partition("\n## ")[0].strip()
    saved = INVESTIGATION_LOG.read_text(encoding="utf-8") if INVESTIGATION_LOG.exists() else ""
    known = "\n\n".join(filter(None, (findings, saved)))
    try:
        with st.spinner("Claude is reviewing the statistics and exploring the claims…"):
            out = ai.review_report(scope, s, known, data_dir, part, refresh=st.session_state[key] == "refresh")
    except Exception as error:
        st.error(f"Review failed: {error}")
        return None
    st.session_state[key] = "shown"
    result = out["result"]
    st.markdown(f"**{md(result['headline'])}**")
    texts = [result["headline"], *[f"{h['title']} {h['summary']}" for h in result.get("highlights", [])]]
    for finding in result["findings"]:
        texts += [finding["observation"], finding["interpretation"], finding["suggestion"], *finding["evidence"]]
    check = ai.check_numbers(texts, [s, [c["output"] for c in out["tool_calls"]]])
    if check["unverified"]:
        st.warning(f"{len(check['unverified'])} of {check['stated']} numbers weren't found in the statistics or tool "
                   f"results: {md(', '.join(check['unverified']))}. Check these before sharing.")
    else:
        st.markdown(f"All {check['stated']} numbers Claude stated were found in the statistics or tool results.")
    st.caption(f"{'Saved result' if out['from_cache'] else 'New result'} from {out['created'][:16].replace('T', ' ')} · "
               f"{out['model']} · {len(out['tool_calls'])} tool calls")

    for n, finding in enumerate(result["findings"]):
        with st.container(border=True):
            st.markdown(f"**{md(finding['title'])}** · {finding['confidence']} evidence")
            st.markdown(md(finding["observation"]))
            for item in finding["evidence"]:
                st.markdown(f"- {md(item)}")
            st.markdown(f"**What it likely means:** {md(finding['interpretation'])}")
            st.markdown(f"**Suggestion:** {md(finding['suggestion'])}")
            if finding["case_studies"]:
                st.markdown("Case studies: " + md(", ".join(finding["case_studies"])))
            if st.button("Add to investigation log", key=f"log_{key}_{n}"):
                append_finding(scope, finding)
                st.success("Added to the investigation log.")
    with st.expander("How Claude worked this out"):
        for n, call in enumerate(out["tool_calls"], 1):
            st.markdown(f"**{n}. {call['name']}** `{call['input']}`")
            st.json(call["output"], expanded=False)
    return out


def append_finding(scope: str, finding: dict) -> None:
    """Append an approved finding to the investigation log."""
    INVESTIGATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    new_file = not INVESTIGATION_LOG.exists()
    with INVESTIGATION_LOG.open("a", encoding="utf-8") as stream:
        if new_file:
            stream.write("# Investigation log\n\nFindings from report reviews, added after a person approved them.\n")
        evidence = "\n".join(f"  - {e}" for e in finding["evidence"])
        cases = ", ".join(finding["case_studies"]) or "none"
        stream.write(
            f"\n## {finding['title']}\n\n"
            f"- **Added:** {now()[:10]} · **Scope:** {scope} · **Confidence:** {finding['confidence']}\n"
            f"- **Observed:** {finding['observation']}\n- **Evidence:**\n{evidence}\n"
            f"- **Interpretation:** {finding['interpretation']}\n- **Suggestion:** {finding['suggestion']}\n"
            f"- **Case studies:** {cases}\n"
        )
