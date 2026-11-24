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

### Historical denial and draft review rates

The drafter produced **4,980 drafts**, of which **3,619 (72.7%)** matched a historical claim for the same payer, patient, service, and date of service.

| Comparison | Historical denial rate | Draft review rate before confirmation |
|---|---:|---:|
| Overall | 1,099 of 3,830 historical claims (28.7%) | 345 of 4,980 drafts (6.9%) |
| Same 3,619 matched services | 888 of 3,619 had only denied claims (24.5%) | 255 of 3,619 drafts (7.0%) |

The matched comparison uses the same services on both sides. Where a service had a paid original and a later denied duplicate, it counts as historically paid. The draft review rate measures drafts the checker would hold before submission in the draft-only replay. Actual payer outcomes for the generated drafts are unavailable.

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

The drafter generated **4,980 drafts**. The history contains **3,830 unique claims**. A draft matches history when the payer, patient, service, and date of service are the same.

| Recorded outcomes for the matching historical claims | Drafts |
|---|---:|
| Paid only | 2,704 |
| Denied only | 888 |
| Both paid and denied | 27 |
| **Subtotal with a historical match** | **3,619** |
| No corresponding claim in history | 1,361 |
| **Total drafts** | **4,980** |

In all **27** mixed cases, the original claim was paid and a later duplicate was denied. Of the **1,361** drafts without a historical match, **470** matched the pending claims file and **891** matched neither file.

Looking from the historical side, **181 historical claims had no corresponding draft**. Of those, **0 were paid** and **181 were denied**.
