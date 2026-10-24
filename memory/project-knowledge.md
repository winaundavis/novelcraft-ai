# Project knowledge

## FACT

- Frontend: React/Vite/TypeScript under `frontend/`.
- Backend: FastAPI/Python under `backend/`.
- Persistence: SQLite backend plus IndexedDB-backed frontend state.
- Core tests include backend pytest and frontend Playwright E2E.
- DeepSeek agent collaboration is a separate project mechanism and must not be folded into this Codex harness.

## OBSERVATION

- Long-running generation, memory recall, and live-model evidence require stronger proof than typecheck or deterministic unit tests.

## HYPOTHESIS

- The smallest useful Codex harness is one project rule file, the existing global analysis/verification workflows, one project-specific postmortem workflow, and compact classified memory rather than a second agent platform.
