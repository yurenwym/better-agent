"""DeepSeek context-budget acceptance: 16 real conversation rounds, including ask.

Drives the running application over HTTP with the production worker, database
and model gateway. Records per-turn:

* the local estimate from ``context.counted`` (counter id/version, estimated
  input units, effective input limit, output reserve, canonical payload bytes),
* the provider's actual usage (uncached + cache-read + cache-write + output),
* the turn outcome and assistant answer length.

Run only after the app is up and a real model is configured.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8000"

TURNS = [
    "你好",
    "制定一个7天的桂林旅游计划。我的日期、同行人、预算和偏好尚未提供。请先通过应用的澄清问答表单询问，收到我的回答后再制定，不要自行假设。只需聊天建议，不保存文档或开启执行管理。",
    "同行是情侣，节奏放松一点，不要每天排满。",
    "出行时间大概十月中旬，想去漓江和阳朔。",
    "把第一天的安排再详细一点，上午下午分开写。",
    "第二天加一个雨天备选方案。",
    "预算帮我控制在人均 4000 元以内。",
    "把整个攻略整理成最终版。",
    "LONG_INPUT",
    "谢谢，再帮我把每天的交通方式补上。",
    "如果第三天太赶，帮我调整得轻松些。",
    "最后用一句话概括这份行程。",
    "请核对目前我们确定的出行时间、同行人、预算和节奏，列出四项即可。",
    "请指出这份行程中还需要我出发前核实的营业时间和交通信息，不要编造实时数据。",
    "如果连续两天下雨，给我一个保持七天总时长的简短调整方案。",
    "请交付最终七天行程表：每天一个主活动、交通和雨天备选。保留之前的预算与轻松节奏，500字以内，不保存文件。",
]

LONG_INPUT_BODY = (
    "以下是用户提供的行程笔记，请总结要点并指出需要确认的信息。\n"
    + ("桂林山水以喀斯特地貌闻名，漓江精华段在杨堤到兴坪之间，"
       "十月中旬天气转凉且可能多雨，需要准备雨具和防滑鞋；"
       "阳朔西街晚上热闹，遇龙河竹筏适合清晨，十里画廊适合骑行；"
       "市区象鼻山、两江四湖适合傍晚，龙脊梯田需要单独一天往返。"
       ) * 400
)


def _call(base_url: str, method: str, path: str, payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(base_url + path, data=data, method=method)
    request.add_header("Host", "127.0.0.1:8000")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("Origin", base_url)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body else None)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def _default_answers(ask: dict) -> list[dict]:
    answers = []
    for question in ask["questions"]:
        if question.get("allow_free_text", True):
            answers.append({
                "question_id": question["id"], "selected_options": [],
                "free_text": "2026年10月15日出发共7天；情侣两人；人均4000元；节奏放松；桂林市区加阳朔，想去漓江和遇龙河，避开人多与商业化。",
            })
        elif question.get("options"):
            answers.append({
                "question_id": question["id"],
                "selected_options": [question["options"][0]["label"]],
                "free_text": "",
            })
        else:
            answers.append({
                "question_id": question["id"],
                "selected_options": [],
                "free_text": "按合理默认：十月中旬出发，情侣同行，节奏放松，人均预算 4000 元。",
            })
    return answers


def _drive_turn(base_url: str, thread_id: str, content: str, csrf: str, turn_index: int) -> dict:
    headers = {"X-CSRF-Token": csrf}
    status, accepted = _call(
        base_url, "POST", f"/api/threads/{thread_id}/turns",
        {"client_turn_id": f"budget-accept-{turn_index}-{uuid.uuid4().hex[:8]}", "content": content},
        headers,
    )
    user_turn_id = accepted["turn_id"]
    chain: list[str] = [user_turn_id]
    interactions: list[dict] = []
    active = None
    timed_out = True
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(1.0)
        _, thread = _call(base_url, "GET", f"/api/threads/{thread_id}")
        turns = {item["id"]: item for item in thread.get("turns", [])}
        active_id = thread.get("active_turn_id")
        active = turns.get(active_id) if active_id else None
        if active is None:
            break
        if active["id"] not in chain:
            chain.append(active["id"])
        if active["status"] == "AWAITING_INPUT":
            _, ask = _call(base_url, "GET", f"/api/turns/{active['id']}/ask")
            answers = _default_answers(ask)
            _call(
                base_url, "POST", f"/api/turns/{active['id']}/ask/answer",
                {
                    "expected_version": active["version"],
                    "idempotency_key": f"accept-ask-{active['id']}",
                    "answers": answers,
                },
                headers,
            )
            interactions.append({"turn_id": active["id"], "action": "answer_ask",
                                 "questions": ask["questions"], "answers": answers})
            if len(interactions) >= 5:
                break
            continue
        if active["status"] == "AWAITING_TOOL_APPROVAL":
            _, pending = _call(base_url, "GET", f"/api/turns/{active['id']}/tool-call")
            _call(
                base_url, "POST", f"/api/turns/{active['id']}/tool-call/decision",
                {
                    "action": "reject",
                    "expected_version": active["version"],
                    "idempotency_key": f"accept-tool-{active['id']}",
                },
                headers,
            )
            interactions.append({
                "turn_id": active["id"], "action": "unexpected_tool_rejected",
                "tool": pending["tool_call"]["tool_name"],
            })
            continue
        if active["status"] == "AWAITING_DIRECTION":
            interactions.append({"turn_id": active["id"], "action": "unexpected_direction"})
            break
        if active["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            chain.append(active["id"]) if active["id"] not in chain else None
            timed_out = False
            break
    _, messages = _call(base_url, "GET", f"/api/threads/{thread_id}/messages")
    answers = [item for item in messages.get("messages", [])
               if item["role"] == "assistant" and item.get("turn_id") in chain]
    _, events = _call(base_url, "GET", f"/api/threads/{thread_id}/events")
    diagnostics = [
        {**event.get("data", {}), "type": event["type"], "turn_id": event["turn_id"], "seq": event["seq"]}
        for event in events.get("events", [])
        if event["turn_id"] in chain
        and event["type"] in {"context.counted", "context.request_estimated", "context.continuation_overflow",
                              "ask.requested", "ask.answered", "model.request.estimated"}
    ]
    return {
        "index": turn_index,
        "user_turn_id": user_turn_id,
        "chain_turn_ids": list(dict.fromkeys(chain)),
        "final_status": active["status"] if active else None,
        "timed_out": timed_out,
        "interactions": interactions,
        "assistant_answer_length": len(answers[-1]["content"]) if answers else 0,
        "assistant_answer": answers[-1]["content"] if answers else "",
        "diagnostics": diagnostics,
    }


def _pair_attempt_usage(attempts: list[dict], estimates: dict[str, dict]) -> list[dict]:
    """Keep every request separate; turn totals are spend, not context occupancy."""
    result = []
    for attempt in attempts:
        item = dict(attempt)
        buckets = [item.get(key) for key in ("uncached_input_tokens", "cache_read_tokens", "cache_write_tokens")]
        actual = sum(buckets) if all(value is not None for value in buckets) else None
        estimate = estimates.get(item["attempt_id"])
        item["actual_input_tokens"] = actual
        item["request_estimate"] = estimate
        item["relative_error_pct"] = (
            round(100 * (estimate["estimated_input_tokens"] - actual) / actual, 2)
            if estimate and actual else None
        )
        result.append(item)
    return result


def _load_usage(database_url: str | None, thread_id: str, rows: list[dict]) -> dict:
    if not database_url:
        return {"available": False, "reason": "DATABASE_URL is not configured"}
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        return {"available": False, "reason": "psycopg is not installed"}
    connection = psycopg.connect(database_url, row_factory=dict_row, options="-c default_transaction_read_only=on")
    try:
        per_turn = {}
        events = connection.execute(
            "SELECT data_json FROM thread_events WHERE thread_id=%s AND type='model.request.estimated'",
            (thread_id,),
        ).fetchall()
        estimates = {item["model_attempt_id"]: item for item in
                     (json.loads(event["data_json"]) for event in events)}
        for row in rows:
            placeholders = ",".join("%s" for _ in row["chain_turn_ids"])
            attempts = connection.execute(
                "SELECT i.id invocation_id,i.turn_id,i.role,i.purpose,i.selected_attempt_id,"
                "a.id attempt_id,a.ordinal,a.status,a.error_kind,a.profile_version_id,"
                "a.uncached_input_tokens,a.cache_read_tokens,a.cache_write_tokens,"
                "a.output_tokens,a.reasoning_tokens,a.usage_status "
                "FROM model_invocations i JOIN model_attempts a ON a.invocation_id=i.id "
                f"WHERE i.thread_id=%s AND i.turn_id IN ({placeholders}) ORDER BY i.created_at,a.ordinal",
                (thread_id, *row["chain_turn_ids"]),
            ).fetchall()
            per_turn[row["user_turn_id"]] = _pair_attempt_usage(attempts, estimates)
        active = None
        channel = connection.execute(
            "SELECT bundle_id FROM runtime_channels WHERE name='stable'",
        ).fetchone()
        if channel is not None:
            bundle = connection.execute(
                "SELECT manifest_json FROM runtime_bundles WHERE id=%s", (channel["bundle_id"],),
            ).fetchone()
            manifest = json.loads(bundle["manifest_json"]) if bundle else {}
            policy_id = (manifest.get("model_routing") or {}).get("policy_id")
            policy = connection.execute(
                "SELECT roles_json FROM model_routing_policies WHERE id=%s", (policy_id,),
            ).fetchone() if policy_id else None
            if policy is not None:
                roles = json.loads(policy["roles_json"])
                version_id = (roles.get("conversation") or {}).get("primary")
                if version_id:
                    active = connection.execute(
                        "SELECT context_window,counter_id,counter_version,max_output_tokens,"
                        "capacity_evidence FROM model_profile_versions WHERE id=%s",
                        (version_id,),
                    ).fetchone()
        return {"available": True, "per_turn": per_turn, "active_profile": dict(active) if active else None}
    finally:
        connection.close()


def _acceptance_errors(rows: list[dict], usage_available: bool) -> list[str]:
    errors = []
    if len(rows) < 15:
        errors.append("fewer than 15 conversation rounds")
    if not usage_available:
        errors.append("provider usage unavailable")
    for row in rows:
        if row["final_status"] != "COMPLETED" or row.get("timed_out") or not row.get("assistant_answer_length"):
            errors.append(f"round {row['index']} did not deliver a completed answer")
        if any(item["action"] != "answer_ask" for item in row["interactions"]):
            errors.append(f"round {row['index']} had unexpected interaction")
        main_calls = [a for a in row.get("provider_attempts", [])
                      if a["purpose"] == "route_and_respond" and a["status"] == "SUCCEEDED"]
        if not main_calls or any(a["actual_input_tokens"] is None or not a["request_estimate"] for a in main_calls):
            errors.append(f"round {row['index']} missing paired final-request usage")
        if any(d["type"] == "context.continuation_overflow" for d in row["diagnostics"]):
            errors.append(f"round {row['index']} overflowed")
    ask_rows = [row for row in rows if any(i["action"] == "answer_ask" for i in row["interactions"])]
    if not any(row["final_status"] == "COMPLETED" and len(row["chain_turn_ids"]) > 1
               and {"ask.requested", "ask.answered"} <= {d["type"] for d in row["diagnostics"]}
               for row in ask_rows):
        errors.append("no completed ask form continuation with event evidence")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if args.out.exists():
        parser.error(f"output directory already exists: {args.out}")
    args.out.mkdir(parents=True)

    _, bootstrap = _call(args.base_url, "GET", "/api/bootstrap")
    csrf = bootstrap["csrf_token"]
    _, thread = _call(args.base_url, "POST", "/api/threads", {"title": "预算修复验收"}, {"X-CSRF-Token": csrf})
    thread_id = thread["id"]

    rows = []
    for index, content in enumerate(TURNS, start=1):
        if content == "LONG_INPUT":
            content = LONG_INPUT_BODY
        row = _drive_turn(args.base_url, thread_id, content, csrf, index)
        row["input_chars"] = len(content)
        row["user_content"] = content
        rows.append(row)
        (args.out / "progress.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"turn {index}: {row['final_status']} answer_chars={row['assistant_answer_length']} "
              f"diagnostics={len(row['diagnostics'])} interactions={len(row['interactions'])}", flush=True)

    usage = _load_usage(args.database_url, thread_id, rows)
    if usage.get("available"):
        for row in rows:
            item = usage["per_turn"].get(row["user_turn_id"], {})
            row["provider_attempts"] = item
    errors = _acceptance_errors(rows, usage.get("available", False))
    report = {
        "status": "passed" if not errors else "failed",
        "acceptance_errors": errors,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "thread_id": thread_id,
        "turn_count": len(rows),
        "rows": rows,
        "provider_usage_available": usage.get("available", False),
        "provider_usage_note": usage.get("reason"),
        "active_profile": usage.get("active_profile"),
        "limitations": [
            "Input usage is per attempt, paired with that attempt's final wire estimate. Cached input is included.",
            "One run only; this is a budget acceptance, not a production reliability claim.",
        ],
    }
    (args.out / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({
        "turns": report["turn_count"],
        "completed": sum(1 for row in rows if row["final_status"] == "COMPLETED"),
        "failed": sum(1 for row in rows if row["final_status"] != "COMPLETED"),
        "out": str(args.out / "results.json"),
    }, ensure_ascii=False))
    if errors:
        print(json.dumps({"acceptance_errors": errors}, ensure_ascii=False), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
