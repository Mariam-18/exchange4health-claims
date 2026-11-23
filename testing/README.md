# Historical testing

From the repository root, run this with Python 3.10 or later:

```bash
python -m testing.checker_tester /path/to/data/claims_history.csv
```

Keep `authorizations.csv`, `evv_visits.csv`, and `claims_to_check.csv` alongside the history file, or point `--data-dir` to their directory. The tester calls the checker and scores each historical claim against the recorded outcome. Pending claims contribute to the replay’s projected authorization usage. Only historical claims contribute to the table.

## Baseline run

The synthetic history contains 3,862 rows representing 3,830 unique claims. The run uses the checker’s inferred payer-specific denial priorities and existing submission-date batching.

| Recorded outcome | Predicted paid | Predicted denied |
|---|---:|---:|
| Paid | 2,730 | 1 |
| Denied | 61 | 1,038 |

The checker matched the recorded paid or denied outcome on **3,768 of 3,830 claims (98.38%)**. Among the **1,038** claims both recorded and predicted as denied, **1,026 (98.84%)** also matched the recorded denial code. This is a retrospective result on the synthetic data used to develop the rules.

For claim-level results, run `python -m checker.claim_checker /path/to/data --audit /path/to/history_audit.csv`.

## Draft evaluation

`draft_tester.py` is reserved for the separate draft evaluation. The existing drafter experiment can be run with:

```bash
python -m drafter.claim_drafter /path/to/data --output-dir /path/to/draft_results
```

The experiment checks generated drafts against the same rules engine and compares their service keys with the historical claims. Its ready and needs-review counts describe pre-submission checks.
