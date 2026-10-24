# Evidence rubric

Use the highest level required by the claim. A lower level may support a narrower claim, not a stronger one.

| Level | Evidence | Supports | Does not prove |
|---|---|---|---|
| E0 | Report or model claim | A lead to investigate | That the result is true |
| E1 | Static inspection/typecheck/build | Syntax, types, packaging | Runtime behavior |
| E2 | Deterministic unit test or mock | A bounded code path | Provider behavior or production quality |
| E3 | API/browser integration test | A real local workflow | Long-run stability or prose quality |
| E4 | Isolated live-model run with provider, model, source, fallback, and job ID | One real model workflow | Million-word quality |
| E5 | Long-run plus restart/recovery and independent review | The stated long-run acceptance scope | Unmeasured genres or future model changes |

Required discipline:

- Never upgrade an E0-E2 result into an E4/E5 claim.
- Separate default configuration results from temporary test overrides.
- Record failures and fallback counts, not only successful output.
