# PostgreSQL test migration (PG01)

Status: **PG01 implementation**. Classification is a static AST inventory; it is
not pytest collection and no business test has been migrated yet. PG02–PG05
targets below are plans, not migrated claims. The stale historical 2696/7 gate is
not used as evidence for this batch.

## 1. Artifacts

| artifact | purpose |
|---|---|
| `docs/harness-mainline/postgres-test-inventory.json` | machine inventory: every test function, classification, flags, dependency/evidence paths, parametrize/marker/usefixture metadata, explicit old→new nodeid and status, and every `assert` / `pytest.raises` / `pytest.warns` with full source span, line range and digest |
| `docs/harness-mainline/postgres-test-inventory.md` | reviewable table: old nodeid → new nodeid/planned target → assertion `line:kind:digest` |
| `scripts/inventory_backend_tests.py` | reproducible generator (no Docker, no database) |

Regenerate:

```powershell
D:/pycharm/python.exe scripts/inventory_backend_tests.py
```

The generator only parses files with `ast`; it never imports a test module, so it
cannot execute a fixture, open SQLite/PostgreSQL or call the network. Parse
errors, duplicate node ids, a `test_*.py` with no test functions, or an
assertion-extraction mismatch make it exit non-zero instead of emitting a
clean-looking report. `analyze()` is exposed so generator self-regressions can
run against tiny temporary AST roots.

Output is deterministic: every set traversal that feeds emitted dependencies or
evidence is sorted, so the JSON and Markdown are byte-identical across processes
with different `PYTHONHASHSEED` values (verified by a two-subprocess regression;
no seed is hardcoded).

## 2. Classification rules (transitive, evidence-backed)

Each test function (including `Test*` class methods and tests re-exported by
`integration/test_*_postgres.py`) is classified from its own body **plus**
transitively resolved dependencies:

- same-file helpers and helper chains (ordinary helper parameters are call
  arguments, never fixture dependencies),
- fixtures defined in the test module, **class fixtures** (`self.<fixture>` /
  method args), and class helper methods reached through `self.<method>`,
- fixtures defined in applicable `conftest.py` files (including imported ones),
- `pytestmark` / `@pytest.mark.usefixtures(...)` at module and class level,
- every effective autouse fixture: all `autouse=True` names from class, module and
  applicable conftests are accumulated, then each resolves to the nearest
  implementation in the collecting context (a closer non-autouse override
  executes). Guard tests override `isolated_postgres_test` with a no-op, so they
  are **not** counted as PostgreSQL-backed; `shadows_autouse` records the
  override,
- imports under `backend/tests` (`pg_fixtures`, sibling helpers).

Parametrize handling: direct parameter names are **values**, not fixtures;
`indirect=True` / `indirect=[...]` names are fixtures. Runtime generated ids stay
unresolved. Marker metadata (`pytestmark`, class/module decorators) is recorded.

Fixture references are routed through the fixture queue: a fixture referenced by
name is not consumed as an ordinary helper, so its own fixture dependencies are
resolved recursively. A fixture's dependencies are resolved in the **original
collecting test module/class context**, so a module override is honoured even for
a conftest fixture's argument, while effective autouse shadowing is preserved.

Priority and flags:

1. **`real_network`** — only with an explicit `pytest.mark.live/network/external/
   acceptance` marker. A filename token alone is **not** network evidence: fake
   `web_search`/client unit tests are not real external calls. Files whose name
   hints network but have no marker and no DB/SQLite evidence are `unresolved`
   with `network_unverified`; they are neither claimed network nor claimed pure.
2. **`legacy_migration`** — file name contains `migration`. Target: keep the
   SQLite/old-revision **input rows** and add a real old-revision → Alembic head
   upgrade on `migrated_postgres_url`; do not rewrite every migration. The
   `test_sqlite_postgres_migration.py` SQLite input is preserved.
3. **`direct_sqlite`** — resolved text contains `sqlite3`, `:memory:`,
   `sqlite://` or a `.db` literal. (`BETTER_AGENT_TEST_ALLOW_SQLITE` is an env
   flag set by the autouse conftest fixture, not storage usage.) Business
   assertions move to `isolated_postgres_test`; they are not dropped.
4. **`database_backed`** — resolved text contains `Database(`, `build_runtime(`
   or `psycopg.connect`, or a resolved **postgres fixture / DB-evidence helper**
   dependency. Pure `pg_fixtures` helpers such as `owned_database_name` are not
   evidence. A re-exported test running on the collecting module's postgres
   fixtures is `database_backed` even when its source module has SQLite evidence
   (`source_sqlite_evidence`).
5. **`unresolved`** — no database evidence and either an unresolvable fixture
   dependency or an unverified network filename hint. Unknown dependencies are
   marked unresolved, never asserted pure; names are recorded per test.
6. **`pure_no_db`** — no database evidence, all dependencies resolved, no network
   hint. Stays untouched and never starts Docker.

### Static counts (this revision)

| classification | test functions |
|---|---:|
| `direct_sqlite` | 1016 |
| `pure_no_db` | 748 |
| `database_backed` | 384 |
| `real_network` | 0 |
| `legacy_migration` | 11 |
| `unresolved` | 110 |
| **total static functions** | **2269** |

177 functions carry `@pytest.mark.parametrize`; runtime node ids are annotated
unresolved. The 110 unresolved entries are all `network_unverified` filename
hints (the suite currently has no explicit network markers); unknown dependency
names are also reported there when present. The static total is not the runtime
collected count; this document does not claim a collection count.

## 3. Mapping contract

Every entry records `old_nodeid`, `new_nodeid`, `status: "planned"`,
`new_location`, classification, `flags`, `evidence`, `dependencies`,
`unresolved_dependencies`, `parametrize`, `markers` and `usefixtures`.

