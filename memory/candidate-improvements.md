# Candidate improvements

Candidates are proposals, not active permanent rules. Adopt only after a repeatable comparison.

## C1: Independent acceptance gate

- Observation: implementation reports can be green while the evidence scope is narrower than the product claim.
- Proposed change: require an independent review of claim, configuration, test path, and evidence level before acceptance.
- Test: compare two future deliveries using the rubric; accept only if unsupported claims are detected consistently.
- Status: candidate

## C2: Default-versus-override reporting

- Observation: a live E2E run can pass under a temporary provider or role override while the user's default configuration remains unverified.
- Proposed change: every live report must state whether it used default configuration or an override.
- Test: run the same bounded scenario in both modes and compare provider/model/source/fallback evidence.
- Status: candidate

## C3: Phase-gated expensive testing

- Observation: long LLM runs cost time and money and should follow local, integration, and short live checks.
- Proposed change: block long-run testing until lower-cost gates pass, unless explicitly overridden.
- Test: verify the checklist prevents an accidental long run and records the override when used.
- Status: candidate

## C4: Test-strength review

- Observation: deleting a reload or state-transition assertion can make a test green while reducing coverage.
- Proposed change: any weakened assertion must record the lost coverage and add an equivalent backend or integration check.
- Test: review future changed tests for preserved behavior, not only pass count.
- Status: candidate
