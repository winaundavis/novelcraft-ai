# Harness evaluator

For each important task, rate only the work process:

| Dimension | Good | Acceptable | Poor |
|---|---|---|---|
| Correctness | Result matches the requested behavior | Minor gap stated | Unsupported or wrong result |
| Completeness | Relevant paths and risks covered | Known gaps remain | Important path ignored |
| Efficiency | Minimal useful work | Some avoidable work | Repeated or broad work without value |
| Reliability | Evidence is reproducible and scoped | Partial evidence is explicit | Claims exceed evidence |
| Scope | No unrelated changes | Small justified adjacent change | Uncontrolled expansion |
| Verification | Appropriate checks pass | Blocker is reported | No meaningful verification |

Outcome: `Success`, `Partial`, or `Failure`.

Only repeated, verified patterns may update Memory or become a Skill candidate.
