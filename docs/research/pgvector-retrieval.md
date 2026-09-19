# Better Agent: PostgreSQL + pgvector memory retrieval research

Status: proposed
Scope: long-term `memory_entries` / `memory_revisions` retrieval only

> Final decision override (2026-09-05): HNSW is enabled in the first implementation. Exact scoped vector search remains the fallback and recall-quality oracle. The authoritative implementation decision is recorded in `docs/superpowers/plans/2026-09-05-postgresql-pgvector-migration-plan.md`.
Decision goal: make PostgreSQL the only authoritative database while preserving the public contract of `MemoryContextProvider.select()` and minimizing retrieval-specific code changes.

## Executive recommendation

Use a semantic-first cascade, not unconditional hybrid retrieval:

1. Resolve the canonical owner/thread/project scope exactly as today and reuse an existing Context Pin before making any embedding call.
2. Always include eligible pinned memories from the same owner and allowed scope.
3. If the query is non-empty and query embedding succeeds, run a scope-filtered pgvector cosine search over current memory revisions.
4. Accept the semantic candidate set only when thresholds calibrated on a labeled Better-specific retrieval set are met.
5. Otherwise fall back to PostgreSQL full-text/substring retrieval. For queries containing exact-looking identifiers, supplement semantic results with lexical results even when semantic quality passes.
6. Preserve the existing importance ranking, token budgets, renderer, revision/episode IDs, and Context Pin behavior.

Do not vectorize episodes in the first release. Episodes are currently intentionally thread-only, have a separate token budget, and are expected to be a small set. Keep their SQL lookup and recency/query-term ranking unchanged. This leaves their current linear scoped read as an explicit non-goal, not a claim that it scales indefinitely. Add a cardinality/latency benchmark and revisit pagination or episode indexing if per-thread p95 or row count crosses an agreed threshold. Vectorizing them now expands the migration, indexing, lifecycle, and evaluation surface without fixing the common long-term-memory paraphrase failure.

Start with exact pgvector search. Add HNSW only after production measurements show that exact, tenant-filtered search violates its latency objective. This avoids filtered approximate-nearest-neighbor recall loss while the memory corpus is small.

## Current implementation and problem

`MemoryContextProvider.select()` currently:

- resolves the thread's canonical `owner_id` and `project_id`, rejects a requested-project mismatch, and checks a Context Pin before retrieval;
- uses SQLite FTS5 to identify at most 50 matched entry IDs, but then reads every active, non-expired user memory and current-project memory into Python;
- ranks entries by pinned first, then FTS/query-term match, importance, and ID;
- retrieves active episodes only for the same owner and thread, ranks them by query-term overlap and recency;
- independently clips long-term memories and episodes to the request's existing 1,500/1,000 token defaults;
- stores the rendered payload and exact revision/episode IDs in `memory_context_pins`, so retries for one model invocation reuse identical context.

The common failure is paraphrase recall: lexical matching may not connect “回答别写太长” to “用户喜欢简洁回答.” At the same time, identifiers such as `SQLITE_BUSY`, `memory_v2.py`, version strings, and IDs need exact lexical behavior. An embedding-only replacement would trade one failure mode for another.

There is also a boundary condition outside retrieval: `Database` is currently a concrete SQLite implementation and SQL dialect assumptions are spread through the application. PostgreSQL-as-authority is therefore a repository migration, not just a new vector table. This document defines the retrieval component and the seams it requires; the parent migration plan must cover all business tables, migrations, transaction syntax, job claims, JSON types, timestamps, and SQLite-specific tests.

## Minimal-change boundary

Keep these types and methods stable:

```python
MemoryContextProvider.select(request: MemoryContextRequest) -> MemoryContextBundle
MemoryContextRequest
MemoryContextBundle
```

Refactor only candidate discovery behind an internal collaborator:

```python
class MemoryCandidateRetriever(Protocol):
    def retrieve(self, *, owner_id: str, project_id: str | None,
                 query: str, now: datetime) -> RetrievalResult: ...

@dataclass(frozen=True)
class RetrievalResult:
    revision_ids: tuple[str, ...]
    reasons_by_revision: Mapping[str, str]
    mode: Literal["semantic", "lexical_fallback", "adaptive_hybrid", "pinned_only"]
    diagnostics: Mapping[str, object]
```

`select()` continues to own canonical scope resolution, prior-Pin reuse, episode retrieval/ranking, final entry ordering, token accounting, rendering, and `_save_pin()`. `PostgresMemoryCandidateRetriever` owns query embedding, semantic/lexical SQL, cascade decisions, and retrieval telemetry. This optimizes the two current callers (`conversation.py` and `runtime.py`) without changing them.

The retriever returns revision IDs rather than arbitrary content. `select()` must rejoin `memory_entries.current_revision_id = memory_revisions.id` under the same owner/scope/status predicates before rendering. This prevents a stale embedding row from injecting an old revision.

