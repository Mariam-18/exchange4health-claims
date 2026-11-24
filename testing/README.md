# Claim checker testing

[checker_tester.py](checker_tester.py) tests the [claim checker](../checker/README.md) against claims with known payer outcomes. It replays the synthetic claims history, compares each prediction with the recorded paid or denied result, and shows where the checker agrees with the payer or misses a denial.

## What the test uses

| Input | Purpose |
|---|---|
| `claims_history.csv` | Historical claims and their recorded outcomes, which the tester uses to score the predictions. |
| `authorizations.csv` and `evv_visits.csv` | Authorization and verified visit evidence used by the checker. |
| `claims_to_check.csv` | Pending claims used to estimate authorization usage during the replay. The tester scores historical claims only. |

## Run the test

From the repository root, with Python 3.10 or later:

```bash
python -m testing.checker_tester /path/to/data/claims_history.csv
```

The tester looks for the companion CSVs alongside the history file. If they’re in a different directory, pass `--data-dir /path/to/companion-csvs`.

## Baseline results

The synthetic history contains 3,862 rows and **3,830 unique claims**. Each row of the table shows what the payer recorded. The columns show what the checker predicted.

| Recorded outcome | Predicted paid | Predicted denied |
|---|---:|---:|
| Paid | 2,730 | 1 |
| Denied | 61 | 1,038 |

The prediction matched the recorded outcome for **3,768 of 3,830 claims (98.38%)**. The checker missed **61** recorded denials, all of which were **D99** (Other / referred for manual review). It flagged **one** claim that was paid. Among the **1,038** claims both recorded and predicted as denied, the predicted denial code matched in **1,026 cases (98.84%)**. These figures describe a replay of the synthetic history used to develop the checker.

To inspect individual predictions and failed checks, export the audit from the same data directory:

```bash
python -m checker.claim_checker /path/to/data --audit history_audit.csv
```

## Claim drafter testing

The [drafter](../drafter/claim_drafter.py) generates claims from verified visits and evaluates them with the shared checker. The outcome is readiness for submission. The historical match joins on payer, patient, service, and date of service.

Run from the repository root:

```bash
python -m drafter.claim_drafter /path/to/data --output-dir /path/to/draft_results
python -m drafter.claim_drafter /path/to/data --check-history --output-dir /path/to/history_check_results
```

### Current run on the synthetic data

Both modes generated **4,980 drafts**. The draft-only run reproduced the earlier **4,635 ready** and **345 needing review**. The second mode includes recorded submissions in the duplicate and usage checks.

| Mode | Ready before confirmation | Needs review before confirmation | Ready if all eligible reductions are confirmed | Needs review after confirmation |
|---|---:|---:|---:|---:|
| Draft-only replay | 4,635 | 345 | 4,736 | 244 |
| Check against recorded submissions | 4,020 | 960 | 4,095 | 885 |

In the draft-only run, the **345** needing review comprised **231** authorization shortfalls and **114** Harbor reduction requests. In the immediate-confirmation simulation, **113** Harbor drafts passed with fewer units. Those releases used authorization capacity that some later drafts needed.

### Coverage against historical claims

| Historical match | Drafts |
|---|---:|
| A paid claim exists | 2,731 |
| Only denied claims exist | 888 |
| No corresponding claim in history | 1,361 |
| **Total** | **4,980** |

**3,619 drafts** matched a historical claim. The paid group includes **27** service keys with a paid original and a later denied duplicate. Of drafts without a historical match, **470** matched the pending claims file and **891** matched neither file. **All 2,731 historically paid claims had a corresponding draft.** The **181 historical claims without a draft** were all denied.
