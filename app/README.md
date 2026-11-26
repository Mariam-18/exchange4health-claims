# Claim review app

A Streamlit app for home care agencies' billing teams and for the Exchange4Health rules team. It sits on top of the [claim checker](../checker/README.md) and the [claim drafter](../drafter/README.md):

- **Agencies:** draft claims from verified EVV visits, check unsubmitted claims and fix the simple errors in one click, follow up denials after submission, and read statistics on their own claims.
- **The rules team:** keeps payer rules up to date from bulletins and from denials the rules didn't predict, with Claude doing the reading and investigating and a person approving every change.

## Contents

- [Run it](#run-it)
- [Signing in and the home page](#signing-in-and-the-home-page)
- [Draft claims](#draft-claims)
- [Review unsubmitted claims](#review-unsubmitted-claims)
- [Check claim status](#check-claim-status)
- [Statistics](#statistics)
- [News](#news)
- [What's rules, what's AI](#whats-rules-whats-ai)
- [How it's built](#how-its-built)
- [Saved data](#saved-data)
- [Demo script](#demo-script)
- [Limitations](#limitations)

## Run it

From the repository root, with Python 3.9 or later:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY="..."          # only needed for the AI features
.venv/bin/streamlit run app/streamlit_app.py
```

Open http://localhost:8501.

The app looks for the data pack in `Resonance_assessment_day__confirmation_and_preparation/Resonance - graduate preparation data/`. Set `E4H_DATA_DIR` to use another folder. App state goes to `app_data/`; set `E4H_STATE_DIR` to change that.

## Signing in and the home page

> [insert screenshot: sign-in screen]

Sign-in is a mock: any password works. Pick an account:

| Account | Home page tiles |
|---|---|
| **Agency AG001–AG012** | Draft claims, Review unsubmitted claims, Check claim status, Statistics, News |
| **Exchange4Health rules team** | News & approvals, Statistics |

> [insert screenshot: agency home page with tiles]

**Each tile shows a headline figure,** e.g. "17 need a person · 9 one-click" or "67% paid · 33% denied", and opens its page. Every page has **← Home**.

**The sidebar** has:
- **Sign out**
- **Reset all changes:** restores the original claims and clears saved activity
- `predictions.csv` for all 546 pending claims, in the format the data pack asks for

## Draft claims

Build claims straight from verified EVV visits instead of writing them by hand.

> [insert screenshot: Draft claims after Generate claims]

**How it works:**
1. **Pick the dates of service and click Generate claims.**
2. **The drafter builds one claim per patient, service and date.** Units, authorization and modifier are set by the payer's rules, and the claim is submitted the day after the visit, so it's never late.
3. **Drafts are checked on their own,** as if they replaced hand-written claims. Authorization balances include drafts for earlier dates.

**The review screens are the same as Review unsubmitted claims,** below. What's left for review is almost entirely authorization shortfalls and multi-visit Harbor days.

## Review unsubmitted claims

The agency's pending claims from `claims_to_check.csv`, checked against the full claims history and the payer rules.

> [insert screenshot: Needs a person tab]

**At the top:**
- **Leading issue only:** decides where a claim with both kinds of issue appears. On, it's listed only under Needs a person; off, it's listed in both tabs, and the counts change accordingly.
- **Tiles:** ready to submit, needs a person, one-click fixes, and the billed value at risk.
- **After any change,** a banner reports what the recheck found, including **knock-on effects**. For example, a fixed claim might now use authorization units that a later claim needed.

**Filters on every list:**
- **date of service** (a range)
- **service**
- **search** by patient, caregiver or claim ID. Caregivers are matched through the EVV visit.

**Needs a person:** claims with at least one issue that has no safe automatic fix.
- **Filter** by payer, and by issue with the chips. The chips show counts, largest first.
- **Sort** by days to the filing deadline or by amount at stake.
- **Columns:** the issues needing a person first, then days to deadline, amount, date of service, payer, claim, patient, and any other issues.
- Under **No verified visit**, a second row of chips filters by visit status: rejected, pending manual verification, or no visit found.

**Click any row** to open the claim below the list:

> [insert screenshot: claim details panel for a claim with two issues]

**What the claim panel shows:**
- **A plain-language summary** of the claim.
- **Each issue**, leading issue first, then "Also: …". Each has:
  - a red **Needs a person** or green **One-click fix** label
  - a **?** with the rule as it applies to this claim, where something is specific to it
  - the facts, e.g. "Filing window: 45 days. Deadline: Sat 17 October 2026. Planned submission: 63 days late."
  - the action:
    - **Apply fix**, with the exact change, e.g. "Change units from 10 to 9"
    - for duplicates, **Submit as a correction of C0xxxxx** or **Remove from batch**
    - **Contact [payer]** or **Message EVV coordinator**, which opens a draft message to copy and a **Mark as sent** button
- **Collapsible sections** for the EVV visits that day, the authorization, and the raw checker evidence.

**One-click fixes:** claims whose every issue has exactly one correct answer.

> [insert screenshot: One-click fixes tab]

| Issue | One-click fix |
|---|---|
| Wrong modifier (Harbor) | Set U2, or U3 on weekends from 1 Sep 2026 |
| Missing or wrong authorization | Use the only authorization on file for that patient, service and date |
| Units over verified time, under 30 minutes | Correct units to the payer's rounding rule. For Summit, round down to whole hours in the same step. |
| Harbor daily cap | Only a single 9-hour claim with nothing else that day: 36 → 32 units (likely an unpaid break) |
| Summit whole hours | Round down to the largest whole number of hours that both the bill and the verified visit cover |

- **Tick claims and click Apply to N ticked, or click Apply to all.**
- **With an issue chip selected,** only that issue's fix is applied, so other quick fixes on those claims stay in the queue.
- **Every fix is logged** with a follow-up note, e.g. "Follow-up: the 9-hour visit may include an unpaid break."

**Ready to submit:** claims that pass every rule.

> [insert screenshot: Ready to submit tab after applying fixes]

**Download** gives a CSV in the same format as `claims_to_check.csv`, with the fixes applied (amounts recalculated) and a `changes_made` column, e.g. `units 10 → 9`.

**Activity log:** every fix, choice and contact, with the recheck result.

## Check claim status

Claims the agency has already submitted, and what the payers decided. The data has no current date, so **today is taken as 31 January 2027**, the latest submission date in the history.

> [insert screenshot: Check claim status with the paid/denied ribbon]

**At the top:**
- **A green and red ribbon,** sized by the share of claims paid and denied, with counts and amounts.
- **The Ask box,** for this agency's submitted claims.

**Denied claims are split into four groups,** each claim in one:

| Group | What's in it | What you can do |
|---|---|---|
| **Quick fix & resubmit** | Every issue the checker found has a one-click fix | **Prepare a corrected claim** (frequency code 7, naming the original) with the fixes applied. It's re-checked as if submitted today. |
| **Needs someone** | A missing or rejected visit, late filing, a used-up authorization, or a gap too large to be rounding | The same messages, rule help and contact buttons as Review unsubmitted claims |
| **Duplicates** | The payer said D05. Usually the first claim was already paid. | See the earlier claim and its outcome; **Mark as resolved** |
| **Unexplained denials** | The payer's reason doesn't match anything the checker found | **Contact the payer** with a drafted message; **Mark as resolved** |

**Preparing corrected claims:** each one is re-checked against the full history and the deadline as of today. So the page says plainly when a resubmission would be denied as late ("Deadline passed …") instead of treating it as fixable. For AG009, of 62 quick-fix denials, 34 corrected claims would be paid and 27 are past the filing deadline.

> [insert screenshot: Resubmissions tab]

**The Resubmissions tab** lists every corrected claim prepared, with its predicted outcome, and downloads the ones that pass. Claims already resubmitted or resolved are hidden from the groups unless you switch that off.

## Statistics

Pick a period, by **date of service** or **submission date**. Agencies see their own claims; the rules team can choose any agency or all of them.

> [insert screenshot: Statistics page with the journey chart]

**Ask about your claims:** type a question, e.g. "Which patients had Harbor daily-cap denials in October?"
- Claude answers with the analysis tools, so every number comes from Python.
- The numbers are checked against the data.
- **Data behind this answer** shows the tables it used.
- It only reads, at a few cents a question.

**Review with Claude:** Claude writes 3–5 highlights in plain English, e.g. "Already-paid claims are being billed again…". Each highlight has the statistics behind it underneath, followed by detailed findings (evidence, what it likely means, a suggestion, confidence, case studies). Every number is checked, the tool calls are listed, and **Add to investigation log** appends a finding to `investigation_log.md` in the app's state directory.

> [insert screenshot: Highlighted by Claude]

**Standard statistics (Python):**

| Statistic | What it shows |
|---|---|
| Claims at each step | Verified visits → drafts → drafts needing review → unsubmitted claims → needing review → fixed in the app → submitted → paid → denied → resubmitted |
| Denials by code | Count and billed amount per code |
| Denials by month | Denial rate over time |
| What the checker flags | Claims flagged by expected reason, and what the payer did |
| Passed without review | Claims the checker passed, and how many were paid |
| One-click estimate | Denials whose only issues have one-click fixes (an estimate: assumes a fixed claim is paid) |
| Outliers | Caregivers and patients with a denial rate well above the scope's (at least 10 claims, z above 2) |
| Payers | Denial rate and unexpected-denial rate by payer |
| Review queue health | Claims waiting now, their value, deadlines within 7 days, already late, and hours waiting |
| Fix uptake | Claims offered a one-click fix, fixes applied, and recheck results (from app activity) |
| Unexpected denial reasons | Denials the checker didn't predict, by payer and code |
| Recurring issues | Denial codes an agency got in 3 or more separate months |
| Re-billed claims | New claims for a visit already billed, and whether the first claim was paid |
| Time to resolve | Minutes from first flagged to the action that cleared it (from app activity) |
| Visits that couldn't become claims | Rejected and pending EVV visits |

## News

### Agencies: rule-change notices

> [insert screenshot: agency News page]

When the rules team approves a change, each agency sees it, with how many of its pending claims are affected. Its claim checks use the new rule straight away.

### Rules team: new bulletins

> [insert screenshot: bulletin read by Claude, with impact]

1. **The bulletin is shown on the left.** These come from the data pack's `payers/` folder, or from **Paste a new bulletin**.
2. **Read with Claude:** Claude returns its reading of the bulletin and a proposed rule, with the sentences from the bulletin behind each value.
3. **Python checks the proposal:** the rule type, payer, dates and values are valid, and every quote appears word for word in the bulletin. A failed check blocks approval.
4. **Python shows the impact.** It replays past and pending claims under the current and proposed rules, and shows:
   - past claims predicted correctly, before → after
   - how many predictions change
   - how many of those now match the payer
   - pending claims affected, by agency
5. **Approve**, optionally with an end date, or **Reject**. A bulletin that needs a kind of rule the checker can't express is sent to developers.

### Rules team: unexpected denials

> [insert screenshot: Claude's investigation of unexpected denials]

**How it works:**
1. **Python finds denials the checker didn't predict,** or predicted with another code, and groups them by payer, code and remark (at least 5 claims).
2. **Investigate with Claude:** Claude works through the claims history with analysis tools (see [What's rules, what's AI](#whats-rules-whats-ai)). It returns one of three outcomes, with reasons drawn from the data:
   - **a proposed rule**
   - **no pattern**
   - **ask the payer**
3. **Python checks the result:**
   - It re-runs the backtest on any proposed rule, so the "explains 20 of 20, wrongly flags 0" line doesn't depend on Claude.
   - Every number Claude stated is checked against what the tools returned. Anything it can't find is listed in a yellow warning, e.g. a total Claude added up itself.
4. **"How Claude worked this out"** lists every tool call with its input and output.
5. **Approve as an inferred rule** (labelled "Inferred from denial remarks", to confirm with the payer), **Ask the payer**, or **Dismiss**.

### Rules team: rules in force, decision log, demo controls

> [insert screenshot: Rules in force]

**Rules in force** lists the dated payer rules the checker reads from settings, with their sources. **Decision log** lists every approval, rejection and dismissal.

**Demo controls** set up the scenarios:
- treat the Harbor weekend-modifier bulletin as new
- remove the inferred Summit whole-hour rule
- restore the repository's rules
- clear the decision log

## What's rules, what's AI

| | Rules (Python) | AI (Claude Sonnet 5.5) | People |
|---|---|---|---|
| Each claim | Applies the payer rules, predicts paid or denied, works out one-click fixes and corrected claims | Not used | Approve fixes, choose for duplicates, prepare resubmissions, contact payers and coordinators |
| Bulletins | Validates the proposal, quotes and dates; replays claims to show the impact | Reads the bulletin and proposes a rule change with quotes | Approve, set an end date, or reject |
| Unexpected denials | Groups them; runs every query and backtest | Decides what to look at, tests hypotheses, explains why | Approve an inferred rule, ask the payer, or dismiss |
| Statistics | Computes every statistic | Writes highlights, explores patterns, traces cases, suggests changes; answers questions | Decide which findings go in the investigation log |

**Claude never decides a claim** and never changes a rule on its own. It works through these tools, which return numbers computed by Python:

| Tool | Returns |
|---|---|
| `breakdown` | Claims, paid, denied by code, unexpected denials and amount, overall or grouped by up to two fields (payer, month, weekday, units modulo 4, visit status, gap bucket and others) |
| `sample_claims` | Up to 15 matching claims with key fields |
| `distribution` | Quantiles and common values of a numeric field, such as minutes over verified time |
| `timeline` | Every visit and claim, in date order, for a patient, caregiver or claim |
| `test_rule` | (Investigations only) A backtest of a candidate rule on the full history |

## How it's built

| File | What it does |
|---|---|
| `streamlit_app.py` | Sign-in, home tiles, navigation, and the Draft claims and Review unsubmitted claims pages |
| `review.py` | Turns checker findings into review items: plain-language messages, rule help, one-click changes, duplicate choices, contact drafts |
| `status.py`, `ui_status.py` | Check claim status: grouping denials, preparing and re-checking corrected claims, the page |
| `ui_common.py` | Claim-list filters, the claim list, the Ask box |
| `activity.py` | Saves the activity log and when each claim was first flagged |
| `rules_admin.py` | Rules in force, proposed changes (validate, apply, describe), impact replay, approvals, bulletin inbox |
| `analysis.py` | The table of past claims with derived fields, and the analysis tools Claude calls |
| `monitoring.py` | Backtests of candidate rules and the unexpected-denial investigation |
| `stats.py` | The standard statistics |
| `ai.py` | Claude calls: output schemas, the tool loop, saved results and the number check |
| `ui_alerts.py`, `ui_reports.py` | The News and Statistics pages |

**Claude calls:**
- **Model and endpoint:** `claude-sonnet-5-5` through the Anthropic SDK's beta messages endpoint.
- **Output shape:** structured outputs (Pydantic schemas), so the shape is guaranteed.
- **Settings:** effort is medium for bulletins and questions, and high for investigations and reviews. Prompt caching is on, and the server-side refusal fallback is enabled.
- **Tool rounds:** up to 14 per task.

**Costs (measured):** about $0.01 per bulletin, $0.02 per investigation or question, and $0.15 per review.

Every result is saved, keyed on its inputs and the rules in force. Showing it again is free; **Read again**, **Investigate again** and **Review again** make a fresh call.

**Look:** the case study's navy (`#1F3864`), serif headings and sans-serif body text. The theme is in `.streamlit/config.toml`.

The checker reads its dated payer rules (filing limits, Harbor's weekend modifier, Summit's whole hours) from [`checker/rule_settings.json`](../checker/rule_settings.json). Approved changes are written to the app's copy in `app_data/`, so the repository's settings, `predictions.csv` and the historical test never change.

See the [main README](../README.md#rules-ai-and-people) for the roles of Python, Claude, and agency staff.

## Saved data

Everything the app saves goes in `app_data/`, which git ignores:

| File | Contents |
|---|---|
| `activity_log.jsonl` | Every fix, choice, contact, resubmission and resolution: time, agency, claim, issue codes, action, result |
| `first_flagged.json` | When each claim first needed review, with its issue codes |
| `resubmissions.jsonl` | Corrected claims prepared on Check claim status, with their predicted outcome |
| `rule_settings.json` | The app's approved rules (absent until a change is made) |
| `rule_changes.jsonl` | Every approval, rejection, dismissal and "ask the payer", with the agencies affected |
| `bulletin_status.json`, `bulletins/` | Bulletin review status, and pasted bulletins |
| `ai_cache/` | Saved Claude results |

## Demo script

About 10 minutes.

**As the agency (sign in as Agency AG009)**
1. **Home:** walk through the five tiles.
2. **Draft claims → Generate claims.** Only 6.9% of drafts need review across all agencies, against 28.7% of hand-written claims denied historically.
3. **Review unsubmitted claims:** click a late claim to show the panel and the **?** help. On **One-click fixes**, click **Apply to all**, then show the recheck banner and the **Ready to submit** download.
4. **Check claim status:** the ribbon. On **Quick fix & resubmit**, click **Prepare all**: 34 would now be paid and 27 are too late. Then show a duplicate and an unexplained denial.

**As the rules team (sign out, then sign in as Exchange4Health rules team)**

5. **News & approvals → Demo controls:** click **Treat the Harbor weekend-modifier bulletin as new**.
6. **Bulletins → Read with Claude:** show the quotes and the impact (+25 past claims correct, 15 pending affected). Click **Approve**.
7. **Demo controls:** click **Remove the inferred Summit whole-hour rule**.
8. **Unexpected denials → Investigate with Claude** on the Summit "Contract amendment 3" group. Claude rediscovers the rule: whole hours from 1 July, explaining 20 of 20 claims with 0 paid claims wrongly flagged.
9. **Statistics → Generate report → Review with Claude,** then ask a question in the Ask box.

**Back as the agency**

10. **Sign in as Agency AG003 → News:** the rule changes, and how many of its claims they affect.

## Limitations

- **Mock sign-in:** there are no real accounts or permissions.
- **There's no "today" in the data.** Pending claims count deadlines from their planned submission date, and Check claim status takes today as 31 January 2027.
- **Activity statistics** (fix uptake, time to resolve, waiting time, resubmissions) only cover what's been done in the app.
- **The one-click estimate** assumes a fixed claim would have been paid.
- **Claude can only propose three rule types:** filing limits, weekend modifiers and whole-hour units. Anything else goes to a developer.
- **The past claims contain no corrected claims** (frequency code 7), so time from denial to correction can't be measured from history.