## PostgreSQL schema

PostgreSQL is the only authority. Markdown remains a rebuildable projection. SQLite and a separate vector store must not be used in the steady state.

Use PostgreSQL `timestamptz`, `boolean`, and `jsonb` when porting the existing ISO-text, integer-boolean, and JSON-text fields. Retain application IDs as `text` in the initial migration to minimize code and data conversion.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Existing authoritative tables, PostgreSQL form (relevant columns only).
CREATE TABLE memory_entries (
    id                    text PRIMARY KEY,
    owner_id              text NOT NULL,
    kind                  text NOT NULL CHECK (kind IN
                              ('preference','constraint','fact','decision','lesson')),
    scope_type            text NOT NULL CHECK (scope_type IN ('user','project')),
    scope_id              text NOT NULL DEFAULT '',
    status                text NOT NULL CHECK (status IN ('ACTIVE','ARCHIVED','PURGED')),
    current_revision_id   text,
    canonical_fingerprint text NOT NULL,
    pinned                boolean NOT NULL DEFAULT false,
    importance            double precision NOT NULL DEFAULT 0.5
                              CHECK (importance BETWEEN 0 AND 1),
    sensitivity           text NOT NULL DEFAULT 'normal',
    valid_until           timestamptz,
    created_at            timestamptz NOT NULL,
    updated_at            timestamptz NOT NULL,
    CHECK ((scope_type = 'user' AND scope_id = '') OR
           (scope_type = 'project' AND scope_id <> ''))
);

CREATE TABLE memory_revisions (
    id               text PRIMARY KEY,
    entry_id         text NOT NULL REFERENCES memory_entries(id) ON DELETE CASCADE,
    revision_no      integer NOT NULL,
    operation        text NOT NULL,
    content          text NOT NULL,
    content_hash     text NOT NULL,
    base_revision_id text,
    actor            text NOT NULL,
    source_refs      jsonb NOT NULL DEFAULT '[]'::jsonb,
    reason           text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL,
    search_document  tsvector GENERATED ALWAYS AS
        (to_tsvector('simple', content)) STORED,
    UNIQUE (entry_id, revision_no)
);

ALTER TABLE memory_entries
    ADD CONSTRAINT memory_entries_current_revision_fk
    FOREIGN KEY (current_revision_id) REFERENCES memory_revisions(id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX memory_entries_scope_idx
    ON memory_entries (owner_id, scope_type, scope_id, status, pinned, importance DESC);
CREATE INDEX memory_revision_fts_idx
    ON memory_revisions USING gin (search_document);
CREATE INDEX memory_revision_trgm_idx
    ON memory_revisions USING gin (content gin_trgm_ops);

CREATE TABLE embedding_profiles (
    id              text PRIMARY KEY,
    provider        text NOT NULL,
    model           text NOT NULL,
    model_revision  text NOT NULL,
    dimensions      integer NOT NULL CHECK (dimensions > 0 AND dimensions <= 2000),
    distance_metric text NOT NULL DEFAULT 'cosine' CHECK (distance_metric = 'cosine'),
    input_prefix    text NOT NULL DEFAULT '',
    active          boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, model, model_revision, dimensions, input_prefix)
);

CREATE UNIQUE INDEX embedding_profiles_one_active_idx
    ON embedding_profiles ((true)) WHERE active;

-- Initial deployment fixes one dimension per physical vector column.
-- Replace 1024 with the selected profile's tested output dimension.
CREATE TABLE memory_embeddings (
    revision_id       text NOT NULL REFERENCES memory_revisions(id) ON DELETE CASCADE,
    profile_id        text NOT NULL REFERENCES embedding_profiles(id),
    owner_id          text NOT NULL,
    scope_type        text NOT NULL CHECK (scope_type IN ('user','project')),
    scope_id          text NOT NULL DEFAULT '',
    content_hash      text NOT NULL,
    embedding         vector(1024) NOT NULL,
    embedded_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (revision_id, profile_id),
    CHECK ((scope_type = 'user' AND scope_id = '') OR
           (scope_type = 'project' AND scope_id <> ''))
);

CREATE INDEX memory_embeddings_scope_idx
    ON memory_embeddings (profile_id, owner_id, scope_type, scope_id);

CREATE TABLE embedding_jobs (
    revision_id     text NOT NULL REFERENCES memory_revisions(id) ON DELETE CASCADE,
    profile_id      text NOT NULL REFERENCES embedding_profiles(id),
    content_hash    text NOT NULL,
    status          text NOT NULL CHECK (status IN
                       ('QUEUED','RUNNING','RETRY_WAIT','COMPLETED','DEAD_LETTER')),
    available_at    timestamptz NOT NULL DEFAULT now(),
    lease_owner     text,
    lease_epoch     bigint NOT NULL DEFAULT 0,
    lease_until     timestamptz,
    attempts        integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 5,
    last_error_code text,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (revision_id, profile_id, content_hash)
);

