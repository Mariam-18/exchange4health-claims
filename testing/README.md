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

We replayed the synthetic visit data as if the drafter had replaced the original claim preparation workflow. It generated **4,980 drafts**. The checker marked **4,635 (93.1%)** ready to submit and flagged **345 (6.9%)** for agency review. Historically, **1,099 of 3,830 claims (28.7%)** were denied. The replay estimates how many drafts would be held before submission. Actual payer outcomes for the drafts are unknown.

Two limitations affect the comparison:

- **Unexplained denials.** D99 accounted for **73 of 1,099 historical denials (6.6%)**. Payer remarks explained 12 of these through Summit’s whole-hour rule, which the drafter applies. The replay cannot assess the cause of the remaining **61 unexplained D99 denials (5.6% of all historical denials)**.
- **Incomplete historical coverage.** **1,361 drafts (27.3%)** had no match in claims history. Of those, 470 matched a pending claim and **891 (17.9% of all drafts)** matched neither supplied claims file. A separate billing pathway outside Exchange4Health is one possible explanation. The data doesn’t establish why those visits lacked a matching claim. For a live rollout, Exchange4Health would need a complete view of claims, including any submitted through a separate pathway, to reconcile visits and avoid duplicate billing.
