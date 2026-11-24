# Claim checker interface contract

Use the existing checker for predictions and evidence. Render the evidence supplied by Python and use the action keys to select interface text and controls.

## Inputs and outputs

```python
from pathlib import Path
from checker.claim_checker import predict_pending_assessments

claims = predict_pending_assessments(Path("/path/to/data"))
```

For a CLI integration, run from the repository root:

```bash
python -m checker.claim_checker /path/to/data \
  --audit history_audit.csv --predictions predictions.csv --details findings.json
```

The JSON array follows `claims_to_check.csv` row order. Each item has `claim_id`, `payer`, `service_code`, `service_date`, `submission_date`, `status`, `predicted_denial_code`, `failed_checks`, `finding_details`, and `review_reasons`. Dates are ISO strings. Every failed code has one object with `evidence`, a `rule_reference` string, and a `suggested_action` key.

- Use `status` for the queue. Values are `ready_to_submit` and `needs_review`. A review-only item has an empty `failed_checks` list and a null `predicted_denial_code`.
- Render all findings in `failed_checks` order. The first code is the predicted denial code. The assessment CSV has separate `paid` and `denied` predictions, with only `claim_id,status,denial_code`.
- Use top-level service and submission dates for the current claim. Dates inside evidence refer to deadlines, prior submissions, or authorization coverage.

## Evidence keys by denial code

| Code | Evidence | Action keys |
|---|---|---|
| D01 | `matching_visits`: list of `visit_id` and `verification_status`. Empty means no matching visit. | `locate_missing_visit`, `review_rejected_visit`, `resolve_pending_verification`, `review_visit_statuses` |
| D02 | `auth_id`, `authorized_units`, `recorded_paid_units`, `projected_paid_units`, `remaining_units`, `requested_units`, `shortfall_units`. Show the confirmed and projected usage separately. Shortfall is the number of units requested beyond the nonnegative available balance. | `review_authorization_balance` |
| D03 | `filing_limit_days`, `filing_deadline`, `days_late`. | `review_late_filing_options` |
| D04 | `submitted_modifier`, `expected_modifier`. An empty submitted value means missing. | `apply_expected_modifier` |
| D05 | `frequency_code`, `matching_submissions`: list of `claim_id`, `submission_date`, and nullable `recorded_status`. A null outcome identifies an earlier pending submission in the replay. | `review_prior_submission` |
| D06 | `daily_cap_units`, `already_counted_units`, `billed_units`, `excess_units`, `available_units`, `proposed_units`. Proposed units are null when none are available. | `confirm_cap_reduction`, `review_daily_cap` |
| D07 | `candidate_auth_ids`. | `apply_matching_authorization`, `resolve_authorization_match` |
| D08 | `submitted_auth_id`, `mismatches`, `authorization_start_date`, `authorization_end_date`, `coverage_date_gap_days`, `candidate_auth_ids`, `unique_candidate_coverage`. | `replace_authorization`, `resolve_authorization_match` |
| D09 | `verified_minutes`, `billed_units`, `supported_units`, `excess_units`, `excess_billable_minutes`, `gap_vs_evv_minutes`. The first excess in minutes uses payer-supported units. The raw gap uses verified EVV time. | `check_rounding` for a raw gap below 30 minutes, `reconcile_visit_and_billed_units` otherwise |
| D10 | `billed_units`, `required_unit_multiple`, `remainder_units`, `verified_minutes`, `evv_units_exact`, `evv_supported_units`. The last three are null when no verified visit exists. Exact units are the EVV duration divided by 15 minutes, before rounding. | `review_whole_hour_units` |

For D08, `coverage_date_gap_days` is negative before the submitted authorization starts, positive after it ends, zero when the date is covered, and null when the ID is unknown. `unique_candidate_coverage` contains `start_date` and `end_date` only when exactly one candidate matches. D99 remains a historical payer outcome.

After an agency confirms a proposed adjustment or changes a claim, rerun the checker. The existing pending-submission and projected-usage rules still determine the result. Use optional AI only to explain the evidence and action already returned by Python.
