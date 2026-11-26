# Claim drafter

The [claim drafter](claim_drafter.py) turns verified visits into drafts. It builds each draft from the visit and payer rules, then runs the [shared claim checker](../checker/README.md) to decide whether it is ready to submit or needs review.

## 1. Build the draft

```mermaid
flowchart TD
    A["Find the patient's visit and map the patient to an agency"] --> B["Group verified visits by patient, service and clock-in date"]
    B --> C{"Does exactly one authorization cover the patient, service and date?"}
    C -->|No| D["Show an authorization issue in Needs review"]
    C -->|Yes| E["Use the matching payer and authorization ID"]
    E --> F["Calculate billed units from verified minutes using payer rounding"]
    F --> G{"Has a claim already been submitted for this visit and payer?"}
    G -->|Yes| I["Warn and skip the draft"]
    G -->|No| H["Set the modifier and create the draft"]
```

All 300 patients in `claims_history.csv` appeared under exactly one agency. The prototype uses that mapping because the EVV export has no `agency_id`. Finding the authorization gives the draft its payer and authorization ID. The checker evaluates the available units in the next stage.

## 2. Check the draft

The app checks the draft against recorded claims and earlier drafts. Previously paid claims reduce the available authorization balance.

```mermaid
flowchart TD
    A["Take the completed draft"] --> C["Check remaining authorization units"]
    C --> D["Check the filing deadline and applicable payer limits"]
    D --> E{"Any issues to resolve?"}
    E -->|No| F["Ready claims"]
    E -->|Yes| G["Needs review with findings and suggested actions"]
```

## Issues drafting prevents

- **Overbilling verified time (D09)** — calculates units from verified minutes using payer rules.
- **Wrong or missing Harbor modifier (D04)** — sets the required modifier.
- **Missing or invalid authorization ID (D07/D08)** — fills the ID when exactly one authorization covers the visit.
- **Partial-hour Summit billing (D10)** — applies the inferred whole-hour rule.

## No draft created

- **No verified visit** — drafting starts from verified EVV. With no matching visit record, there is nothing to draft. Pending and rejected visits are skipped.
- **Missing or ambiguous authorization** — there must be exactly one matching authorization to build the draft.
- **Duplicate submission (D05)** — the drafter skips a matching payer, patient, service, and date and shows the earlier claim ID.

## Draft created but held for review

The checker expects these drafts to be denied if submitted as written.

- **Insufficient authorization units (D02)** — the agency must verify the balance or obtain more units.
- **Harbor daily cap exceeded (D06)** — the agency must confirm any reduction.

See the [drafter tests and results](../testing/README.md#claim-drafter-testing).
