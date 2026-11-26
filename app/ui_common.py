"""Pieces shared by several pages: claim-list filters, safe markdown, and the "Ask about your claims" box."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from app import ai
from app.review import SERVICE_NAMES
from checker.claim_checker import parse_date


def md(text) -> str:
    """Escape dollar signs so Streamlit doesn't render amounts as maths."""
    return str(text).replace("$", "\\$")


def filter_claims(items: list, key: str, visits: dict) -> list:
    """Date-of-service range, service and a search box (patient, caregiver or claim ID) above a claim list.

    items: objects with a .row claim dict and .claim_id. visits: EVV rows by (patient, service, date).
    """
    if not items:
        return items
    dates = [parse_date(i.row["date_of_service"]) for i in items]
    cols = st.columns([2, 1, 2])
    picked = cols[0].date_input("Date of service", value=(min(dates), max(dates)),
                                min_value=min(dates), max_value=max(dates), key=f"{key}_dates")
    services = sorted({i.row["service_code"].strip().upper() for i in items})
    service = cols[1].selectbox("Service", ["All"] + services, key=f"{key}_service",
                                format_func=lambda s: s if s == "All" else f"{SERVICE_NAMES.get(s, s).capitalize()} ({s})")
    search = cols[2].text_input("Search", placeholder="Patient, caregiver or claim ID", key=f"{key}_search").strip().upper()

    start, end = (picked if isinstance(picked, (tuple, list)) and len(picked) == 2 else (min(dates), max(dates)))
    shown = []
    for item, day in zip(items, dates):
        r = item.row
        code = r["service_code"].strip().upper()
        if not start <= day <= end or service not in ("All", code):
            continue
        if search:
            caregivers = {v["caregiver_id"] for v in visits.get((r["patient_id"], code, day.isoformat()), [])}
            haystack = {item.claim_id.upper(), r["patient_id"].upper(), *caregivers}
            if not any(search in value for value in haystack):
                continue
        shown.append(item)
    return shown


def ask_box(key: str, scope: str, data_dir: Path, frame: pd.DataFrame, *, agency: str | None = None) -> None:
    """Ask Claude a question and show the data returned by its analysis tools."""
    with st.container(border=True):
        st.markdown(f"**Ask about your claims** · {scope}")
        with st.form(f"ask_{key}", clear_on_submit=False, border=False):
            cols = st.columns([5, 1])
            question = cols[0].text_input(
                "Question", label_visibility="collapsed",
                placeholder="e.g. Which patients had Harbor daily-cap denials in October?")
            cols[1].form_submit_button("Ask", type="primary", use_container_width=True)
        if not question:
            return
        try:
            with st.spinner("Claude is looking through the claims…"):
                out = ai.ask(question, scope, data_dir, frame, agency=agency)
        except Exception as error:
            st.error(f"Couldn't answer: {error}")
            return
        result = out["result"]
        st.markdown(md(result["answer"]))
        tables = [table_from(c["output"]) for c in out["tool_calls"]]
        tables = [t for t in tables if t is not None and len(t)]
        if tables:
            with st.expander("Data behind this answer"):
                for t in tables:
                    st.dataframe(t, hide_index=True, width="stretch")
        if out["tool_calls"]:
            with st.expander("Queries used"):
                for call in out["tool_calls"]:
                    st.markdown(f"`{call['name']}`")
                    st.json(call["input"], expanded=False)


def table_from(output) -> pd.DataFrame | None:
    """Turn a tool result into a table where it has rows (breakdown groups, sample claims, timeline events)."""
    if not isinstance(output, dict):
        return None
    for field in ("groups", "claims", "events"):
        if isinstance(output.get(field), list):
            rows = [{k: (v if not isinstance(v, dict) else ", ".join(f"{a} {b}" for a, b in v.items()))
                     for k, v in row.items()} for row in output[field]]
            return pd.DataFrame(rows)
    if isinstance(output.get("total"), dict):
        total = output["total"]
        return pd.DataFrame([{k: (v if not isinstance(v, dict) else ", ".join(f"{a} {b}" for a, b in v.items()))
                              for k, v in total.items()}])
    return None


def days_label(days: int | None) -> str:
    if days is None:
        return "No limit"
    return f"{-days} days late" if days < 0 else f"{days} days"


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
