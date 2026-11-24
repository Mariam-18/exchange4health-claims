# Design decisions: alerts, reports and roles

Choices made while building the sign-in roles, the Alerts page and the Reports page (24 November 2026), with the reason for each. Edit freely.

## Roles

- **Two kinds of account.** Agencies (AG001–AG012) see Check claims, Draft claims, Reports for their own agency, and Alerts as notices. The "Exchange4Health rules team" sees Alerts with approvals and Reports across all agencies. Approving a payer rule affects every agency, so it isn't an agency's decision.
- **Agency notices.** When the rules team approves a change, each agency sees "Rule changed: …" with how many of its pending claims the change affects. The count is taken at approval time.

## Rules the AI can change

- **Only dated, bulletin-driven rules moved to a settings file** (`checker/rule_settings.json`): filing limits, Harbor's weekend modifier and Summit's whole hours. Base rules from the manuals (EVV, unit rounding, authorizations, duplicates, Harbor's daily cap, the U2 modifier) stay in the checker code. With the default settings, every output is byte-for-byte identical to before ([LOGIC_CHANGES.md](LOGIC_CHANGES.md)).
- **Three rule types Claude can propose:** a filing limit (days, for dates of service in a period), a weekend modifier, and whole-hour units. Anything else comes back as "needs a developer" rather than being forced into a type. Tested with a made-up telehealth bulletin.
- **The repository's settings are the baseline; the app's approved copy lives in `app_data/`.** `predictions.csv` and the historical test always use the repository settings, so demo changes can't alter the submitted results.
- **A new filing limit ends the payer's open-ended one the day before it starts.** For example, approving "Summit 90 days from 1 December" turns the 60-day rule into "up to 30 November".

## Bulletins

- **Claude (Sonnet 5.5) reads the bulletin with the rule types and the rules in force, and returns structured output:** outcome, summary, and for each change the values, the dates of service it covers, an explanation and **quotes copied from the bulletin**.
- **Python validates before anything can be approved:** known rule type and payer, valid dates, sensible values, and every quote must appear word for word in the bulletin (ignoring case, spacing and markdown). A failed check blocks approval.
- **Python shows the impact:** it replays history and pending claims under the current and proposed rules, then shows how many past claims are predicted correctly, how many predictions change, how many of those changes now match the payer, and the pending claims affected per agency.
- **The approver can set an end date.** Approval and rejection are logged in `app_data/rule_changes.jsonl`.
- **Test set, run live:**
  - The two real bulletins reproduce the hand-coded rules exactly (Northwind 45 days from 1 August 2026; Harbor U3 on weekends from 1 September 2026).
  - A made-up Summit 90-day notice maps correctly.
  - A made-up telehealth bulletin returns "needs a developer".

## Unexpected denials

- **What counts as unexpected:** a denial whose code differs from the checker's prediction, including predicted paid. Groups are by payer, code and remark, with at least 5 claims. Groups that were dismissed or sent to the payer are hidden; groups explained by an approved rule disappear on their own.
- **Claude investigates with tools, not with the raw data in its prompt.** The tools are `breakdown`, `sample_claims`, `distribution`, `timeline` and `test_rule`, and each returns numbers computed by Python. It has up to 14 tool rounds.
- **`test_rule` backtests a candidate on the full history:** group claims explained, paid claims wrongly flagged, and accuracy before and after. Python re-runs the backtest on the final proposal itself, so the numbers shown don't depend on Claude.
- **Three outcomes:** rule proposed, no pattern, or ask the payer. "No pattern" is a valid answer: on Northwind's 14 "Denied - contact payer" D99s, Claude checked services, weekdays, units, visits and filing lag and recommended asking the payer.
- **Inferred rules are labelled as such.** Their source is "Inferred from denial remarks: …", and the approval note says to confirm with the payer.
- **Demo:** "Remove the inferred Summit whole-hour rule" makes the Summit amendment denials unexpected again. Claude rediscovered the rule (whole hours from 1 July 2026), explaining 20 of 20 group claims with 0 paid claims wrongly flagged.

## Reports

- **The period is the user's choice:** by date of service or by submission date. The default is 1 September to 30 November 2026.
- **Standard statistics (Python):**
  - denials by code (count and $)
  - denial rate by month
  - what the checker flags and what the payer did
  - claims that passed without review and how many were paid
  - one-click fix estimate
  - caregiver and patient outliers
  - payers
  - review queue health
  - fix uptake
  - unexpected denial reasons
  - recurring issues
  - re-billed claims
  - time to resolve
  - visits that couldn't become claims
- **Changed from the plan, because the data doesn't support the original:**
  - *Repeat issues after a correction* and *time from denial to correction* need corrected claims, and none of the 3,862 past claims is a corrected claim (frequency code 7). They were replaced by **recurring issues** (the same code for an agency in 3 or more months) and **time to resolve from app activity** (first flagged → the fix that made the claim ready).
  - **Re-billed claims** was added: 30 past claims re-bill a visit, and for 27 of them the first claim had already been paid.
- **Estimates are labelled.** "Denials a one-click fix would have prevented" assumes a fixed claim would have been paid.
- **Outliers need at least 10 claims and a z-score above 2** against the scope's denial rate, so small samples don't show up as patterns.
- **Queue health** counts deadlines from each claim's planned submission date, since the data has no "today". Waiting time comes from app activity.
- **Claude's review (Sonnet 5.5)** gets the statistics, `INVESTIGATION.md` and the investigation log as known context, plus the same analysis tools as the investigations. They work on the chosen scope and period. Claude picks 3–6 statistics to highlight (shown first, with its reason), digs into patterns, traces case studies with `timeline`, and returns findings with evidence, interpretation, suggestion and confidence.
- **A finding goes into `docs/investigation_log.md` only when a person clicks "Add to investigation log".** `INVESTIGATION.md` itself is never edited by the app.
- **Charts:** single-series bars and lines in the app's blue, with tooltips, light gridlines and no dual axes. Each statistic also has a table.

## Checking Claude's work

- **Structured outputs** (Pydantic schemas), so the shape is guaranteed.
- **Number check:** every number Claude states (above 12, excluding dates and IDs) must appear in the statistics or tool results. Percentages may round within 0.5. Unmatched numbers are listed in a warning. In testing, investigations had 11–19 numbers, all found; the report review had 48, with one flagged ("46", taken from a "≤ 45 days" bucket label).
- **Every investigation and review shows "How Claude worked this out"**: each tool call with its input and output.
- **Results are cached** in `app_data/ai_cache/`, keyed on the inputs and the rules in force. A demo replays without new calls; "Read again" or "Review again" makes a fresh call.

## Model and cost

- **Claude Sonnet 5.5** (`claude-sonnet-5-5`) for all three tasks, as chosen.
- **Settings:** adaptive thinking at medium effort for bulletins and high effort for investigations and reports. Prompt caching is on, and the server-side refusal fallback is enabled.
- **Measured costs** (excluding cache writes): a bulletin is about $0.007. An investigation was $0.26 before prompt caching and about $0.02 after, at 6–7 tool calls. A full report review, with 32 tool calls and 90 seconds, was about $0.14. Saved results cost nothing to show again.

## Activity data

- **Stored in `app_data/`** (ignored by git):
  - the activity log: every fix, choice and contact, with recheck result
  - first-flagged times
  - rule settings, rule-change decisions and bulletin status
  - pasted bulletins
  - Claude results
- **"Reset all changes"** clears claims and activity. **"Restore the repository's rules"** and **"Clear the decision log"** are on the Demo controls tab.