CREATE INDEX embedding_jobs_claim_idx
    ON embedding_jobs (status, available_at, lease_until, created_at);
```

Why duplicate owner/scope onto `memory_embeddings`: it lets the vector query apply tenant and project predicates in the ANN scan. These columns are denormalized routing data, not authority. Write them from the authoritative entry in the same transaction as the embedding upsert and verify them by constraint/integration tests. A stricter alternative is a composite foreign key back to a revision-scope projection, but it adds schema and migration complexity. The mandatory rejoin before rendering is the final correctness guard.

`content_hash` already exists on revisions. Store it on the job and embedding row as a concurrency guard. A job must update `memory_embeddings` only when all three still match: requested revision, expected `content_hash`, and active `profile_id`. Model identity alone is insufficient: provider-side model changes, requested dimensions, and any query/document prefix affect the vector space, so `embedding_profiles` records all of them.

If the chosen model emits over 2,000 dimensions, either explicitly request a supported dimension at or below 2,000, or use `halfvec` (whose HNSW index supports up to 4,000 dimensions). Do not silently truncate vectors in application code. SiliconFlow's current Embeddings API documents a `dimensions` parameter for its Qwen3 embedding family and a float/base64 encoding choice; use float and validate returned vector length and finite values before commit.

## Semantic-first cascade

### 1. Canonical and security filtering

Keep the existing `resolve_scope(thread_id, owner_id)` and requested-project equality check. Every candidate query, including lexical fallback, must include:

```sql
e.owner_id = :owner_id
AND e.status = 'ACTIVE'
AND (e.valid_until IS NULL OR e.valid_until > now())
AND (
  (e.scope_type = 'user' AND e.scope_id = '')
  OR
  (e.scope_type = 'project' AND e.scope_id = COALESCE(:project_id, ''))
)
AND e.current_revision_id = r.id
```

This means an unbound thread receives user-scope memory only; `project_id IS NULL` must not accidentally match malformed project rows with an empty scope. Never retrieve broad candidates and filter owner/project only in Python.

Application predicates are required even if Row-Level Security is later added as defense in depth. An RLS policy tied to transaction-local owner context can reduce the impact of a missed predicate, but service roles/table owners can bypass RLS unless configured carefully; RLS does not replace explicit scope tests.

### 2. Pinned candidates

Read eligible pinned rows without requiring an embedding. They retain highest priority and remain available while the embedding provider/index is unhealthy. A pinned project memory still requires the exact project match.

### 3. Semantic candidates

Generate one query embedding using the active profile and cache it briefly by `(query_hash, profile_id)` in process. Set a strict timeout lower than the context-building budget and make exactly one synchronous provider attempt. A timeout, 429, or 5xx immediately selects lexical fallback; query-time embedding never enters exponential retry.

Exact initial query:

```sql
SELECT r.id AS revision_id,
       1 - (me.embedding <=> CAST(:query_embedding AS vector)) AS semantic_score
FROM memory_embeddings me
JOIN memory_revisions r
  ON r.id = me.revision_id AND r.content_hash = me.content_hash
JOIN memory_entries e
  ON e.current_revision_id = r.id AND e.id = r.entry_id
WHERE me.profile_id = :profile_id
  AND me.owner_id = :owner_id
  AND ((me.scope_type = 'user' AND me.scope_id = '') OR
       (me.scope_type = 'project' AND me.scope_id = COALESCE(:project_id, '')))
  AND e.owner_id = :owner_id
  AND e.status = 'ACTIVE'
  AND (e.valid_until IS NULL OR e.valid_until > now())
  AND ((e.scope_type = 'user' AND e.scope_id = '') OR
       (e.scope_type = 'project' AND e.scope_id = COALESCE(:project_id, '')))
ORDER BY me.embedding <=> CAST(:query_embedding AS vector)
LIMIT :semantic_k;
```

Keep `ORDER BY distance-operator ASC LIMIT` in this form so pgvector can use an ANN index later. Do not order by `1 - distance DESC`.

### 4. Acceptance and lexical fallback

Do not hard-code a universal cosine threshold. Scores differ by embedding model, language, dimensions, document prefix, and corpus. Build a labeled retrieval set from sanitized real queries plus adversarial scope cases. For each query label relevant revision IDs and “no relevant memory.” Sweep:

- `semantic_k`;
- top-1 minimum score;
- top-1/top-2 margin where applicable;
- minimum count above a score floor;
- query-embedding timeout.

Choose operating values that meet the release gates, then persist them by `profile_id` in configuration and record a calibration-set digest. Recalibrate whenever the profile changes. A practical cascade decision is:

```text
semantic_acceptable =
    top1_score >= calibrated_top1_min
    AND count(score >= calibrated_candidate_min) >= calibrated_min_candidates
    AND current_scope_embedding_coverage >= calibrated_min_scope_coverage
