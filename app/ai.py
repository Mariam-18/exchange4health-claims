"""Claude calls: reading bulletins, investigating unexpected denials, and exploring report statistics.

Python computes every number. Claude interprets, decides what to look at next through the
analysis tools, and returns structured output that Python validates before anything is shown.
Results are cached on disk so a demo can be replayed without new API calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, List, Literal, Optional

import anthropic
from pydantic import BaseModel

from app import analysis
from app.activity import STATE_DIR, now
from app.rules_admin import PAYERS, RULE_TYPES

MODEL = "claude-sonnet-5-5"
CACHE_DIR = STATE_DIR / "ai_cache"
MAX_TOOL_ROUNDS = 14


# ---------- output shapes ----------

class RuleChange(BaseModel):
    rule_type: Literal["filing_limit", "weekend_modifier", "whole_hour_units"]
    payer: Literal["PY01", "PY02", "PY03"]
    days: Optional[int]
    service_code: Optional[str]
    modifier: Optional[str]
    effective_from: Optional[str]
    effective_to: Optional[str]
    explanation: str


class QuotedRuleChange(RuleChange):
    evidence_quotes: List[str]


class BulletinReading(BaseModel):
    outcome: Literal["rule_change", "no_rule_change", "needs_developer"]
    summary: str
    changes: List[QuotedRuleChange]
    reason_if_no_change: str


class Investigation(BaseModel):
    outcome: Literal["rule_proposed", "no_pattern", "ask_payer"]
    interpretation: str
    why: List[str]
    proposed_change: Optional[RuleChange]
    suggestion: str


class Finding(BaseModel):
    title: str
    observation: str
    evidence: List[str]
    interpretation: str
    suggestion: str
    case_studies: List[str]
    confidence: Literal["strong", "moderate", "weak"]


StatKey = Literal[
    "denials_by_code", "denials_by_month", "flagged_by_reason", "passed_without_review", "one_click_estimate",
    "outliers", "payers", "queue_health", "fix_uptake", "unexpected_reasons", "recurring_issues",
    "rebilled_claims", "time_to_resolve", "visits_not_drafted",
]


class Highlight(BaseModel):
    title: str
    summary: str
    stat_keys: List[StatKey]


class ReportReview(BaseModel):
    headline: str
    highlights: List[Highlight]
    findings: List[Finding]


class Answer(BaseModel):
    answer: str


# ---------- tools ----------

FILTERS_SCHEMA = {
    "type": "object",
    "description": (
        "Optional filters. Keys: any groupable field (value or list of values), service_date_from, "
        "service_date_to, submitted_date_from, submitted_date_to (YYYY-MM-DD), remark_contains (text)."
    ),
}


def analysis_tools(include_test_rule: bool) -> list[dict]:
    fields = "\n".join(f"- {k}: {v}" for k, v in analysis.FIELDS.items())
    tools = [
        {"name": "breakdown",
         "description": f"Count claims, paid, denied (by code), unexpected denials and billed amount, overall or "
                        f"grouped by up to 2 fields, for claims matching the filters. Groupable fields:\n{fields}",
         "input_schema": {"type": "object", "properties": {
             "filters": FILTERS_SCHEMA,
             "group_by": {"type": "array", "items": {"type": "string", "enum": list(analysis.FIELDS)}, "maxItems": 2}},
             "required": ["group_by"]}},
        {"name": "sample_claims",
         "description": "Return up to 15 matching claims with key fields (units, verified minutes, visit status, "
                        "caregivers, outcome, remark, checker prediction), earliest date of service first.",
         "input_schema": {"type": "object", "properties": {
             "filters": FILTERS_SCHEMA, "n": {"type": "integer", "minimum": 1, "maximum": 15}}, "required": []}},
        {"name": "distribution",
         "description": "Quantiles and most common values of a numeric field for matching claims.",
         "input_schema": {"type": "object", "properties": {
             "field": {"type": "string", "enum": sorted(analysis.NUMERIC)}, "filters": FILTERS_SCHEMA},
             "required": ["field"]}},
        {"name": "timeline",
         "description": "All visits and claims, in date order, for one patient, caregiver, or the patient behind a claim. "
                        "Use it to trace a case study.",
         "input_schema": {"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["patient", "caregiver", "claim"]}, "id": {"type": "string"}},
             "required": ["kind", "id"]}},
    ]
    if include_test_rule:
        tools.append({
            "name": "test_rule",
            "description": "Backtest a candidate rule on the full claims history. Returns how many claims in the "
                           "group under investigation it explains (predicted with the payer's code), how many paid "
                           "claims it would wrongly flag, and overall accuracy before and after.",
            "input_schema": {"type": "object", "properties": {
                "rule_type": {"type": "string", "enum": list(RULE_TYPES)},
                "payer": {"type": "string", "enum": list(PAYERS)},
                "days": {"type": ["integer", "null"]}, "service_code": {"type": ["string", "null"]},
                "modifier": {"type": ["string", "null"]},
                "effective_from": {"type": ["string", "null"]}, "effective_to": {"type": ["string", "null"]}},
                "required": ["rule_type", "payer"]},
        })
    return tools


def run_tool(name: str, args: dict, data_dir: Path, frame, test_rule: Callable | None,
             *, agency: str | None = None) -> dict:
    if name == "breakdown":
        return analysis.breakdown(frame, args.get("filters"), args.get("group_by", []))
    if name == "sample_claims":
        return analysis.sample_claims(frame, args.get("filters"), args.get("n", 10))
    if name == "distribution":
        return analysis.distribution(frame, args["field"], args.get("filters"))
    if name == "timeline":
        return analysis.timeline(data_dir, frame, args["kind"], args["id"], agency=agency)
    if name == "test_rule" and test_rule:
        return test_rule(args)
    raise ValueError(f"Unknown tool {name}")


# ---------- calling Claude ----------

def client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def call(system: str, user: str, output: type[BaseModel], *, tools: list[dict] = (),
         tool_runner: Callable | None = None, effort: str = "medium") -> dict:
    """Run one task with optional tool use and return the parsed output and a log of tool calls.

    Output: {"result": dict, "tool_calls": [{name, input, output}], "usage": {...}}
    """
    messages = [{"role": "user", "content": user}]
    tool_calls, usage = [], {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    for round_number in range(MAX_TOOL_ROUNDS + 1):
        extra = {"tools": list(tools)} if tools else {}
        if tools and round_number == MAX_TOOL_ROUNDS:
            extra["tool_choice"] = {"type": "none"}
            messages.append({"role": "user", "content": "Tool budget used up. Give your final answer now."})
        response = client().beta.messages.parse(
            model=MODEL, max_tokens=16000, system=system, messages=messages,
            output_format=output, output_config={"effort": effort}, cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **extra,
        )
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        usage["cache_read_input_tokens"] += response.usage.cache_read_input_tokens or 0
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's answer was cut off (max tokens).")
        if response.stop_reason != "tool_use":
            if response.parsed_output is None:
                raise RuntimeError("Claude's answer didn't match the expected format.")
            return {"result": response.parsed_output.model_dump(), "tool_calls": tool_calls, "usage": usage}
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                output_data = tool_runner(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(output_data, default=str)})
            except Exception as error:  # report tool errors back to Claude
                output_data = {"error": str(error)}
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"Error: {error}", "is_error": True})
            tool_calls.append({"name": block.name, "input": block.input, "output": output_data})
        messages.append({"role": "user", "content": results})
    raise RuntimeError("Claude didn't finish within the tool budget.")


def cached(kind: str, key_data, compute: Callable[[], dict], refresh: bool = False) -> dict:
    """Return a saved result for the same inputs, or compute, save and return a new one."""
    key = hashlib.sha256(json.dumps(key_data, sort_keys=True, default=str).encode()).hexdigest()[:16]
    path = CACHE_DIR / f"{kind}_{key}.json"
    if path.exists() and not refresh:
        return {**json.loads(path.read_text(encoding="utf-8")), "from_cache": True}
    result = {**compute(), "created": now(), "model": MODEL}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return {**result, "from_cache": False}


def catalogue_text(rules: dict) -> str:
    types = "\n".join(f"- {k}: {v}" for k, v in RULE_TYPES.items())
    payers = ", ".join(f"{k} = {v}" for k, v in PAYERS.items())
    return (f"Rule types the checker supports:\n{types}\n\nPayers: {payers}\n\n"
            f"Rules currently in force (JSON):\n{json.dumps(rules, indent=1)}")


# ---------- tasks ----------

BULLETIN_SYSTEM = """You maintain payer billing rules for Exchange4Health, a home care claims platform. \
A payer has issued a bulletin. Map any billing-rule change it makes to the rule types the claims checker supports.

