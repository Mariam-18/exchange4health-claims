# Core logic changes

Changes to how the checker or drafter decides an outcome. Style, refactoring and compatibility changes are not listed.

| Date | Change | Why | Effect |
|---|---|---|---|
| 2026-11-24 | Pending claims (`claims_to_check.csv`) are now recorded as submissions once their submission date has passed in the replay. Pending claims the checker forecasts as paid also count toward the Harbor daily total, alongside the existing projected authorization usage. | Previously a pending claim could not be caught as a duplicate of an earlier pending claim, and pending claims for the same Harbor patient and day were not added together. Among pending claims, 8 new-claim keys appear more than once and 21 Harbor patient-days exceed 32 units. | Historical replay unchanged: 3,768 of 3,830 (98.38%). Pending predictions: D05 22 → 29, D06 12 → 18, denied 157 → 169 of 546. |