```

`current_scope_embedding_coverage` is the count of eligible active/current/non-expired revisions with a matching ready profile/hash vector divided by all eligible revisions in the canonical owner/project scope. It prevents a high-scoring result from a sparse partial backfill from hiding unembedded relevant memory. Compute/cache this operational value without weakening row filters.

Use a margin only if evaluation shows it improves no-result precision; multi-memory questions legitimately have several close neighbors. The calibration corpus must include no-relevant, exactly-one-relevant, and multi-relevant queries so acceptance does not overfit only multi-hit cases. Fall back lexically when the query is empty, embedding times out/errors, the active profile is missing, no current embeddings exist, dimensions/hash validation fails, or semantic acceptance fails.

Lexical fallback uses PostgreSQL FTS plus trigram/escaped substring matching. `websearch_to_tsquery('simple', :query)` is safer for raw user text than constructing tsquery syntax. The `simple` configuration avoids English-only stemming but Chinese search quality must be measured; `pg_trgm` supplies character-level matching closer to current trigram FTS5 behavior. Query terms containing `%` or `_` must be escaped before `ILIKE`.

### 5. Exact-query supplement

Semantic success does not suppress lexical retrieval when the query includes exact-looking tokens. Trigger a small lexical supplement when deterministic parsing finds any of:

- path separators or recognized file extensions;
- identifiers matching specific structured grammars, such as `snake_case`, a path with a recognized extension, namespaced identifiers, or multi-segment model/version names;
- error/status codes, long digit/hex strings, UUID-like values;
- semantic version/model patterns or URLs;
- quoted text.

A bare hyphen or period is not sufficient: ordinary prose, decimal numbers, and punctuation would otherwise cause excessive hybrid queries. Test the detector against Chinese punctuation, bullet-like hyphens, dates, decimals, and normal sentences, and prefer false negatives to a high global supplement rate; genuinely weak semantic results still take the lexical fallback.

This is `adaptive_hybrid`, not unconditional hybrid. Union by revision ID. Pinned rows rank first; exact lexical hits then receive a deterministic boost; remaining semantic rows order by calibrated semantic score, then existing importance and stable ID. After relevant semantic/lexical candidates, preserve today's scoped importance fallback: append still-eligible same-owner/user-or-current-project revisions ordered by importance and stable ID so spare token budget behaves as before. This backfill is scope-filtered SQL and must never reintroduce archived, expired, stale, foreign-owner, or foreign-project rows. Do not add incomparable raw `ts_rank` and cosine scores. If combined rank becomes necessary, use rank-based fusion and evaluate it; the initial cascade needs only an exact-hit boost.

### Why not run both every time?

Unconditional hybrid adds a second database search on every turn, makes ranking calibration harder, and provides little value for ordinary paraphrase queries when semantic confidence is good. Cascade retrieval keeps the common route small while retaining lexical correctness when semantic evidence is weak. The exact-token detector covers the main case where lexical retrieval is complementary rather than merely a fallback.

## Exact, HNSW, and IVFFlat decision

Phase 1 uses exact search with the scope B-tree index. It has perfect nearest-neighbor recall and is likely adequate while each owner/project candidate set is small.

If measured p95 `vector_search_ms` exceeds the agreed objective at production-like per-owner cardinality, add:

```sql
CREATE INDEX CONCURRENTLY memory_embeddings_hnsw_cos_idx
ON memory_embeddings USING hnsw (embedding vector_cosine_ops);
```

Choose HNSW over IVFFlat for this workload: pgvector documents a better speed/recall tradeoff, and HNSW does not require a trained populated index. IVFFlat builds faster and uses less memory but needs list/probe tuning as corpus size changes and generally has lower query performance.

Filtered ANN has a correctness risk. pgvector documents that approximate index filtering is applied after the ANN scan; selective `owner_id`/project filters can therefore return fewer scoped rows even when eligible neighbors exist. Mitigations, in order:

1. Keep exact search until it is demonstrably too slow.
2. Maintain the scope B-tree index and test plans/cardinality.
3. With pgvector 0.8+, use `SET LOCAL hnsw.iterative_scan = strict_order` and tune `hnsw.ef_search` from recall tests.
4. If multitenant scale creates interference, partition `memory_embeddings` by a bounded tenant bucket, not one partition/index per user. A partial HNSW index can improve filtering only when its predicate is a literal, stable, low-cardinality scope that the planner can prove from the query. It does not solve arbitrary parameterized owner/project filters, and one partial index per user/project causes index and planning overhead. Consider it only for a few stable, high-volume scopes; PostgreSQL warns against using many partial indexes as a substitute for partitioning.
5. In the retriever, if ANN returns fewer above-threshold rows than expected, first run an exact vector query restricted to the same canonical owner/project scope and profile. If exact-vector quality/coverage still fails, then run lexical fallback. This distinguishes ANN post-filter recall loss from genuinely weak semantic relevance. Bound this recovery by the same retrieval deadline; never broaden scope. Evaluation/debug endpoints compare ANN against this exact scoped query.

Do not claim that explicit `WHERE owner_id/project` makes HNSW exact. It protects isolation at the SQL result level, but post-filtered ANN can still lose recall. Cross-owner rows must never be returned; insufficient rows are a degradation signal, not permission to weaken the filter.

## Embedding lifecycle

### Commit path

`embedding_jobs` is the transactional outbox for vector generation. When a new revision becomes current, the same PostgreSQL transaction that commits it inserts an idempotent outbox row for the active profile. It does not call the external embedding API. Either both revision and outbox commit, or neither does; memory save latency and availability remain independent of the embedding provider.

Archive/purge/update rules:

- update creates a new immutable revision and job; the prior embedding may remain for audit but cannot be selected because of the current-revision join;
- archive makes the entry immediately ineligible; no synchronous vector deletion is required;
- purge cascades revision embeddings/jobs and invalidates existing context pins according to current rules;
- profile activation queues every current active revision missing a matching `(profile_id, content_hash)`.

### Worker claim, retry, and completion

Reuse the repository's lease-based worker pattern. PostgreSQL workers claim jobs in a short transaction using `FOR UPDATE SKIP LOCKED`, which PostgreSQL explicitly describes as suitable for multiple consumers of a queue-like table. Commit the claim before the network request.

The worker:

1. claims up to a bounded batch with `FOR UPDATE SKIP LOCKED`, sets lease times from PostgreSQL `clock_timestamp()`, and increments both `attempts` and `lease_epoch`;
2. reads the immutable revision text and verifies `content_hash`;
3. calls the provider adapter in a batch where supported;
4. validates count, dimension, finite numbers, model/profile identity if returned, and response mapping;
5. in a new transaction, rechecks content hash/profile, upserts `memory_embeddings`, and marks the exact job completed only under `WHERE lease_owner = :owner AND lease_epoch = :epoch AND lease_until > clock_timestamp()`;
6. on retryable timeout/429/5xx, stores a redacted error code and exponential backoff with jitter;
7. on invalid vector, unsupported dimensions, authentication/configuration error, or exhausted attempts, moves to `DEAD_LETTER` and alerts without blocking lexical retrieval.

Leases recover crashed workers. Every heartbeat, retry transition, completion, and failure transition is fenced by `(lease_owner, lease_epoch)` and database time, and must affect exactly one row. Application-host time is diagnostic only and cannot decide lease validity. Completion is idempotent on `(revision_id, profile_id)` plus `content_hash`; a late worker from an older epoch or obsolete hash must not overwrite the current vector or job state.

### Profile replacement and rebuild

Never compare embeddings from different profiles. To change provider/model/revision/dimensions/prefix:

1. insert a new inactive profile and create a compatible physical vector column/table migration if dimensions differ;
2. enqueue backfill jobs for all active current revisions;
3. monitor coverage and evaluate retrieval against the fixed labeled set;
4. atomically switch the configured active profile only after coverage and quality gates pass;
5. retain the old profile for rollback, then delete it after the rollback window.

For minimal schema churn, standardize on one tested dimension (recommended initial candidate: 1,024) across profiles. pgvector's typed column enforces dimensional consistency. If future simultaneous dimensions are required, use profile-specific embedding tables rather than an untyped `vector` column with fragile expression indexes.

## Context Pin and token-budget behavior

Pin lookup stays before embedding, so a model retry incurs no embedding/search and receives byte-identical memory context. Extend the Pin binding/metadata, without changing the bundle type, with:

```json
{
  "retrieval_policy_version": "semantic-cascade-v1",
  "embedding_profile_id": "...",
  "mode": "semantic|lexical_fallback|adaptive_hybrid|pinned_only",
  "calibration_version": "...",
  "thresholds_digest": "..."
}
```

The profile, policy, calibration version, and exact threshold digest belong in the binding hash so the same invocation cannot be rebound to different retrieval decisions. Existing pins retain the rendered payload and remain usable during a profile rollout until expiry unless their source revision/episode is edited/deleted under current invalidation rules.

Keep `semantic_token_budget`, `episode_token_budget`, tokenizer version, renderer version, selected revision IDs, selected episode IDs, and final content hash unchanged. Candidate scores affect order before clipping but are not rendered as facts. Reasons should become specific (`pinned`, `semantic`, `lexical_fallback`, `exact_lexical`) and remain aligned with selected items.

## Provider interface

Use an OpenAI-compatible but vendor-neutral adapter, not model-specific calls inside `memory_v2.py`:

```python
class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str], *, model: str,
              dimensions: int, timeout_s: float) -> EmbeddingBatch: ...