- Use only the listed rule types. If the bulletin changes a billing rule that none of them can express, \
set outcome to needs_developer and explain what kind of rule is needed.
- If the bulletin changes no billing rule, set outcome to no_rule_change.
- For every change, evidence_quotes must contain the exact sentences from the bulletin that support each value, \
copied word for word. Don't paraphrase inside quotes.
- effective_from and effective_to are dates of service (YYYY-MM-DD) the change covers. Use null when the bulletin \
doesn't give one. Don't guess dates.
- Fill only the fields the rule type uses; set the others to null.
- explanation: say what changes for billers. If it overrides an existing rule, name that rule or its source \
and state the service dates for which the override applies.
- summary: one sentence on what the bulletin says."""


def read_bulletin(name: str, text: str, rules: dict, refresh: bool = False) -> dict:
    """Ask Claude to turn a bulletin into proposed rule changes."""
    user = f"{catalogue_text(rules)}\n\nBulletin ({name}):\n<bulletin>\n{text}\n</bulletin>"
    return cached("bulletin", [BULLETIN_SYSTEM, name, text, rules], lambda: call(BULLETIN_SYSTEM, user, BulletinReading), refresh)


INVESTIGATION_SYSTEM = """You investigate claims that a payer denied with a code the rules-based checker didn't \
predict. Decide whether a payer rule we don't yet have explains them.

