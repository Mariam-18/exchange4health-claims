# Claim investigation and suggested fixes

These observations come from the synthetic historical claims, verified visits and authorization records. The proposed changes below describe workflow improvements to discuss with agencies.

## D09 · Billed units exceed verified time

- **Observed:** For 55% of claims, the billed time was less than 15 minutes above verified time. For 73.8%, it was less than 30 minutes above verified time. Of the claims with a gap of less than 30 minutes, 89.6% matched rounding up to a 15- or 30-minute boundary. That is consistent with rounding as an explanation.
- **Proposed change:** Where the gap between billed time and verified EVV time is less than 30 minutes, recalculate the correct number of billed units based on the payer’s rules, automatically update the claim, and notify the claimant.
- **Larger gaps:** Gaps of more than 30 minutes should be confirmed with claimants. These gaps seem to cluster around an hour. 


## D06 · Harbor daily cap

- **Observed:** 96 of 106 flags exceeded the 32-unit cap by exactly four units. Of those 96, 93 matched a single EVV visit and none matched multiple visits.
- **Hypothesis:** A recorded nine-hour span could include a one-hour lunch break. 
- **Proposed change:** Add a non-billable break field to EVV and use it to appropriately bill claims.

## D07 and D08 · Authorization selection

- **D07:** All 84 flags for a missing ID had exactly one authorization matching payer, patient, service and service date. Filling it cleared all D07 flags. 
- **D08:** All 80 flags used an authorization for the correct payer, patient and service but the wrong date window. All 80 claims had exactly one matching replacement, and replacing the ID cleared D08 in every case.
- **Proposed change:** Suggest setting the correct ID automatically when there is one match. If there's many or none, flag for review. 

## Hypotheses tested

| Hypothesis | What we found |
|---|---|
| D01 missing visits were billed against the wrong date. | Of 103 cases with no matching visit, 13 had a candidate on another date with compatible units and no claim in history. Checking the pending file reduced this to eight, all from the final block of 900 EVV rows without a claim in either export. Only three were within a week. The schedule is needed before changing a service date. |
| A caregiver change around the service date explained missing visits. | The caregivers before and after the date differed in 95 of 96 missing-visit cases (99.0%) and in 2,383 of 2,400 paid cases (99.3%). A change was routine in both groups. |
| Agencies routinely prioritized newer claims, leaving older claims to go late. | Newer care was filed before the older claim’s deadline in 199 of 277 late cases. Across comparable claim pairs filed on different days, newer care went first only 47.6% of the time. The data does not establish when the older claims were ready for billing. |
