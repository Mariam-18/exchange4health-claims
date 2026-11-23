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

The prediction matched the recorded outcome for **3,768 of 3,830 claims (98.38%)**. The checker missed **61** recorded denials and flagged **one** claim that was paid. Among the **1,038** claims both recorded and predicted as denied, the predicted denial code matched in **1,026 cases (98.84%)**. These figures describe a replay of the synthetic history used to develop the checker.

To inspect individual predictions and failed checks, export the audit from the same data directory:

```bash
python -m checker.claim_checker /path/to/data --audit history_audit.csv
```