Work from the data with the tools:
1. Look at the group's claims and remarks.
2. Compare them with similar claims from the same payer that were paid, before and after the denials started. \
Look for a field that separates denied from paid (units, units_mod_4, modifier, weekday, timing, verified-time gap, service).
3. Find when the pattern starts.
4. If you have a candidate the supported rule types can express, backtest it with test_rule. Propose it only if it \
explains most of the group and wrongly flags few or no paid claims.

Outcomes: rule_proposed (with proposed_change), no_pattern (nothing in the data separates these claims; set \
proposed_change to null), or ask_payer (the remarks point to a rule we can't express or can't confirm from the data).

Use only numbers returned by the Python tools or supplied in the Python statistics. For each number, keep the metric, group, and period tied to the source. If asked how a number was obtained, give the exact tool name and input arguments (including filters and grouping) that produced it. If the result cannot be reproduced with those inputs, leave it out.
Treat patterns in fewer than 10 claims as weak. Keep interpretation and suggestion short and plain."""


def investigate(group: dict, data_dir: Path, frame, rules: dict, test_rule: Callable, refresh: bool = False) -> dict:
    """Ask Claude to explain a group of unexpected denials using the analysis tools."""
    summary = {k: v for k, v in group.items() if k != "claim_ids"}
    user = (f"{catalogue_text(rules)}\n\nGroup of unexpected denials:\n{json.dumps(summary, indent=1)}\n"
            f"Filter for exactly these claims: {{\"payer\": \"{group['payer']}\", \"denial_code\": "
            f"\"{group['denial_code']}\", \"remark_contains\": {json.dumps(group['remark'])}, \"unexpected\": true}}")
    tools = analysis_tools(include_test_rule=True)
    return cached("investigation", [INVESTIGATION_SYSTEM, group["key"], group["claims"], rules],
                  lambda: call(INVESTIGATION_SYSTEM, user, Investigation, tools=tools, effort="high",
                               tool_runner=lambda n, a: run_tool(n, a, data_dir, frame, test_rule)),
                  refresh)


REPORT_SYSTEM = """You review a billing report for Exchange4Health, a home care claims platform. Python has \
computed the standard statistics below for the chosen scope and period. Your job:

