# Claim checker

The checker evaluates existing claims against verified visits, authorizations, submission history, and payer rules. Each applicable check runs, and the payer’s priority determines the predicted denial code.

The [structured output](#structured-output) supplies the evidence used by the app.

For the runnable historical evaluation, see the [testing guide](../testing/README.md).

## Assumptions

- **Summit whole-hour billing.** From 1 July 2026, Summit claims must bill in whole-hour increments of four 15-minute units. Drafting rounds verified billable time down to the nearest whole hour. For example, 1 hour 45 minutes supports 4 units. We inferred the requirement from historical denial remarks citing **Contract Amendment 3** and inferred the July start from the change in recorded outcomes.
- **Harbor filing.** We apply the in-state HCBS filing exemption to every Harbor claim in the sample because the export has no provider-state field.
- **Same-day submission order.** When submission dates tie within a CSV, earlier rows are treated as earlier submissions.
- **Pending authorization usage.** Earlier pending claims predicted to be paid provisionally reduce the remaining units for later claims.

#### 1. Payer and submission checks

```mermaid
flowchart TD
S(["Start"]) --> P{"Known payer?"}
    P -->|No| U(["Needs review: unknown payer"])
    P -->|Yes| K{"Is this a corrected claim (frequency_code = 7)?"}
    K -->|Yes| O{"Was the referenced original claim submitted earlier?"}
    O -->|No| OR["Record review: original claim unavailable"]
    O -->|Yes| V
    K -->|No| N{"Is this a new claim (frequency_code = 1)?"}
    N -->|No| NR["Record review: unknown submission type"]
    N -->|Yes| D{"Earlier claim for the same payer, patient, service and date?"}
    D -->|Yes| D5["Record D05: duplicate"]
    D -->|No| V
    OR --> V
    NR --> V
    D5 --> V
    V(["Continue to visit evidence"])
```

#### 2a. Visit evidence

```mermaid
flowchart TD
S(["From submission checks"]) --> M["Find visit records for this patient, service and date"]
    M --> V{"Is any matching visit verified?"}
    V -->|No| D1["Record D01"]
    D1 --> N{"Does a matching visit exist?"}
    N -->|No| Z["Record detail: no matching visit"]
    N -->|Yes| Q["Record matching visit status (rejected or pending manual verification)"] --> Next
    Z --> Next
    V -->|Yes| Units(["Continue to billed units in 2b"])
    Next(["Continue to authorization checks in 3"])
```

#### 2b. Billed units

```mermaid
flowchart TD
    S(["From verified visit evidence"]) --> M["Add verified visit minutes"]
    M --> F["Full units = verified minutes divided by 15, rounded down. Record the remaining minutes."]
    F --> N{"Is the payer Northwind?"}
    N -->|No| HS["Harbor or Summit: supported units = full units"]
    N -->|Yes| R{"Is the remainder at least 8 minutes?"}
    R -->|Yes| P["Supported units = full units plus 1"]
    R -->|No| K["Supported units = full units"]
    HS --> U
    P --> U
    K --> U
    U{"Do billed units exceed what verified time supports?"}
    U -->|No| Next
    U -->|Yes| D9["Record D09"]
    D9 --> X["Record the excess units, equivalent billable minutes and the gap from raw verified time"]
    X --> B{"Is billed time less than 30 minutes above verified time?"}
    B -->|Yes| Y["Suggest correcting billed units to the payer-supported amount"] --> Next
    B -->|No| L["Flag for the agency to reconcile billed units with the visit"] --> Next
    Next(["Continue to authorization checks"])
```

Summit’s inferred whole-hour requirement is checked separately under its payer-specific rules.

#### 3. Authorization and balance

```mermaid
flowchart TD
Start(["From visit evidence or billed units"]) --> A
    A{"Is an authorization ID supplied?"}
    A -->|No| D7["Record D07: missing authorization ID"]
    A -->|Yes| E{"Does that ID exist in the authorization records?"}
    E -->|No| D8["Record D08: invalid authorization"]
    E -->|Yes| M{"Same payer, patient and service, and does it cover the date?"}
    M -->|No| D8
    M -->|Yes| B{"Do billed units exceed the remaining authorization?"}
    B -->|Yes| D2["Record D02: insufficient balance"]
    B -->|No| BS{"Would approval of earlier pending submissions against this authorization make this claim exceed the balance?"}
    BS -->|Yes| BR["Record review: pending authorization usage"]
    BS -->|No| Next
    D7 --> Next
    D8 --> Next
    D2 --> Next
    BR --> Next
    Next(["Continue to the payer branch"])
```

#### 4a. Select the payer and check Northwind or Summit

```mermaid
flowchart TD
S(["From authorization checks"]) --> H{"Is the payer Harbor?"}
    H -->|Yes| HH(["Continue to Harbor checks in 4b"])
    H -->|No| N{"Is the payer Northwind?"}
    N -->|Yes| ND{"Care date on or after 1 August 2026?"}
    ND -->|Yes| N45["Use 45-day filing limit"]
    ND -->|No| N30["Use 30-day filing limit"]
    N45 --> NL
    N30 --> NL
    NL{"Filed after the selected limit?"}
    NL -->|Yes| N3["Record D03"] --> OUT
    NL -->|No| OUT
    N -->|No| SU{"Is the payer Summit?"}
    SU -->|No| UR(["Needs review: unknown payer"])
    SU -->|Yes| SL{"Filed more than 60 days after care?"}
    SL -->|Yes| S3["Record D03"] --> SD
    SL -->|No| SD
    SD{"Care date on or after 1 July 2026?"}
    SD -->|No| OUT
    SD -->|Yes| SW{"Does the bill include a partial hour?"}
    SW -->|Yes| D10["Record D10 under the inferred Summit amendment"] --> OUT
    SW -->|No| OUT
    OUT(["Claim output"])
```

#### 4b. Harbor checks

```mermaid
flowchart TD
S(["Harbor branch"]) --> F["Apply in-state HCBS filing exemption in the prototype"]
    F --> PC{"Is this a personal-care claim?"}
    PC -->|No| C
    PC -->|Yes| D{"Was the visit on or after 1 September 2026?"}
    D -->|No| U2{"Is the modifier U2?"}
    D -->|Yes| W{"Was the care date Saturday or Sunday?"}
    W -->|Yes| U3{"Is the modifier U3?"}
    W -->|No| U2
    U2 -->|Yes| C
    U3 -->|Yes| C
    U2 -->|No| D4["Record D04"]
    U3 -->|No| D4
    D4 --> C
    C{"Would this claim take the patient above 32 units for the date of care?"}
    C -->|Yes| D6["Record D06"] --> OUT
    C -->|No| OUT
    OUT(["Claim output"])
```

#### 5. Produce the claim output

```mermaid
flowchart TD
S(["From the payer checks"]) --> F["Sort the complete set of failed codes by configured denial priority"]
    F --> A["Write all codes to failed_checks and keyed evidence to finding_details"]
    A --> Q{"Any failed codes?"}
    Q -->|Yes| P["Write the highest-priority code to predicted_denial_code"]
    Q -->|No| B["Leave predicted_denial_code blank"]
    P --> R(["Needs review"])
    B --> Q2{"Any review-only findings?"}
    Q2 -->|Yes| R
    Q2 -->|No| O(["Ready to submit"])
```

## Structured output

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
