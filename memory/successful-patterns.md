# Successful patterns

## FACT

- Validate the actual provider/model/source for live LLM tests.
- Use unique temporary projects for write-heavy integration and long-run tests.
- Treat synthetic memory tests as capacity and retrieval smoke tests, not as proof of real novel-scale quality.

## OBSERVATION

- Exact job IDs, database state, and post-restart checks provide stronger evidence than aggregate test counts.