1. Write 3 to 5 highlights: the most interesting results of this period for a billing lead. Each has a short \
title, a plain-English summary of two or three sentences (what happened, how big it is, why it matters), and \
stat_keys: the keys of the statistics that back it up (e.g. "rebilled_claims"). Skip what shows nothing unusual.
2. Dig into what looks unusual with the tools: break it down, compare groups, check distributions. Decide whether \
a real pattern exists. Report patterns that hold up, and say plainly when something you checked turned out to be \
noise.
3. For your strongest findings, trace one or two case studies with the timeline tool (a patient, caregiver or \
claim) and cite their IDs.
4. Give a practical suggestion for each finding: a process change, a conversation with caregivers, a question for \
a payer, or a rule to review.

Use what's already known (below) to interpret, without repeating it as new. For example, 9-hour Harbor visits \
against an 8-hour cap suggest unpaid breaks inside the shift; unit gaps under 30 minutes are usually rounding; \
late filing is about the agency's process, not caregivers.

Use only numbers returned by the Python tools or supplied in the Python statistics. For each number, keep the metric, group, and period tied to the source. If asked how a number was obtained, give the exact tool name and input arguments (including filters and grouping) that produced it. If the result cannot be reproduced with those inputs, leave it out.
Treat patterns in fewer than 10 claims as weak and say so. Write plainly and briefly."""


def review_report(scope: str, stats: dict, known: str, data_dir: Path, frame,
                  refresh: bool = False, *, agency: str | None = None) -> dict:
    """Ask Claude to pick the statistics to highlight and explore patterns behind them."""
    user = (f"Scope and period: {scope}\n\nWhat's already known:\n<known>\n{known}\n</known>\n\n"
            f"Standard statistics (JSON):\n{json.dumps(stats, indent=1, default=str)}\n\n"
            "The tools work on claims within this scope and period.")
    tools = analysis_tools(include_test_rule=False)
    return cached("report", [REPORT_SYSTEM, agency, scope, stats, known],
                  lambda: call(REPORT_SYSTEM, user, ReportReview, tools=tools, effort="high",
                               tool_runner=lambda n, a: run_tool(n, a, data_dir, frame, None, agency=agency)),
                  refresh)


ASK_SYSTEM = """You answer questions from a home care billing team about their claims, using the analysis \
tools over the claims history for the given scope. Each claim has a payer outcome (paid or denied with a code), \
the checker's prediction, EVV visit details and caregivers.

- Use only numbers returned by the Python tools or supplied in the Python statistics. For each number, keep the metric, group, and period tied to the source. If asked how a number was obtained, give the exact tool name and input arguments (including filters and grouping) that produced it. If the result cannot be reproduced with those inputs, leave it out.
- Answer in a few plain sentences, leading with the direct answer. Name claims, patients or caregivers where \
that helps.
- If the data can't answer the question, say so and say what it can tell them instead.
- Codes: D01 no verified visit, D02 authorization used up, D03 late filing, D04 modifier, D05 duplicate, \
D06 Harbor daily cap, D07 missing authorization number, D08 wrong authorization, D09 units over verified time, \
D10 Summit whole hours, D99 other. Payers: PY01 Northwind, PY02 Harbor, PY03 Summit."""


def ask(question: str, scope: str, data_dir: Path, frame, *, agency: str | None = None) -> dict:
    """Answer a free-text question about claims in scope with the analysis tools."""
    user = f"Scope: {scope}\n\nQuestion: {question}"
    tools = analysis_tools(include_test_rule=False)
    return cached("ask", [ASK_SYSTEM, agency, scope, question.strip().lower(), len(frame)],
                  lambda: call(ASK_SYSTEM, user, Answer, tools=tools, effort="medium",
                               tool_runner=lambda n, a: run_tool(n, a, data_dir, frame, None, agency=agency)))
