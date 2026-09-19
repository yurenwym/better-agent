# M1-LIVE-20260908-004 Result

Status: stopped on the first hard failure; do not rerun.

- Round 1 failed the Episode safety gate because the live model placed an assistant-only "90 minutes per day" suggestion in `decisions`.
- External usage: 3 DeepSeek model attempts, 2 SiliconFlow embedding requests, 387 UTF-8 upper-bound input tokens.
- Conservative batch upper bound: US$0.075696 and CNY 0.00005418.
- Cumulative M1 conservative upper bound through this batch: US$0.428944 and CNY 0.00022176.
- Report: `m1-live-results-2026-09-08-004.json`.
- The production fix requires every `decisions`/`outcomes` item to cite at least one `role='user'` source message. Assistant-only suggestions are rejected to the dead-letter path; source text and archive cursor remain unchanged.
- The next batch is `M1-LIVE-20260908-005`; prior successful rounds are not reused.