```

The current `select()` callers are inside async conversation/run paths, while `Database`, `MemoryContextProvider`, and their callers are otherwise synchronous. Converting this one method and its transitive database/Pin helpers to async would broaden the migration and duplicate sync/async persistence APIs. The minimal compatible change is therefore to `await loop.run_in_executor(retrieval_executor, provider.select, request)` at the two call sites.

`retrieval_executor` must be one process-level, runtime-owned, bounded blocking-I/O executor created at startup and shut down with the runtime. Never construct an executor per query. Size it against the configured maximum active conversation plus agent-run concurrency, put an explicit semaphore/queue bound in front of it, and expose saturation. Its workers reuse the process-level HTTP client/connection pool but each retrieval obtains a normal PostgreSQL pooled connection. The background indexing worker uses its own process/thread capacity and cannot consume query-retrieval slots.

Assign one monotonic context deadline before awaiting executor capacity. Executor queue wait, query-embedding HTTP, vector SQL, exact-vector recovery, lexical fallback, entry hydration, rendering, and Pin save all consume that single remaining budget; each layer receives `deadline - monotonic_now`, not an independent full timeout. If network semantic work cannot start within its scheduling slice, skip it and spend the remaining deadline on the no-network lexical path. Database statement timeouts must also be capped by the remaining budget. Load tests must prove the executor does not become a new global serialization point and the fallback retains enough reserved budget to complete.

Configuration references an environment secret name; API keys are never stored in profiles, traces, jobs, or memory. Accept provider base URL, model, dimensions, batch size, and timeout. Emit float vectors and verify response order/count. Query embedding uses a single-item call; indexing workers batch documents within the provider's documented limits.

SiliconFlow documents `POST /v1/embeddings` with `model`, string/string-array `input`, `encoding_format` (`float` or `base64`), and model-dependent `dimensions`. That is compatible with this adapter. Availability, model permissions, limits, and semantic quality still require a deployment smoke test; they must not be inferred from chat-model configuration.

## Observability

Add one retrieval span per fresh `select()` and no fresh-search span for a Pin hit. Record only hashes/IDs, never raw memory or queries:

```text
memory_retrieval_ms
retrieval_executor_wait_ms
query_embedding_ms
vector_search_ms
keyword_search_ms
pin_lookup_ms
retrieval_mode
fallback_reason
embedding_profile_id
semantic_candidate_count
semantic_accepted_count
lexical_candidate_count
selected_revision_count
selected_episode_count
pinned_count
stale_embedding_count
embedding_coverage_ratio
current_scope_embedding_coverage
top1_semantic_score_bucket
token_count
dropped_count
```

Worker metrics:

```text
embedding_job_queue_depth
embedding_job_oldest_age_seconds
embedding_job_attempts_total{outcome,error_code}
embedding_api_ms
embedding_batch_size
embedding_jobs_dead_letter_total
embedding_profile_coverage_ratio
```

Integrate `memory_retrieval_ms` under the existing `context_ms`; do not redefine `context_ms`. Trace UI should show the compact mode, total retrieval seconds, vector/keyword/embedding seconds, candidate-to-selected counts, and fallback reason, with detailed identifiers behind expansion.

Alert on sustained low coverage, dead letters, embedding/provider error rate, lexical fallback-rate regression, scope-isolation test failure, and p95 retrieval/context latency regression.

## Failure modes and required behavior

| Failure | Required behavior |
|---|---|
| Embedding provider timeout, 429, or 5xx | Bound query timeout; lexical fallback; answer continues. Worker retries asynchronously. |
| Provider auth/config invalid | Lexical fallback; dead-letter jobs; alert; never expose secret/error body. |
| Embedding PENDING/STALE or missing/partial backfill | Search only matching current profile/hash vectors, then lexical fallback when acceptance/coverage is insufficient; never wait for document embedding. |
| Dimension mismatch, NaN, Infinity, wrong response count | Reject response; do not write vector; fallback/dead-letter as appropriate. |
| Revision changes while job runs | Hash/current-profile recheck prevents stale overwrite; current-revision join prevents stale retrieval. |
| HNSW post-filter recall loss | Iterative scan plus calibrated `ef_search`; insufficient candidates trigger bounded same-scope exact-vector recovery, then lexical fallback if still unacceptable; never broaden scope. |
| FTS parser-sensitive input | Use `websearch_to_tsquery`; escaped trigram/substring query; return empty lexical set on a bounded query error. |
| Empty query | Pinned plus existing scoped ordering/episodes; no embedding API call. |
| Pin hit during provider outage/profile rollout | Return stored, hash-verified payload unchanged. |
| PostgreSQL unavailable | Fail the operation clearly. Do not silently read SQLite, because PostgreSQL is the only authority. |
| Duplicate worker completion/crash | Idempotent upsert and leases; no duplicate authoritative revisions. |
| Cross-owner/project query bug | Explicit predicates and final rejoin return zero foreign rows; security tests are release blockers. |

## Acceptance criteria

1. PostgreSQL contains all application and memory authority; the production process performs no SQLite read/write, and Markdown can be rebuilt from PostgreSQL.
2. The two existing `select()` callers require no signature or result-shape change.
3. A valid Context Pin is checked before query embedding and returns the identical payload/revision/episode IDs on retry.
4. No query, failure, ANN setting, or missing vector can return memory from another owner or a different project. Unbound threads receive no project memory.
5. On a versioned labeled retrieval set, semantic-first recall@K improves over lexical-only by the agreed release delta while no-relevant precision and exact-identifier success do not regress. Thresholds and their calibration-set digest are checked in, not chosen informally.
6. With the embedding endpoint unavailable, normal answers still receive eligible pinned and lexical memories within the context latency SLO.
7. New memory creation does not wait for embedding; a healthy worker reaches the agreed embedding coverage SLA, and revisions remain lexically retrievable before vector completion.
8. Stale/wrong-dimension/wrong-profile vectors are never selected. Profile rollout is reversible until the rollback window closes.
9. Exact search meets the measured p95 target at expected per-owner/project cardinality. HNSW is enabled only if benchmark evidence requires it and its scoped recall meets the release gate versus exact search.
10. Existing 1,500/1,000 default budgets, untrusted-data rendering, Episode thread-only policy, and pin invalidation semantics remain intact.
11. The retrieval context deadline covers executor wait plus all provider/database/recovery work; it reserves enough time for lexical fallback and exposes executor wait separately.

Numerical targets should be approved from a same-hardware baseline run. Initial release gates, aligned with the PostgreSQL migration test strategy, are: zero isolation violations; 100% migration invariants; ACTIVE-current embedding coverage at least 99% before semantic becomes default; semantic recall@10 no worse than 0.90 on labeled positives; exact-identifier top-10 success no worse than lexical baseline; lexical database search p95 no more than 50 ms; exact-vector database search p95 no more than 100 ms at projected per-owner cardinality; if HNSW is enabled, p95 no more than 50 ms and scoped recall@10 at least 0.95 versus exact (target 0.98), with filtered insufficient-result rate below 1%; connection-pool wait p95 no more than 50 ms; and query-embedding timeout configured in the 300-800 ms envelope based on measured provider latency. Embedding-provider outage may add no more than that configured timeout. These are release gates, not hard-coded ranking thresholds.

## Test plan

### Unit tests

- exact-token detector covers paths, IDs, versions, URLs, quoted strings, ordinary Chinese paraphrases, and false-positive cases;
- cascade chooses semantic, lexical fallback, adaptive hybrid, and pinned-only modes for deterministic score/error fixtures;
- threshold values are loaded by profile/calibration version and dimension/profile mismatch is rejected;
- ranking is pinned first, exact lexical boost second, semantic score/importance/stable ID thereafter;
- scoped importance backfill preserves current spare-budget behavior without admitting ineligible rows;
- token clipping and reason/revision alignment match current behavior;
- provider adapter validates dimensions, finite floats, batch count/order and redacts errors;
- content hashes are deterministic over the exact embedded document representation.

### PostgreSQL integration tests

- migrations install `vector`/`pg_trgm`, constraints, generated `tsvector`, GIN indexes, and embedding/job tables;
- semantic query returns a paraphrase while lexical fallback returns an exact error code/path;
- owner A cannot retrieve owner B under semantic, fallback, adaptive hybrid, pinned, missing-vector, and forced-provider-error paths;
- project A cannot retrieve project B; user-scope memory is visible to both owned projects; a null-project thread sees no project-scope row;
- expired, archived, purged, stale-revision, stale-hash, and inactive-profile rows are excluded;
- a newly saved revision is lexically available before its embedding job completes;
- concurrent workers using `FOR UPDATE SKIP LOCKED` never process a job as two successful authoritative completions; expired leases recover;
- application clock skew of plus/minus five minutes does not change claim/takeover/finalization because database `clock_timestamp()` and `lease_epoch` fence all transitions; an old epoch updates zero rows;
- retryable errors back off and eventually complete; permanent/exhausted jobs dead-letter;
- edit during an in-flight embedding cannot publish the old hash as current;
- purge cascades embeddings/jobs and current pin invalidation behavior remains correct;
- profile backfill/switch/rollback never mixes vector spaces;
- Context Pin lookup precedes provider invocation and returns identical payload on the second call;
- changing a calibrated threshold digest for the same invocation produces a binding conflict rather than silently changing pinned retrieval;
- PostgreSQL transaction rollback leaves no partial memory revision/job state.

### Retrieval evaluation

Build a versioned corpus with:

- Chinese paraphrases of preferences, constraints, facts, decisions, and lessons;
- project/user overlap and deliberately similar cross-owner/cross-project decoys;
- exact paths, model names, error codes, UUIDs, versions, and mixed Chinese/English;
- no-relevant-memory queries and multi-relevant-memory queries;
- stale and missing embeddings.

Compare lexical-only, semantic cascade, and adaptive hybrid for recall@K, precision@K, MRR/nDCG where useful, no-result precision, exact-token success, fallback rate, and latency. Calibrate on one split and report once on a held-out split. Repeat whenever the embedding profile/document representation changes.

### Performance and resilience tests

- measure exact vector p50/p95/p99 at projected per-owner cardinalities and concurrent turn load;
- measure the shared retrieval executor's queue wait/saturation and prove concurrent different-thread turns do not serialize on retrieval; verify no executor/thread-pool is created per request;
- force delays independently in executor wait, embedding HTTP, ANN SQL, exact recovery, and lexical SQL; assert their sum obeys one context deadline and fallback retains its reserved slice;
- if HNSW is proposed, compare scoped ANN recall/latency to exact across selective owner/project filters and tune `ef_search`/iterative scan;
- simulate provider timeout, 429, 500, malformed JSON, wrong dimension, partial batch, slow worker, worker crash, lease expiry, and PostgreSQL restart;
- verify embedding outage affects only retrieval up to the configured timeout and does not consume model retry budget;
- verify query text/content/secrets are absent from logs, metrics labels, and trace summaries.

### Migration and end-to-end tests

- migrate a copy of SQLite data; compare every table row count plus canonical per-table content hashes, foreign keys, current revisions, audit order, and projection output;
- run old and PostgreSQL implementations against the same fixture set and compare non-retrieval behavior;
- run conversation and agent-run flows through unchanged `select()` callers;
- canary with shadow retrieval: old lexical output drives answers while semantic results and isolation/quality metrics are compared; then enable semantic cascade by feature flag;
- prove rollback before cutover and prove that after cutover no SQLite fallback path exists.

## Rollout sequence

1. Introduce the PostgreSQL database adapter/migrations and port all application persistence, while preserving service APIs.
2. Rehearse SQLite-to-PostgreSQL copy, validation, maintenance-window delta/final copy, cutover, and rollback. Only cut over when PostgreSQL is the sole writable authority. Once PostgreSQL accepts a new write, application rollback means deploying a prior build that still targets the compatible PostgreSQL schema; it never means resuming SQLite. Keep SQLite only as a read-only migration artifact, with no runtime connection path.
3. Add embedding profile, job, provider adapter, worker, and lexical PostgreSQL retriever. Keep semantic retrieval disabled while backfilling.
4. Build the labeled retrieval set, calibrate the active profile, and run semantic retrieval in shadow mode.
5. Enable exact semantic-first cascade behind a flag; observe latency, fallback rate, quality, isolation, and coverage.
6. Add HNSW only after an exact-search benchmark demonstrates need; validate filtered ANN recall before enabling it.
7. Remove SQLite runtime code and FTS5 after the rollback window. Keep Markdown explicitly documented as a projection.

## Primary sources

- pgvector official README: exact/approximate search, cosine operator, HNSW vs IVFFlat, vector dimensional limits, filtered ANN behavior, iterative scans, multitenancy, hybrid search, and index-usable query form: https://github.com/pgvector/pgvector/blob/master/README.md
- PostgreSQL full-text preferred indexes: GIN is the preferred text-search index: https://www.postgresql.org/docs/current/textsearch-indexes.html
- PostgreSQL `SELECT` locking: `SKIP LOCKED` is suitable for multiple consumers of queue-like tables: https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE
- PostgreSQL partial indexes, including the warning not to create many partial indexes as a substitute for partitioning: https://www.postgresql.org/docs/current/indexes-partial.html
- PostgreSQL Row-Level Security and default-deny/bypass behavior: https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- PostgreSQL JSON types: `jsonb` supports indexing and avoids repeated reparsing: https://www.postgresql.org/docs/current/datatype-json.html
- SiliconFlow Create Embeddings API: OpenAI-shaped `/v1/embeddings`, input/model/encoding/dimensions fields and Qwen3 dimensional options: https://docs.siliconflow.com/en/api-reference/embeddings/create-embeddings

## Repository evidence

- `backend/app/memory_v2.py`: `MemoryContextRequest`, `MemoryContextBundle`, `MemoryContextProvider.select`, token clipping, renderer, Context Pin binding/payload persistence, and scope validation.
- `backend/app/db.py`: SQLite connection/PRAGMA/FTS5 initialization and authoritative memory/context-pin schemas.
- `backend/app/conversation.py`: primary conversation caller and existing `context_ms` timing.
- `backend/app/runtime.py`: agent-run caller that consumes selected revision IDs.
- `backend/tests/test_memory_v2.py`: current FTS lifecycle, scope, budget, pin reuse/invalidation, and projection behavior tests.
