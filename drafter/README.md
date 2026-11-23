# Claim drafter

The claim drafter turns verified visits into draft claims. It assigns the payer and authorization, calculates billable units, and sends each draft through the [claim checker](../checker/README.md). The flowcharts describe the proposed agency workflow. [The Python prototype](claim_drafter.py) simulates drafting and review decisions on the synthetic data.

## 1. Find the visit, agency and authorization

```mermaid
flowchart TD
    A["Find visits for the patient, service and date"] --> B{"Any verified visits?"}
    B -->|No| C["Visit needs review"]
    B -->|Yes| D["Group verified visits by patient, service and clock-in date"]
    D --> E["Look up the agency from the assignment record"]
    E --> F{"Exactly one agency assignment?"}
    F -->|No| G["Send to data review to resolve the assignment"]
    F -->|Yes| H{"Exactly one authorization covers the patient, service and date?"}
    H -->|No| I["Ask the agency to resolve the authorization"]
    H -->|Yes| J["Use the matched payer and authorization"]
```

All 300 patients in `claims_history.csv` appeared under exactly one agency. The prototype uses that patient-to-agency lookup because the EVV export has no `agency_id`. In a live system, the agency can come from the visit’s scheduling record or the agency account that supplied it.

## 2. Build the draft

```mermaid
flowchart TD
    A["Sum verified visit minutes"] --> B["Divide by 15 and round down to get full units"]
    B --> C{"Northwind with at least 8 minutes left over?"}
    C -->|Yes| D["Add one unit"]
    C -->|No| E["Use full units"]
    D --> F
    E --> F
    F{"Does Summit's whole-hour rule apply?"}
    F -->|Yes| G["Round units down to a multiple of four"]
    F -->|No| H
    G --> H
    H{"At least one billable unit?"}
    H -->|No| I["Record as no billable units"]
    H -->|Yes| J["Set the required modifier and calculate the billed amount"]
    J --> K["Create a new claim draft and retain its visit IDs"]
```

For Harbor personal care, set `U3` for weekends from 1 September 2026 and `U2` otherwise. The Summit branch uses the whole-hour requirement inferred for the prototype.

## 3. Check, review and submit

```mermaid
flowchart TD
    A["Run the claim checker on the draft"] --> B{"Any failed checks or review findings?"}
    B -->|Yes| C["Needs review: show findings and suggested actions"]
    C --> D["Agency resolves the issue"]
    D --> A
    B -->|No| E["Ready to submit"]
    E --> F["Agency approves the draft"]
    F --> G["Recheck with the latest usage and submission date"]
    G --> H{"Still clear?"}
    H -->|Yes| I["Submit to payer"]
    H -->|No| C
```

For an authorization shortfall, show requested units, available units and the difference. Keep the draft in review while the agency resolves the balance.

## 4. Review a Harbor cap reduction

```mermaid
flowchart TD
    A["Checker flags D06"] --> B{"Any units left under the daily cap?"}
    B -->|No| C["Keep in Needs review and show counted units"]
    B -->|Yes| D["Show the proposed reduction and revised amount"]
    D --> E{"Agency confirms the reduction?"}
    E -->|No| C
    E -->|Yes| F["Apply the reduction and rerun the checker"]
```
