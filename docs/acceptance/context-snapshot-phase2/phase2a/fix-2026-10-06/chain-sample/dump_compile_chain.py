"""Produce the fix-round redacted chain sample for the Phase 2A repair evidence pack.

The sample has to show two things the fix round is about:

1. a compile and its JSON repair are **two logical calls** - two invocations, two
   distinct spans, both hanging off the same parent (the tool) span;
2. a **transport retry stays inside one logical call** - the retried call has two
   attempts that share one invocation, one span and one frozen input.

Both are produced by driving the *real* ``GoalProgramCompiler`` through the real
``ModelGateway``, with the network scripted at the wire (``httpx.MockTransport``):
attempt 1 dies with a 504, attempt 2 returns unusable JSON, attempt 3 returns a
valid program.  Only identifiers, digests, lengths and statuses are printed -
never the frozen prompt text.

Run:
    cd backend
    "D:/pycharm/python.exe" "D:/RAG/better/docs/acceptance/context-snapshot-phase2/phase2a/fix-2026-10-06/chain-sample/dump_compile_chain.py"
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import sys
import tempfile

BACKEND = r"D:\RAG\better\backend"
sys.path.insert(0, BACKEND)
sys.path.insert(0, BACKEND + r"\tests")

import httpx  # noqa: E402

from test_snapshot_gateway import _configured_control_plane, _registered_profile  # noqa: E402

SOURCE_MARKDOWN = "# 计划\n每天跑步。"
REQUEST = {"start_date": "2026-09-01", "end_date": "2026-09-05", "daily_minutes": 60}


def short(value, size: int = 24) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= size else f"{text[:size]}…"


def _sse(content: str) -> bytes:
    return (
        "data: " + json.dumps({"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]})
        + "\n\ndata: [DONE]\n\n"
    ).encode()


async def main() -> int:
    from dataclasses import replace

    from app.execution_context import create_root_context
    from app.goal_program_compiler import GoalProgramCompiler
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from test_harness_context_flow import _program_fixture, stored_context

    monkeypatch = __import__("pytest").MonkeyPatch()
    tmp_path = pathlib.Path(tempfile.mkdtemp(dir="C:/tmp", prefix="compile-chain-"))

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    control = ModelControlStore(db)
    profile = replace(
        _registered_profile(db, versions["planner"]),
        max_attempts=2, network_retries=1, retry_base_seconds=0,
    )
    wire: list = []

    def handler(request):
        wire.append(request)
        if len(wire) == 1:
            # The first attempt dies on the wire, so the first logical call retries.
            return httpx.Response(504, json={"error": "upstream"})
        if len(wire) == 2:
            return httpx.Response(200, content=_sse("这不是 JSON"))
        return httpx.Response(200, content=_sse(json.dumps(_program_fixture(), ensure_ascii=False)))

    async def no_sleep(_seconds):
        return None

    gateway = ModelGateway(
        profile, transport=httpx.MockTransport(handler), sleep=no_sleep, control_store=control,
    )
    harness = create_root_context(owner_id="local-user", runtime_bundle_id=bundle.id)
    token = gateway.set_call_context(
        ModelCallContext.from_harness(harness, role="planner", purpose="compile_goal_program")
    )
    try:
        result = await GoalProgramCompiler(gateway).compile(SOURCE_MARKDOWN, REQUEST)
    finally:
        gateway.reset_call_context(token)

    store = ModelInputSnapshotStore(db)
    with db.connection() as connection:
        invocations = connection.execute(
            "SELECT id, owner_id, role, purpose, runtime_bundle_id, status, request_digest, "
            "execution_context_digest, execution_context_json, context_snapshot_id, "
            "context_snapshot_digest, idempotency_key FROM model_invocations "
            "ORDER BY created_at, id"
        ).fetchall()
        attempts = connection.execute(
            "SELECT id, invocation_id, ordinal, reason, status, request_digest "
            "FROM model_attempts ORDER BY ordinal, id"
        ).fetchall()

    lines: list[str] = []
    add = lines.append
    add("# 脱敏链路样本（修复轮）：编译 / 修复两个 span，retry 共用同一快照")
    add("")
    add("由 `dump_compile_chain.py` 驱动**真实 `GoalProgramCompiler` + 真实 `ModelGateway`** 产生；")
    add("传输层被脚本化（`httpx.MockTransport`）：attempt 1 返回 504，attempt 2 返回非法 JSON，attempt 3 返回合法 program。")
    add("调用带真实的根执行上下文（`create_root_context`），模拟工具内部的编译调用。")
    add("只打印标识符、摘要、长度与状态，**不打印冻结的提示词正文**。")
    add("")
    add(f"- 网络 attempt 数：**{len(wire)}**")
    add(f"- 逻辑调用数（invocation）：**{len(invocations)}**")
    add(f"- attempt 记录数：**{len(attempts)}**")
    add(f"- 编译结果：objective_title = `{short(result.get('objective_title'), 20)}`")
    add("")

    spans: list[dict] = []
    ok = True
    for row in invocations:
        snapshot = store.load(row["owner_id"], row["context_snapshot_id"])
        raw = snapshot.content_json.encode("utf-8")
        recomputed = hashlib.sha256(raw).hexdigest()
        digest_matches = recomputed == snapshot.content_digest
        binding_matches = row["context_snapshot_digest"] == snapshot.content_digest
        context = stored_context(row)
        spans.append(context)
        ok = ok and digest_matches and binding_matches

        add(f"## invocation `{short(row['id'], 40)}`")
        add("")
        add("| 字段 | 值 |")
        add("|---|---|")
        add(f"| role / purpose | `{row['role']}` / `{row['purpose']}` |")
        add(f"| status | `{row['status']}` |")
        add(f"| idempotency_key | `{short(row['idempotency_key'], 32)}` |")
        add(f"| request_digest | `{short(row['request_digest'])}` |")
        add(f"| **span_id** | `{short(context['span_id'])}` |")
        add(f"| **parent_span_id** | `{short(context['parent_span_id'])}` |")
        add(f"| trace_id | `{short(context['trace_id'])}` |")
        add(f"| execution_context_digest | `{short(row['execution_context_digest'])}` |")
        add(f"| **context_snapshot_id** | `{row['context_snapshot_id']}` |")
        add(f"| **context_snapshot_digest** | `{short(row['context_snapshot_digest'])}` |")
        add(f"| 规范化 JSON 字节数 | {len(raw)} |")
        add(f"| provenance.status | `{snapshot.provenance().status}` |")
        add(f"| 重新计算 SHA-256 == content_digest | {'✅' if digest_matches else '❌'} |")
        add(f"| invocation.context_snapshot_digest == snapshot.content_digest | {'✅' if binding_matches else '❌'} |")
        add("")
        add("### attempts")
        add("")
        add("| ordinal | reason | status | attempt id | request_digest |")
        add("|---|---|---|---|---|")
        mine = [item for item in attempts if item["invocation_id"] == row["id"]]
        for attempt in mine:
            add(
                f"| {attempt['ordinal']} | `{attempt['reason']}` | `{attempt['status']}` | "
                f"`{short(attempt['id'])}` | `{short(attempt['request_digest'])}` |"
            )
        add("")

    first_span = spans[0]["span_id"] if spans else None
    second_span = spans[1]["span_id"] if len(spans) > 1 else None
    parents = {context["parent_span_id"] for context in spans}
    traces = {context["trace_id"] for context in spans}
    digests = [row["context_snapshot_digest"] for row in invocations]
    first_attempts = [a for a in attempts if a["invocation_id"] == invocations[0]["id"]] if invocations else []

    add("## 结论")
    add("")
    add(f"- 两个逻辑调用（编译 / 修复）span 互不相同：{'✅' if first_span and first_span != second_span else '❌'}")
    add(f"- 两个 span 的 parent_span_id 都是根上下文 span（`{short(harness.span_id)}`）："
        f"{'✅' if parents == {harness.span_id} else '❌'}")
    add(f"- 两个 span 共用同一 trace_id：{'✅' if len(traces) == 1 and None not in traces else '❌'}")
    add(f"- 两个逻辑调用各自绑定不同快照：{'✅' if len(set(digests)) == len(digests) else '❌'}")
    add(f"- 首个逻辑调用的 {len(first_attempts)} 个 attempt 共用同一 invocation 与同一快照："
        f"{'✅' if len(first_attempts) >= 2 and len({a['request_digest'] for a in first_attempts}) == 1 else '❌'}")
    add(f"- 快照摘要自校验与绑定一致：{'✅' if ok else '❌'}")
    add("- 本文件不含提示词正文，仅含摘要与标识符。")
    add("")

    target = pathlib.Path(__file__).with_name("chain-compile.md")
    target.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten -> {target}")
    monkeypatch.undo()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
