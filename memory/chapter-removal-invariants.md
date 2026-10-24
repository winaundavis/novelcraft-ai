# Verified chapter removal invariants

Validated by `backend/tests/test_chapter_trash.py` and `test_chapter_trash_memory.py`, 2026-09-12.

- `replay_project()` is additive. Replaying surviving commits does not itself retract a removed chapter's facts or cumulative state.
- Recycle the snapshot and invalidate source-derived records in the same SQLite transaction as chapter removal. Preserve author fields and shared contributions from surviving chapters.
- Never replay recycled, superseded or content-mismatched commits. Restoration creates a new canonical commit at the chosen current-outline location.
- Validate both outline and target-content hashes before replacement; archive displaced content before inserting restored text.
- Editor dirty state must be checked from the mounted editor, including edits made while a save response is pending. Same-ID restore requires explicit content refresh; component unmounts must not let late responses switch away from new unsaved input.
- Legacy state without a before snapshot cannot support exact semantic rollback of untracked author changes. Preserve that limitation explicitly rather than resetting all Story State.
