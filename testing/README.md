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

This replay asks what would happen if the [drafter](../drafter/claim_drafter.py) had replaced the existing claim creation workflow from the start. It builds drafts from verified visits and checks them in order. Earlier drafts that pass count toward authorization usage and daily caps. Recorded claims provide the historical comparison.

### Results on the synthetic data

| Draft outcome before agency review | Drafts | Share |
|---|---:|---:|
| Ready to submit | 4,635 | 93.1% |
| Needs review | 345 | 6.9% |
| **Total** | **4,980** | **100%** |

Of the **345** drafts needing review, **231** had an authorization shortfall and **114** needed approval for a Harbor cap reduction.

**3,619 of the 4,980 drafts (72.7%)** matched a historical claim on payer, patient, service, and date of service. For those same services, **888 (24.5%)** had only denied claims in history, while **255 (7.0%)** of the generated drafts needed review before submission. A service with a paid original and a later denied duplicate counts as historically paid. Across the full history, **1,099 of 3,830 claims (28.7%)** were denied. Actual payer outcomes for the generated drafts are unavailable.

Run from the repository root:

```bash
python -m drafter.claim_drafter /path/to/data --output-dir /path/to/draft_results
```
