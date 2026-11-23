# Claim investigation and suggested fixes

These observations come from the synthetic historical claims, verified visits and authorization records. The proposed changes below describe workflow improvements to discuss with agencies.

## D09 · Billed units exceed verified time

- **Observed:** For 192 of 260 D09 flags, the billed time was less than 30 minutes above the verified visit duration. For 172 of those 192 (89.6%), rounding up to a 15- or 30-minute boundary reproduced the submitted units exactly. That is consistent with rounding as an explanation.
- **Proposed change:** Recalculate small discrepancies automatically from verified minutes using the payer’s rule. Show the change in the draft and rerun the remaining checks before release. Apply the under-30-minute cutoff to the gap from raw EVV time.
- **Larger gaps:** They tended to cluster near an hour. Among the 66 claims more than 30 minutes above verified time, 45 were within ten minutes of an hour. Some fit whole-hour rounding. For gaps of 30 minutes or more, ask the agency to reconcile the billed time with the visit and check for any additional service or time recorded elsewhere.

## D06 · Harbor daily cap

- **Observed:** 96 of 106 flags exceeded the 32-unit cap by exactly four units. Of those 96, 93 matched a single EVV visit and none matched multiple visits. In 84 cases, one verified visit supported all 36 units.
- **Hypothesis:** A recorded nine-hour span could include a one-hour non-billable break. Subtracting a documented hour would bring those 84 visits within 32 units. The EVV export has no break field, so the agency would need to check the shift or timesheet.
- **Proposed change:** Show the units available under the cap and let the agency confirm a reduction, then check the reduced claim again.

## D07 and D08 · Authorization selection

- **D07:** All 84 flags for a missing ID had exactly one authorization matching payer, patient, service and service date. Filling it cleared all D07 flags. Of those, 61 passed every check.
- **D08:** All 80 flags used an authorization for the correct payer, patient and service but the wrong date window. In 47 cases the authorization had yet to start. In 33 it had expired. All 80 had a unique replacement for the service date. Replacing it cleared D08 in every case, and 55 passed every check.
- **Proposed change:** Set the ID automatically when there is one match, then check available units. Send missing or ambiguous matches to review.

## Hypotheses tested

| Hypothesis | What we found |
|---|---|
| D01 missing visits were billed against the wrong date. | Of 103 cases with no matching visit, 13 had a candidate on another date with compatible units and no claim in history. Checking the pending file reduced this to eight, all from the final block of 900 EVV rows without a claim in either export. Only three were within a week. The schedule is needed before changing a service date. |
| A caregiver change around the service date explained missing visits. | The caregivers before and after the date differed in 95 of 96 missing-visit cases (99.0%) and in 2,383 of 2,400 paid cases (99.3%). A change was routine in both groups. |
| Agencies routinely prioritized newer claims, leaving older claims to go late. | Newer care was filed before the older claim’s deadline in 199 of 277 late cases. Across comparable claim pairs filed on different days, newer care went first only 47.6% of the time. The data does not establish when the older claims were ready for billing. |
