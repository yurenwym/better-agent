# M1 Live Acceptance Budget 005

Status: approved and passed. Three consecutive rounds completed; this batch is closed.

Date: 2026-09-08 Asia/Shanghai. Batch: `M1-LIVE-20260908-005`.

## Prior usage and cumulative ceiling

- `-001` made no external calls. `-002`, `-003`, and `-004` stopped on hard failures and their successful rounds are not combined.
- Prior conservative upper bound: 17 DeepSeek attempts, 9 embedding requests, 1,584 embedding input tokens, US$0.428944 and CNY 0.00022176.
- New ceiling: 21 DeepSeek attempts / US$0.53, 6 embedding requests / 12,288 input tokens / CNY 0.001721, maximum 25 minutes.
- Worst-case cumulative ceiling after this batch: US$0.958944 and CNY 0.00194276.

## Frozen inputs

- HEAD: `c7221cf34ae065ddb7d2de86b14e9dbb90889af4`.
- Tracked working-tree diff object: `28f409451754877851648dc75d36bf6d6e9322e0`.
- Production SHA-256: `costs.py` `024ce73af79d286e764fddfe819d115d0b16b443a912d98ddadaec975b3ebecd`; `live_model.py` `c10640c148ef517ac0af8dfb76522fc1fb20ebf633d01b7d3abdd614e26fba4c`; `memory_archive.py` `aa47618ce5fe8d76c938240c58b933fdce3d055feb58e221ef3b65cb906ae0a2`; `memory_v2.py` `857b74803a1f06b9fddd6a67aca50b85c3f9aa749a5825cef65fd26000e6cddb`; `conversation.py` `9e51691df04da066af9a00813140316ef5de5e2eddc1aaacbc66c51724c9953d`.
- Harness SHA-256: `scripts/m1_live_acceptance.py` SHA-256 is `ef82cf94152f171450a42874237c6a11603ac3ad0c8a5da8d95e499b12cc7e77`; it is frozen after the final patch check; `tests/test_m1_live_acceptance_budget.py` `5428488afa7997c7743b688cd2b9fdc54749ffdae8786d53c353efe452f0998a`; `tests/integration/test_m1_live_harness.py` `a2c88c3eb22763e3639925441c6f460ca863aaec742f4e38ee754d0d2b057fe4`; `tests/test_memory_archive.py` `f480e78e33053779dd8113af5e60d5b7dfe8a4364c4a54e727ea3d1cc2848ab9`.
- Model: DeepSeek `deepseek-v4-flash`, context 32,768, max output 8,192, no fallback. Episode prompt `episode-v4`; memory renderer `memory-v5`; tokenizer `utf8-upper-bound-v1`.
- Embedding: SiliconFlow `Qwen/Qwen3-Embedding-4B`, 1,024 dimensions, acceptance timeout 60 seconds.

## Fixed cases and stop rules

- Three fresh isolated PostgreSQL databases, owners, and thread namespaces; six model invocations and at most two embedding requests per round.
- Require semantic cycling-memory recall, lexical `recovery_friday` recall, scope/expiry/deletion exclusion, SQLite correction precedence, Episode user attribution, idempotent archive, and incomplete-history gating.
- Any timeout, unsafe attribution, cross-scope recall, source loss, duplicate Episode, dependent request release, side effect, or unexplained usage/cost stops the batch immediately. Do not start application services, workers, research, multi-agent, evolution, Canary, or release workflows.
- Only after three consecutive fresh rounds and final offline regression pass may M1 be marked complete and M2 considered.

## Approved scope requested

- Maximum 21 DeepSeek attempts; model hard ceiling US$0.53.
- Maximum 6 SiliconFlow embedding requests; 12,288 UTF-8 input-token upper bound; hard ceiling CNY 0.001721.
- Maximum 25 minutes; isolated PostgreSQL only; no fallback model and no other workflow.
- Previous cumulative conservative upper bound is US$0.428944 / CNY 0.00022176; approved use of this batch could raise the cumulative worst-case ceiling to US$0.958944 / CNY 0.00194276.

Explicit user approval is required before execution.


