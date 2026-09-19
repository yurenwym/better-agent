# M1-LIVE-20260908-005 Result

Status: PASSED. Three fresh isolated PostgreSQL rounds passed consecutively.

- 18 DeepSeek model attempts, 6 SiliconFlow embedding requests, 1,161 UTF-8 upper-bound embedding input tokens.
- Conservative model ledger charge: 454,176 microusd (US$0.454176), below the US$0.53 hard ceiling.
- Elapsed time: 103.647551 seconds, below 25 minutes.
- Every round used hybrid retrieval, renderer `memory-v5`, tokenizer `utf8-upper-bound-v1`, and Episode prompt `episode-v4`.
- Episode evidence preserved the user-confirmed 60-minute decision; the assistant 90-minute suggestion remained unconfirmed and was not stored as a decision/outcome.
- The independent incomplete-history question completed with the notice; the dependent save request failed and produced no ask, plan, research, agent-run, or memory side effect.
- All round databases were isolated; the application service and unrelated workflows were not started. The live compose project was stopped after completion and its evidence remains available.
- Full report: `m1-live-results-2026-09-08-005.json`.
