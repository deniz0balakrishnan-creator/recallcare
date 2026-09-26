Real trace of eval scenario `G01_en_books_tuesday_morning` (mock mode) — patient wrote: *"Hi, can I come in next Tuesday morning?"*

| Agent | Action | Outcome | Model / tokens | Rationale |
|------|---------|-----|---------|----------------|
| supervisor | `route → guard` | ok |  | every inbound message is screened by the guard first |
| guard | `prescreen` | ok |  | deterministic hits: none |
| guard | `verdict` | ok |  | verdict **safe → allow** (source rules, urgency routine) |
| supervisor | `route → conversation` | ok |  | inbound_message → conversation |
| conversation | `fast_path` | ok |  | unambiguous short reply handled without a model call |
| supervisor | `route → scheduling` | ok |  | conversation handed off a scheduling request |
| scheduling | `tool:book_slot` | ok |  | patient chose option 1 |
| supervisor | `route → conversation` | ok |  | render scheduling result for the patient |
| conversation | `send_message` | ok |  |  |
| conversation | `tool:send_message` | ok |  |  |
| supervisor | `route → end` | ok |  | inbound_message → end |
