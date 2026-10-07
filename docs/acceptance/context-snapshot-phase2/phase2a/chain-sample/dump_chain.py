"""Produce one redacted chain sample for the Phase 2A evidence pack.

Chain required by the task book section 7:
    invocation -> execution digest -> snapshot id/digest -> attempts

The sample is produced by driving a *real* minimal-chain conversation call
through the real routed gateway (the "network" is a scripted function), then
reading the committed rows back out of the database.  Only identifiers,
digests, sizes and statuses are printed - never the frozen user text.

Run:
    cd backend
    "D:/pycharm/python.exe" "D:/RAG/better/docs/acceptance/context-snapshot-phase2/phase2a/chain-sample/dump_chain.py"
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

from app.model_gateway import GatewayError  # noqa: E402
from test_snapshot_gateway import _answer, _configured_control_plane  # noqa: E402

USER_TEXT = "今天要做什么？"


def short(value, size: int = 16) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= size else f"{text[:size]}…"


async def main() -> int:
    from app.live_model import LiveConversationModel
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_input_snapshot_store import ModelInputSnapshotStore

    monkeypatch = __import__("pytest").MonkeyPatch()
    tmp_path = pathlib.Path(tempfile.mkdtemp(dir="C:/tmp", prefix="chain-sample-"))

    calls = {"n": 0}

    async def execute(profile, request, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            # The first attempt dies on the wire, so the call has to retry.
            raise GatewayError("temporary upstream failure", "timeout")
        return _answer('{"v":1,"policy":"answer"}\n今天没有安排。')

    db, bundle, _versions = _configured_control_plane(
        tmp_path, monkeypatch, max_attempts={"chat": 2},
    )
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    model = LiveConversationModel(gateway)

    # A real thread/turn root, so this call carries an execution identity instead
    # of the harness-less "no execution digest" shape.
    from test_snapshot_flow import _turn_context

    harness = _turn_context(db)

    deltas: list[str] = []
    await model.route_and_respond(
        content=USER_TEXT,
        history=[{"role": "user", "content": USER_TEXT}],
        skill_names=[],
        on_text_delta=deltas.append,
        on_text_reset=lambda: None,
        cancel_event=asyncio.Event(),
        harness=harness,
    )

    store = ModelInputSnapshotStore(db)
    with db.connection() as connection:
        invocations = connection.execute(
            "SELECT id, owner_id, role, purpose, runtime_bundle_id, status, "
            "request_digest, execution_context_digest, execution_context_json, "
            "context_snapshot_id, context_snapshot_digest, selected_attempt_id, "
            "idempotency_key FROM model_invocations ORDER BY created_at, id"
        ).fetchall()
        attempts = connection.execute(
            "SELECT id, invocation_id, ordinal, reason, status, provider_protocol, "
            "provider_request_id, request_digest, started_at, finished_at "
            "FROM model_attempts ORDER BY ordinal, id"
        ).fetchall()

    lines: list[str] = []
    add = lines.append
    add("# 脱敏链路样本：invocation → execution digest → snapshot → attempts")
    add("")
    add("由 `dump_chain.py` 驱动一次**真实最小链路对话调用**（对话 → LLM）产生；")
    add("调用带真实的 turn 根上下文（`create_root_context`），因此有执行身份摘要。")
    add("第一次 attempt 被脚本化的传输层打死（`GatewayError(timeout)`），因此该调用有 2 个 attempt。")
    add("只打印标识符、摘要、长度与状态，**不打印冻结的用户正文**。")
    add("")
    add(f"- 逻辑调用数（invocation）：**{len(invocations)}**")
    add(f"- attempt 数：**{len(attempts)}**")
    add("")

    ok = True
    for row in invocations:
        snapshot = store.load(row["owner_id"], row["context_snapshot_id"])
        raw = snapshot.content_json.encode("utf-8") if isinstance(snapshot.content_json, str) else bytes(snapshot.content_json)
        recomputed = hashlib.sha256(raw).hexdigest()
        digest_matches = recomputed == snapshot.content_digest
        binding_matches = row["context_snapshot_digest"] == snapshot.content_digest
        holds_user_text = USER_TEXT in (snapshot.content_json if isinstance(snapshot.content_json, str) else snapshot.content_json.decode("utf-8"))
        has_execution_digest = bool(row["execution_context_digest"])
        ok = ok and digest_matches and binding_matches and holds_user_text and has_execution_digest
        add(f"## invocation `{short(row['id'], 40)}`")
        add("")
        add("| 字段 | 值 |")
        add("|---|---|")
        add(f"| id | `{row['id']}` |")
        add(f"| owner_id | `{row['owner_id']}` |")
        add(f"| role / purpose | `{row['role']}` / `{row['purpose']}` |")
        add(f"| runtime_bundle_id | `{short(row['runtime_bundle_id'], 24)}` |")
        add(f"| status | `{row['status']}` |")
        add(f"| idempotency_key | `{row['idempotency_key']}` |")
        add(f"| request_digest | `{short(row['request_digest'], 24)}` |")
        add(f"| **execution_context_digest** | `{short(row['execution_context_digest'], 24)}` |")
        add(f"| execution_context_json 已持久化 | {'✅' if row['execution_context_json'] else '❌'} |")
        add(f"| **context_snapshot_id** | `{row['context_snapshot_id']}` |")
        add(f"| **context_snapshot_digest** | `{short(row['context_snapshot_digest'], 24)}` |")
        add(f"| selected_attempt_id | `{row['selected_attempt_id']}` |")
        add("")
        add("### 冻结输入（快照）")
        add("")
        add("| 字段 | 值 |")
        add("|---|---|")
        add(f"| snapshot id | `{snapshot.id}` |")
        add(f"| schema_version | `{snapshot.schema_version}` |")
        add(f"| content_digest | `{short(snapshot.content_digest, 24)}` |")
        add(f"| 规范化 JSON 字节数 | {len(raw)} |")
        add(f"| provenance.status | `{snapshot.provenance().status}` |")
        add(f"| 重新计算 SHA-256 == content_digest | {'✅' if digest_matches else '❌'} |")
        add(f"| invocation.context_snapshot_digest == snapshot.content_digest | {'✅' if binding_matches else '❌'} |")
        add(f"| 快照内含本次用户输入（未打印正文） | {'✅' if holds_user_text else '❌'} |")
        add("")
        add("### attempts")
        add("")
        add("| ordinal | reason | status | provider_protocol | attempt id | request_digest |")
        add("|---|---|---|---|---|---|")
        for attempt in attempts:
            if attempt["invocation_id"] != row["id"]:
                continue
            add(
                f"| {attempt['ordinal']} | `{attempt['reason']}` | `{attempt['status']}` | "
                f"`{attempt['provider_protocol']}` | `{short(attempt['id'], 24)}` | "
                f"`{short(attempt['request_digest'], 24)}` |"
            )
        add("")

    shared = {a["invocation_id"] for a in attempts}
    add("## 结论")
    add("")
    add(f"- 两个 attempt 同属一个 invocation：{'✅' if shared == {r['id'] for r in invocations} else '❌'}")
    add(f"- 执行身份摘要非空：{'✅' if all(r['execution_context_digest'] for r in invocations) else '❌'}")
    add(f"- 快照摘要自校验与绑定一致：{'✅' if ok else '❌'}")
    add("- 本文件不含用户正文，仅含摘要与标识符。")
    add("")

    target = pathlib.Path(__file__).with_name("chain.md")
    target.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten -> {target}")
    monkeypatch.undo()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
