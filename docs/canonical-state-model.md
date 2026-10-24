# Canonical State Model

更新：2026-10-12。

## Single fact chain

```text
Chapter Contract + observed evidence + confirmed state
                         |
                         v
                   Chapter Commit
                         |
                         v
             Canonical Projection Runner
              /     /      |      \      \
     Story State  Memory  Digest  Index  Foreshadow
```

`chapter_commit_history` is the only canonical record of an accepted chapter.
For each `(project_id, chapter_id)`, exactly one row with `status=accepted` is
canonical. Its immutable content snapshot, hashes, contract version, and commit
evidence are the formal story facts for that chapter. Earlier versions are
`superseded`, not a competing source of truth.

The projection runner consumes that accepted commit. Story State must use the
commit evidence (`observed_facts`, state/relationship/knowledge/conflict/
foreshadow changes, questions, and next direction) rather than perform a new
free-form interpretation of prose. The Chapter Contract provides planned
context; it does not replace observed commit evidence.

## Projections

`story_states`, `novel_facts` (Memory), `chapter_digests`, FTS/Index records,
and foreshadow projection status are rebuildable derived state. Each canonical
projection status is bound to `memory_projections.commit_id`. A replay selects
one canonical commit per chapter in narrative order and never needs a model.

Digest is a projection, not a fact source. It is keyed to its source commit and
each important field records `source_type`, `source_id`, `chapter_id`, and
`evidence_level`. Digest may be regenerated from the commit; editing it must
not alter canonical story facts.

## Legacy compatibility

The legacy `chapter_summary`, `facts`, `entities`, `volume`, and `fts` names,
tables, and APIs are compatibility adapters only. New code uses the canonical
projection vocabulary: `summary`, `memory`, and `index` (plus Story State and
Foreshadow). Retry and replay map legacy input names to the canonical runner;
they do not invoke the old LLM-backed extraction APIs. Doctor ignores legacy
projection rows when assessing canonical health. UI that still shows a legacy
record must label it as a legacy-derived view rather than canonical truth.
