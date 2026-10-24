# Beta Permission Matrix

Updated: 2026-10-12.

| Resource | Read | Create | Update | Delete | LLM |
| --- | --- | --- | --- | --- | --- |
| Project | owner | authenticated user | owner | owner | - |
| Chapter | owner | owner | owner | owner | - |
| Story State / Memory / Style | owner | owner | owner | owner | - |
| Copilot / Rewrite | owner | owner | owner | owner | yes |
| Review | owner | - | - | - | yes |
| LLM Config | maintainer | maintainer | maintainer | maintainer | health only |

`maintainer` is a server-configured `ADMIN_USER_IDS` identity (or a user flagged in the database). A normal invite-only Beta user cannot alter shared provider credentials or models — and the "Model settings" navigation entry is not rendered at all for them (typing `/settings` shows an explicit admin-only notice). The front-end decision comes from `GET /api/auth/permissions`, which shares its predicate with `require_maintainer`.
