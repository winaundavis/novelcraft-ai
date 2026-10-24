# Experience log

Use one compact entry per important completed task. Do not paste full conversations.

Template:

```text
Task:
Outcome: Success | Partial | Failure
Problem / Success pattern:
Verification:
Lesson:
Reusable:
Memory updated:
Skill improvement candidate:
```

## 2026-08-19 Harness initialization retrospective

Task: Extract reusable Codex workflow from the NovelCraft collaboration history.
Outcome: Partial
Problem / Success pattern: Repeated reports showed that test counts, temporary model overrides, synthetic memory data, and same-agent reviews can overstate product readiness.
Verification: Project structure, Git scope, rule coverage, and secret-pattern scan passed. Business tests were not rerun because no business code changed.
Lesson: Match the evidence level to the claim and keep DeepSeek implementation work separate from Codex acceptance.
Reusable: Evidence rubric, default-versus-override distinction, phase-gated live testing, and test-strength review.
Memory updated: evidence-rubric.md, candidate-improvements.md, evaluator.md.
Skill improvement candidate: Review C1-C4 after repeated future tasks; do not promote automatically.

## 2026-09-04 Beta outline request failure

Task: Diagnose repeated "model response invalid" failures while editing an outline.
Outcome: Success
Problem / Success pattern: Provider/network/config were healthy. The project budget ledger held 100,308 reserved tokens while provider audits supported 48,448 actual tokens, so the request was rejected before HTTP dispatch and a broad `LLMResponseError` handler mislabeled it.
Verification: Red-green error taxonomy and settlement tests; focused suites 18/18, 45/45, and 40/40; full backend 819/819; health endpoint returned `status=ok`; no live model call was made.
Lesson: Preserve failure-layer semantics and settle conservative token reservations to actual provider usage when usage is available.
Reusable: Typed budget/timeout/transport/response errors, one real-call audit row, and project/date budget settlement without changing call-count semantics.
Memory updated: recurring-problems.md and experience-log.md.
Skill improvement candidate: None; the regression tests enforce the workflow directly.
