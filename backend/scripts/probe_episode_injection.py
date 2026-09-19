"""Probe: does the Episode that replaced the cropped history actually reach the model?

Plan v2 / M2-02 asks for one specific thing that is easy to mistake for "the
priority was raised":

    「新归档替换原始历史后，本轮必须能注入对应已提交 Episode，
      避免只依赖相关性检索偶然选中」

Raising ``_context_priority`` from 20 to 60 fixes a *different* problem -- that
an Episode which *was* selected got dropped before the raw turns it replaces.
It says nothing about whether the Episode covering the just-cropped prefix is
selected in the first place.

This probe measures the selection itself, on the real
``MemoryContextProvider.select`` path, with no model calls and no network.

Scenarios
---------
A  small older episode matches the query, small newest does not
   -> both fit; the happy path.
B  query matches nothing -> recency ordering carries the newest one.
C  one episode at the size the summariser is allowed to produce
   (``MAX_SUMMARY_OUTPUT_TOKENS``) against the default episode budget.
D  a *medium* older episode matches the query, so it eats the episode budget
   before the newest one is considered.

Read-only: builds throwaway databases under a temp directory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Database  # noqa: E402
from app.memory_archive import MAX_SUMMARY_OUTPUT_TOKENS  # noqa: E402
from app.memory_v2 import (  # noqa: E402
    DEFAULT_TOKEN_COUNTER, MemoryContextProvider, MemoryContextRequest, MemoryStore,
)

EPISODE_BUDGET = MemoryContextRequest.__dataclass_fields__["episode_token_budget"].default


def _thread(db, thread_id: str, owner_id: str) -> None:
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (thread_id, thread_id, owner_id, None, "now", "now"),
        )


def _save(store, *, thread_id: str, start: int, end: int, source_hash: str, summary: str, evidence: str = ""):
    synopsis = [{"text": evidence, "source_message_ids": [f"m-{start}"]}] if evidence else []
    return store.save_episode(
        owner_id="u", thread_id=thread_id, project_id=None,
        start_message_seq=start, end_message_seq=end, source_hash=source_hash,
        summary=summary, synopsis=synopsis,
    )


def _rendered_bytes(db, episode_id: str) -> int:
    with db.connection() as connection:
        row = connection.execute("SELECT * FROM memory_episodes WHERE id=?", (episode_id,)).fetchone()
    return DEFAULT_TOKEN_COUNTER.count_text(MemoryContextProvider._render_episode(row))


def _run(title: str, query: str, provider, labels: dict[str, str]) -> dict:
    bundle = provider.select(MemoryContextRequest("u", "t", None, query))
    selected = [labels.get(eid, eid) for eid in bundle.episode_ids]
    newest = any(name.startswith("NEWEST") for name in selected)
    print(f"{title}")
    print(f"  query = {query!r}")
    print(f"  selected = {selected or '(nothing)'}")
    print(f"  the episode covering the cropped prefix arrived = {newest}")
    print(f"  rendered memory block = {DEFAULT_TOKEN_COUNTER.count_text(bundle.rendered)} bytes")
    print()
    return {"title": title, "selected": selected, "newest_arrived": newest}


def main() -> int:
    print(f"episode_token_budget (default) = {EPISODE_BUDGET} bytes")
    print(f"MAX_SUMMARY_OUTPUT_TOKENS      = {MAX_SUMMARY_OUTPUT_TOKENS} bytes")
    print()

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)

        # ------------------------------------------------------------- A / B / D
        db = Database(root / "ab.db")
        store = MemoryStore(db, root / "ab-memory")
        _thread(db, "t", "u")

        small_older = _save(store, thread_id="t", start=1, end=100, source_hash="sha256:o1",
                            summary="用户讨论 postgres 索引与查询计划。")
        small_newest = _save(store, thread_id="t", start=101, end=200, source_hash="sha256:n1",
                             summary="用户调整了发布流程与值班表。")
        provider = MemoryContextProvider(db)
        print(f"[setup] small OLDER rendered = {_rendered_bytes(db, small_older.id)} bytes")
        print(f"[setup] small NEWEST rendered = {_rendered_bytes(db, small_newest.id)} bytes")
        print()

        labels = {
            small_older.id: "OLDER (messages 1-100)",
            small_newest.id: "NEWEST (messages 101-200, the cropped prefix)",
        }
        results = [
            _run("A: query matches the OLDER episode", "postgres", provider, labels),
            _run("B: query matches nothing", "完全无关的问题", provider, labels),
        ]

        # D: make the matching episode big enough to consume the budget alone.
        db_d = Database(root / "d.db")
        store_d = MemoryStore(db_d, root / "d-memory")
        _thread(db_d, "t", "u")
        filler = "y" * 300
        medium_older = _save(store_d, thread_id="t", start=1, end=100, source_hash="sha256:o2",
                             summary=f"用户讨论 postgres 索引 {filler}", evidence=filler)
        medium_newest = _save(store_d, thread_id="t", start=101, end=200, source_hash="sha256:n2",
                              summary=f"用户调整了发布流程 {filler}", evidence=filler)
        provider_d = MemoryContextProvider(db_d)
        print(f"[setup] medium OLDER rendered = {_rendered_bytes(db_d, medium_older.id)} bytes")
        print(f"[setup] medium NEWEST rendered = {_rendered_bytes(db_d, medium_newest.id)} bytes")
        print()
        labels_d = {
            medium_older.id: "OLDER (messages 1-100, matches the query)",
            medium_newest.id: "NEWEST (messages 101-200, the cropped prefix)",
        }
        results.append(_run("D: a matching OLDER episode eats the budget first", "postgres", provider_d, labels_d))

        # -------------------------------------------------------------------- C
        db_c = Database(root / "c.db")
        store_c = MemoryStore(db_c, root / "c-memory")
        _thread(db_c, "t", "u")
        big_filler = "x" * 400
        big = store_c.save_episode(
            owner_id="u", thread_id="t", project_id=None,
            start_message_seq=1, end_message_seq=400, source_hash="sha256:big",
            summary=big_filler,
            synopsis=[{"text": big_filler, "source_message_ids": ["m-1"]}],
            decisions=[{"text": big_filler, "source_message_ids": ["m-2"]}],
            open_loops=[{"text": big_filler, "source_message_ids": ["m-3"]}],
        )
        provider_c = MemoryContextProvider(db_c)
        size = _rendered_bytes(db_c, big.id)
        bundle_c = provider_c.select(MemoryContextRequest("u", "t", None, "完全无关的问题"))
        print("C: one episode at the size the summariser is allowed to produce")
        print(f"  episode rendered = {size} bytes vs episode_token_budget = {EPISODE_BUDGET} bytes")
        print(f"  arrived = {bool(bundle_c.episode_ids)}  (rendered memory block = "
              f"{DEFAULT_TOKEN_COUNTER.count_text(bundle_c.rendered)} bytes)")
        print(f"  the episode covering the cropped prefix arrived = {bool(bundle_c.episode_ids)}")
        print()
        results.append({
            "title": "C: episode at the summariser's own output ceiling",
            "selected": list(bundle_c.episode_ids),
            "newest_arrived": bool(bundle_c.episode_ids),
            "episode_bytes": size,
        })

        failures = [r for r in results if not r["newest_arrived"]]
        print("=" * 70)
        print(f"scenarios where the cropped-prefix episode did NOT reach the model: {len(failures)}/{len(results)}")
        for r in failures:
            print(f"  - {r['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
