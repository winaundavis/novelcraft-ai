# Recurring problems

## FACT

- Model-specific timeout and output behavior can make a test pass under one provider and fail under another.
- Generated reports and build caches can be mistaken for source changes or accidentally committed.
- Project token budgets that reserve `estimated_input + max_tokens` without settling provider usage can block requests at roughly half the configured daily limit. A pre-provider budget rejection must have its own error type; otherwise the chat layer can misreport it as an invalid model response.
- A scoped outline alone does not scope a prose request: synopsis, latest global state, hooks, character-voice worldview and rejected source prose can reintroduce later events. Regression checks must inspect the final provider input for both formal generation and Copilot, not only the context helper. Evidence: `tests/test_arc_scope_regression.py` and `docs/outline-prose-context-fix-20260913.md`.
- Truncating every outline summary does not bound the total request: arc IDs, titles and repeated schema keys can exceed the cap by themselves. Test many-arc outlines and repeated candidate application; bound the final serialization and retain server-side patch merging. Evidence: `tests/test_outline_budget_regression.py`.
- A prose correction phrased as “调整正文” can bypass a “重新生成”-only guard. Replay the actual saved action/instruction/selection at the final provider boundary; separate plot authority from replacement scope and keep wording-only edits intact. HTTP 200 with identical original/rewritten text must not become a successful edit candidate. Evidence: `tests/test_prose_correction_flow.py` and `docs/prose-adjustment-arc-fix-20260913.md`.

## OBSERVATION

- Copilot acceptance must update the visible editor state and its current-content ref, not only the shared store. Regenerating an older conversation turn must replay its instruction against current saved prose, not replay the old body snapshot. Regression coverage: `frontend/tests/e2e/copilot-accept.spec.ts` and `backend/tests/test_prose_correction_flow.py`.

- A test modified to avoid an unstable state is weaker evidence until the underlying state transition is tested directly.
