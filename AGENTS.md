# NovelCraft AI project rules

## Project

- React/Vite frontend is under `frontend/`; FastAPI backend is under `backend/`.
- SQLite data, `.env`, uploads, logs, browser profiles, and test artifacts are local state.
- DeepSeek's agent collaboration files, when present under `.github/`, belong to that system. Do not modify, duplicate, or replace them.
- `docs/` contains product and audit evidence. Treat claims there as evidence to verify, not as proof by themselves.
- Do not expand scope for unrelated cleanup, refactoring, dependency upgrades, or architecture changes.
- Server-wide model config (`/api/settings/*`) is admin-only. The UI hides the entry from non-admins and
  the API returns 403 regardless of the UI. The front-end decision comes from `GET /api/auth/permissions`,
  which shares its predicate with `require_maintainer` — keep those two in sync.

## Workflow

1. Inspect Git status, relevant docs, existing tests, and the affected modules before editing.
2. Check for a false premise, missing information, or scope expansion before acting.
3. Keep changes scoped to the requested behavior. Report unrelated findings without fixing them.
4. Use the smallest relevant verification first, then run the broader check required by the change.
5. Separate facts, observations, and hypotheses in reports.
6. Do not claim a live-model, browser, or long-run result without recording the exact command and evidence.

## Verification commands

- Backend focused/full: `cd backend; python -m pytest <test> -q` / `python -m pytest tests -q`
  (add `--basetemp=<非 C 盘目录>`: SQLite reports `database or disk is full` when C: runs low)
- Frontend static/build: `cd frontend; npm run typecheck` / `npm run build`
- Frontend unit tests (Node built-in runner, no jsdom/testing-library): `cd frontend; npm run test:unit`
  (`npm run test:normalize` is only the normalize subset of it)
- Browser E2E: `cd frontend; npm run test:e2e`
  **Restart the dev server first.** After editing `src/` (especially `stores/`, `App.tsx`), HMR makes
  `page.evaluate(import('/src/...'))` resolve to a second module instance and yields false failures.
  A guard placed in a file that is skipped wholesale (`novelcraft-smoke.spec.ts`) never runs —
  `EXIT=0` can mean `skipped`; always read the passed/skipped/failed counts.
- Live LLM tests require explicit cost approval and evidence for provider, model, source, fallback count, and job ID.

## Constraints

- Never expose, rotate, or copy API keys unless explicitly requested.
- Do not delete databases, backups, uploads, reports, or Git history without explicit confirmation.
- Do not commit local databases, credentials, browser state, generated reports, or build caches.
- Preserve existing tests and `data-testid` hooks unless the change explicitly requires an update.
- Do not treat synthetic memory tests as proof of real long-context recall or prose quality.

## Completion

A task is complete only when the requested behavior is implemented, the minimum relevant verification passes, remaining risks are stated, and no unrelated work was silently included.

## Codex harness

- Use existing global task-analysis and verification skills rather than duplicating them locally.
- The project-specific postmortem workflow lives in `skills/postmortem/`; durable project knowledge lives in `memory/`.
- Suggestions for changing this harness are candidates only. Adopt them only after repeated evidence and a reversible comparison.