- `old_nodeid` is the current `backend/tests/<file>::[Class::]test_name`.
- `new_nodeid` is the explicit planned target nodeid (same nodeid for in-place
  migration; `[runtime ids unresolved]` appended for parametrized tests).
- `status` is `planned`: PG01 has not migrated any business test.
- Assertions: `assert` statements and every `pytest.raises`/`pytest.warns` call
  are recorded at their exact source span (not the surrounding `with` block, so
  original line references are not duplicated or obscured). JSON keeps the full
  source (no truncation), `line`, `end_line`, `span_complete`, `kind` and digest;
  the Markdown shows compact `line:kind:digest` references.
- The generator independently counts `ast.Assert` + `pytest.raises`/`warns`
  calls and fails if extraction disagrees.

A migrated test must preserve the recorded assertions. Deleting or rewriting one
requires an explicit note in the PG02–PG04 change that cites the old nodeid and
digest.

## 4. Isolation fixtures (implemented)

`backend/tests/pg_fixtures.py` is the single implementation shared by
`backend/tests/conftest.py` (ordinary, opt-in) and
`backend/tests/integration/conftest.py` (autouse, preserved).

- **One Docker lifecycle.** Only `postgres_url` starts `integration/compose.postgres.yaml`
  under a unique compose project (`better-agent-pgtest-<pid>-<uuid>`), with its
  own tmpfs data directory and published port. Pure tests never request it.
- **Configured base is never mutated.** `TEST_DATABASE_URL` is validated and then
  used only to allocate a uniquely named owned database
  (`<base>_<full-uuid>_test`, 128-bit, within PostgreSQL's 63-byte limit).
- **Ownership proof before any drop.** The allocation is registered **before**
  `CREATE DATABASE`. A successful `CREATE` is recorded immediately, before the
  admin connection closes, so a close failure still permits a proven cleanup. A
  `DuplicateDatabase` means the name already existed: nothing is dropped. Any
  other CREATE failure is transport-ambiguous, so cleanup refuses and reports
  that ownership cannot be proven instead of guessing. `drop_owned_database`
  accepts only a registered allocation, refuses the configured base, and cannot
  drop an arbitrary suffix-safe name.
- **Validation before mutation.** `assert_isolated_test_database` runs before
  `CREATE DATABASE`, before `alembic upgrade`, and inside every truncate.
- **Bounded connections and cleanup.** Connections use a connect timeout and
  per-session `statement_timeout`/`lock_timeout`; Docker start/stop and alembic
  are bounded. Safe DSN query options (`sslmode`, `application_name`, …) survive
  the database-path swap; routing/override options remain refused by the guard.
- **Lifecycle with `finally`.** `isolated_database_lifecycle` puts the post-test
  truncate in `finally` around setup+yield. If setup and cleanup both fail, the
  primary error is preserved and the cleanup failure is attached as a note; a
  cleanup-only failure raises `pytest.fail`. Ordinary and integration fixtures
  share this one lifecycle.
- **No SQLite fallback / non-zero failures.** Missing Docker, start failure,
  unparsable port, unsafe target, create/connection failure, migration
  failure/timeout, and cleanup failure/timeout all fail the run (non-zero).
- **Cleanup order.** Test-owned finalizers run before session teardown; the owned
  DB is dropped with `DROP DATABASE … WITH (FORCE)` so a leaked handle is
  terminated, and fixture-owned admin connections are closed first.
- **Integration autouse preserved.** `integration/conftest.py` keeps the autouse
  truncating fixture and re-exports `migrated_postgres_url` (the guard-ordering
  test uses `.__wrapped__`).
- **Ordinary tests opt in** by requesting `postgres_url`,
  `migrated_postgres_url`, or `isolated_postgres_test`; the parent conftest
  imports them as non-autouse fixtures.

Focused regressions: `backend/tests/test_pg01_isolation.py` (guard/fault,
configured-base ownership, CREATE-collision survival, ambiguous-create refusal,
close-after-success cleanup, and actual fixture teardown in pytest subprocesses)
and `backend/tests/test_pg01_postgres_fixtures.py` (two independent connections,
opt-in truncation, concurrent owned allocations against one dedicated base with
an untouched sentinel, create/drop, and a real same-name collision that must
survive).

## 5. Bounded supplementary references

The generator scans `scripts/` and `frontend/e2e/` (bounded to 200 rows) for
`sqlite3`, `:memory:`, `sqlite://`, `BETTER_AGENT_TEST_ALLOW_SQLITE` and `.db`
literals. Current hits are limited to `scripts/e2e_postgres.py` lines 44, 46 and
154, which *remove* `BETTER_AGENT_TEST_ALLOW_SQLITE` from the page-test
environment; they are not SQLite test paths. Full output is in the inventory.

## 6. Limits and non-claims

- Static AST inventory, **not** pytest collection. Parametrized runtime ids are
  unresolved by construction.
- Dependency resolution is syntactic (`backend/tests` imports, module/class/
  conftest fixtures, pytestmark/usefixtures). Plugin-provided fixtures that cannot
  be found are marked unresolved rather than guessed.
- No business test is migrated in PG01; per-nodeid targets are plans.
- Network is only claimed with an explicit marker; unmarked filename hints are
  recorded as unresolved for human review.
- The default gate remains the existing mixed `scripts/test_all.py` until PG05.

## 7. Follow-on tasks

- **PG02:** notifications, trace, archive, task runtime, learning.
- **PG03:** conversation, plan, approvals, reviews.
- **PG04:** remaining persistence + real old-revision upgrade coverage.
- **PG05:** remove the global SQLite permit, add SQLite-target refusal, keep full
  collection, and make the gate PostgreSQL-only.
