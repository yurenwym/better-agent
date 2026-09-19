"""Run the separately authorised R1/R2 live acceptance batch.

``app.context_replay`` proves the priority fix offline, against a deterministic
stand-in archiver.  This batch closes the two gaps that replay documents:

1. ``boundary`` - a request whose counted size sits exactly on ``H`` must be
   **accepted** by the provider.  The counter is a UTF-8 byte upper bound, so
   the real risk is refusing a request the provider would have taken.  The live
   reply also returns the provider's own prompt-token count, which is the first
   honest measurement of what the byte bound costs.
2. ``summariser`` - the real model produces the Episode summary, so the
   conclusion no longer depends on my own summariser preserving the facts.
3. ``probe`` - the real model answers the seven probes from the packed request,
   for both the pre-R1 and post-R1 priority.  This is the live version of the
   offline 1/7 vs 7/7 result.

The default invocation is a no-network preflight.  A live batch requires both
``--execute`` and ``R1_LIVE_APPROVED=1`` so ordinary development commands can
never spend money accidentally.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import load_llm_ap
from app.context_replay import (
    PROBES,
    archive_input,
    assemble_with_summary,
    render_summary,
)
from app.model_control import RoutedModelGateway
from app.model_gateway import ModelGateway, ModelRequest
from app.token_budget import (
    ContextOverflow,
    assert_provider_payload_fits,
    effective_input_budget,
    envelope_overhead,
    hot_window,
    packing_limit,
    per_message_overhead,
    per_tool_overhead,
    strip_packing_hints,
)

BATCH_ID = "R1-LIVE-20260914-001"

# Hard batch limits. Six attempts covers boundary + summariser + two probes with
# one spare; nothing here may grow into an unbounded spend.
MAX_ATTEMPTS = 6
MAX_SECONDS = 300

# The frozen acceptance configuration: the legacy 32k/8k profile's input budget,
# the R1 min_turns floor, and the two priorities under test. Both arms use the
# same budget so the priority is the only difference.
ACCEPTANCE_BUDGET = 23_348
SUMMARY_PRIORITY_POST = 60
SUMMARY_PRIORITY_PRE = 20
MIN_TURNS = 5

SUMMARISER_SYSTEM = (
    "你是会话归档器。只输出一个 JSON 对象，不要输出解释、前后缀或代码块标记。"
)
SUMMARISER_SCHEMA = {
    "synopsis": "字符串，概述这段对话发生了什么",
    "decisions": ["已确定的决定"],
    "outcomes": ["已确认的结果"],
    "open_loops": ["尚未解决的问题"],
    "facts": {"主题名": "该主题当前最新的取值"},
}

PROBE_QUESTIONS: dict[str, str] = {
    "constraint": "总花费有什么硬约束？",
    "latest_value": "当前预算上限是多少元？",
    "confirmed_param": "出发日期是哪天？",
    "tool_result": "查到的杭州高铁票价是多少元？",
    "failed_attempt": "哪个方案已经失败过？",
    "open_question": "还有什么问题没有定下来？",
    "recent_turns": "这次出发一共几个人？",
}

# A real model paraphrases, so the live check accepts a set of phrasings while
# still refusing a superseded value. The offline containment check stays exact
# string matching - that one measures the packing layer, not the model.
LIVE_ACCEPT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "constraint": (("不能超过预算", "不超过预算", "不能超预算", "不得超预算", "总花费不能超过", "不得超出预算"), ()),
    "latest_value": (("8000",), ("5000",)),
    "confirmed_param": (("3月12日",), ()),
    "tool_result": (("553",), ()),
    "failed_attempt": (("A方案",), ()),
    "open_question": (("住宿",), ()),
    "recent_turns": (("4人",), ()),
}
OPEN_QUESTION_MARKERS = ("没定", "未定", "还没", "没有决定", "待定", "未决", "悬而未决", "尚未")


class AcceptanceFailure(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


def _llm_ap_path() -> Path:
    explicit = os.getenv("LLM_AP_PATH") or os.getenv("R1_LLM_AP_PATH")
    if explicit:
        return Path(explicit)
    # Mirrors app.eval.DEFAULT_LLM_AP. Imported lazily because app.eval pulls in
    # the evaluation suite, which is not needed for a budget acceptance run.
    return Path(r"D:\Users\王一鸣\Desktop\直到尽头\LLM_AP.txt")


def _profile() -> Any:
    path = _llm_ap_path()
    _require(path.exists(), f"live credential file is missing: {path}")
    from dataclasses import replace

    profile = load_llm_ap(path)
    _require(
        profile.context_window == 32_768 and profile.max_output_tokens == 8_192,
        f"acceptance requires the frozen 32768/8192 profile, got "
        f"{profile.context_window}/{profile.max_output_tokens}",
    )
    _require(
        profile.provider_protocol == "openai_compatible",
        f"acceptance requires the openai_compatible adapter, got {profile.provider_protocol}",
    )
    # One attempt, no network retries: a retry would hide a rejection and spend
    # money twice for the same signal.
    return replace(profile, max_attempts=1, network_retries=0, timeout_seconds=120)


# --------------------------------------------------------------------------- #
# Attempt ledger
# --------------------------------------------------------------------------- #


class BatchBudget:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.attempts: list[dict[str, Any]] = []

    def remaining(self) -> float:
        left = MAX_SECONDS - (time.monotonic() - self.started)
        if left <= 0:
            raise AcceptanceFailure("batch duration exceeded")
        return left

    def reserve(self, case: str) -> dict[str, Any]:
        self.remaining()
        if len(self.attempts) >= MAX_ATTEMPTS:
            raise AcceptanceFailure("batch attempt hard limit reached before network request")
        record: dict[str, Any] = {"ordinal": len(self.attempts) + 1, "case": case, "status": "started"}
        self.attempts.append(record)
        return record


# --------------------------------------------------------------------------- #
# Exact wire accounting, without sending anything
# --------------------------------------------------------------------------- #


async def _dry_payload(profile: Any, request: ModelRequest) -> dict[str, Any]:
    """Capture the exact body the adapter would send, using a stub transport.

    Reimplementing the adapter's payload dict here would drift. Driving the real
    adapter against a mock transport keeps one source of truth: whatever the
    wire gate counts in production is what this measures.
    """
    captured: dict[str, Any] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
        )

    gateway = ModelGateway(profile, transport=httpx.MockTransport(handler))
    await gateway._attempt(request, "dry-run", None, None, None)
    _require("payload" in captured, "dry run never built a provider payload")
    return captured["payload"]


async def _gate_units(profile: Any, request: ModelRequest) -> int:
    """The exact number the wire gate compares against ``H``. No network."""
    payload = await _dry_payload(profile, request)
    try:
        return assert_provider_payload_fits(payload, profile)
    except ContextOverflow as exc:
        found = re.search(r"requires (\d+) conservative budget units", str(exc))
        return int(found.group(1)) if found else -1


async def _live(profile: Any, request: ModelRequest, budget: BatchBudget, case: str) -> dict[str, Any]:
    """Send one request over the real network and record everything about it."""
    record = budget.reserve(case)
    limit = effective_input_budget(profile).input_limit
    units = await _gate_units(profile, request)
    record.update({
        "gate_units": units,
        "input_limit": limit,
        "headroom_units": limit - units,
        "messages": len(request.messages),
        "request_digest": _digest({
            "messages": strip_packing_hints(request.messages),
            "tools": request.tools or [],
            "max_tokens": request.max_tokens,
            "thinking": request.thinking,
        }),
    })
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            RoutedModelGateway._execute_http_attempt(profile, request),
            timeout=min(float(profile.timeout_seconds), budget.remaining()),
        )
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        record["status"] = "failed"
        record["error_kind"] = getattr(exc, "kind", type(exc).__name__)
        record["error"] = str(exc)[:400]
        raise
    record["status"] = "succeeded"
    record["latency_seconds"] = round(time.monotonic() - started, 3)
    record["finish_reason"] = response.finish_reason
    record["reply_bytes"] = len(response.message.encode("utf-8"))
    usage = response.usage
    record["usage"] = {
        "input_tokens": usage.input_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "output_tokens": usage.output_tokens,
    }
    record["reply"] = response.message
    return record


# --------------------------------------------------------------------------- #
# Case 1: the boundary must be accepted, never falsely refused
# --------------------------------------------------------------------------- #


def _boundary_request(padding: int) -> ModelRequest:
    return ModelRequest(
        messages=[
            {
                "role": "system",
                "content": "以下是无意义的填充数据，请完全忽略：" + "x" * padding,
            },
            {"role": "user", "content": "请只回复两个字：收到"},
        ],
        tools=[],
        temperature=0,
        max_tokens=16,
        role="conversation",
        purpose="r1_boundary",
        thinking=False,
    )


async def _run_boundary(profile: Any, budget: BatchBudget) -> dict[str, Any]:
    """Pad until the wire gate sits exactly on ``H``, then send it for real.

    Padding is inert ASCII, so the arithmetic is exact: one ASCII byte of
    content is one counted unit in the serialised payload.
    """
    limit = effective_input_budget(profile).input_limit
    padding = 0
    units = 0
    request = _boundary_request(0)
    for _ in range(5):
        request = _boundary_request(padding)
        units = await _gate_units(profile, request)
        if units < 0:
            padding = max(0, padding - 4096)
            continue
        if units >= limit:
            break
        padding += limit - units
    else:
        raise AcceptanceFailure("could not place the boundary request on H")

    _require(units == limit, f"boundary request landed at {units}, not on H={limit}")
    attempt = await _live(profile, request, budget, "boundary")
    attempt["padding_bytes"] = padding
    attempt["on_boundary"] = True
    attempt["accepted"] = attempt["status"] == "succeeded" and bool(attempt["reply"].strip())
    _require(attempt["accepted"], "the provider refused a request our own gate accepted")
    return attempt


# --------------------------------------------------------------------------- #
# Case 2: the real summariser produces the Episode
# --------------------------------------------------------------------------- #


def _archive_transcript(turns: list[Any]) -> str:
    lines: list[str] = []
    for turn in turns:
        for role, content, _call_id, event_type in turn:
            if event_type == "tool_call":
                continue
            if event_type == "tool_result":
                lines.append(f"[工具结果] {content}")
            else:
                lines.append(f"[{'用户' if role == 'user' else '助手'}] {content}")
    return "\n".join(lines)


def _summariser_prompt(transcript: str) -> str:
    schema = json.dumps(SUMMARISER_SCHEMA, ensure_ascii=False, indent=2)
    return (
        "请把下面的历史对话归档成一个 JSON 对象，字段与类型如下：\n"
        f"{schema}\n\n"
        "规则：\n"
        "1. facts 只记录每个主题**当前最新**的取值；被后续对话覆盖的旧值不要写入。\n"
        "2. 尚未解决的事项放进 open_loops，不要写成已确认。\n"
        "3. 只依据对话内容，不要补充对话里没有的信息。\n"
        "4. 只输出 JSON 对象本身。\n\n"
        f"历史对话：\n{transcript}"
    )


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Lenient JSON extraction: real models still add fences or prose."""
    candidate = str(text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.S)
    if fence:
        candidate = fence.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


async def _run_summariser(profile: Any, budget: BatchBudget) -> dict[str, Any]:
    turns = archive_input()
    transcript = _archive_transcript(turns)
    request = ModelRequest(
        messages=[
            {"role": "system", "content": SUMMARISER_SYSTEM},
            {"role": "user", "content": _summariser_prompt(transcript)},
        ],
        tools=[],
        temperature=0,
        max_tokens=1200,
        role="conversation",
        purpose="r1_summariser",
        thinking=False,
    )
    attempt = await _live(profile, request, budget, "summariser")
    attempt["archive_turns"] = len(turns)
    attempt["archive_bytes"] = len(transcript.encode("utf-8"))
    parsed = _parse_json_object(attempt["reply"])
    attempt["parsed"] = parsed is not None
    attempt["summary_text"] = render_summary(parsed) if parsed is not None else ""
    attempt["summary_bytes"] = len(attempt["summary_text"].encode("utf-8"))
    attempt["summary"] = parsed
    _require(parsed is not None, "the real summariser did not return a JSON object")
    _require(bool(attempt["summary_text"]), "the real summariser returned an empty summary")
    return attempt


# --------------------------------------------------------------------------- #
# Case 3: behavioural probes, pre-R1 vs post-R1
# --------------------------------------------------------------------------- #


def _probe_question() -> str:
    lines = ["请只依据上面的上下文回答下面的问题。输出一个 JSON 对象，键是问题编号，值是答案："]
    for probe in PROBES:
        lines.append(f"- {probe.probe_id}：{PROBE_QUESTIONS[probe.probe_id]}")
    lines.append("如果上下文里确实没有相关信息，就回答“不知道”，不要猜测。")
    return "\n".join(lines)


def _score_live(answers: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for probe in PROBES:
        raw = answers.get(probe.probe_id)
        answer = _norm(raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
        any_of, none_of = LIVE_ACCEPT[probe.probe_id]
        matched = any(token in answer for token in any_of)
        clean = not any(token in answer for token in none_of)
        passed = matched and clean
        if probe.probe_id == "open_question":
            passed = passed and any(marker in answer for marker in OPEN_QUESTION_MARKERS)
        results.append({
            "probe_id": probe.probe_id,
            "dimension": probe.dimension,
            "answer": raw,
            "matched": matched,
            "clean": clean,
            "passed": passed,
        })
    return results


def _payload_text(messages: list[dict[str, Any]]) -> str:
    """The context as the model reads it: message by message, oldest first."""
    parts: list[str] = []
    for message in messages:
        parts.append(str(message.get("content", "")))
        for call in message.get("tool_calls") or []:
            parts.append(json.dumps(call, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def _containment_probes(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whitespace-insensitive containment of every probe fact in the payload.

    ``context_replay.run_probes`` compares against the fixture's exact strings,
    which is right when the summary comes from the deterministic archiver. A real
    model writes "3月12日" where the fixture says "3 月 12 日", so this check
    normalises whitespace instead of reporting a formatting difference as a lost
    fact. The superseded-value rule is unchanged: a stale value may only
    contaminate when it appears *after* the current one.
    """
    text = _norm(_payload_text(messages))
    results: list[dict[str, Any]] = []
    for probe in PROBES:
        expected = _norm(probe.expected)
        last_expected = text.rfind(expected)
        contaminated = [
            value for value in probe.superseded
            if _norm(value) in text and text.rfind(_norm(value)) > last_expected
        ]
        results.append({
            "probe_id": probe.probe_id,
            "dimension": probe.dimension,
            "passed": last_expected >= 0 and not contaminated,
            "retained": last_expected >= 0,
            "contaminated": contaminated,
        })
    return results


async def _run_probe(
    profile: Any,
    budget: BatchBudget,
    *,
    summary_text: str,
    summary_priority: int,
    case: str,
) -> dict[str, Any]:
    question = _probe_question()
    # The reservation depends on how large the candidate request is, so measure
    # it against the full candidate set (the uncompressed reference) - an upper
    # bound for any subset the selector may choose.
    candidate = assemble_with_summary(
        summary_text, summary_priority=summary_priority, budget=None, min_turns=MIN_TURNS,
        newest_user=question,
    )
    packing = packing_limit(profile, message_count=len(candidate))
    messages = assemble_with_summary(
        summary_text,
        summary_priority=summary_priority,
        budget=packing,
        min_turns=MIN_TURNS,
        newest_user=question,
    )
    # The packing layer is checked exactly, offline: containment is string
    # matching, which is the right precision for "did the selector keep it".
    # Compare message contents, not the serialised payload - JSON escapes the
    # summary's newlines, so a raw substring test against the dump is wrong.
    offline = _containment_probes(messages)
    summary_present = any(message.get("content") == summary_text for message in messages)

    request = ModelRequest(
        messages=messages,
        tools=[],
        temperature=0,
        max_tokens=900,
        role="conversation",
        purpose=case,
        thinking=False,
    )
    attempt = await _live(profile, request, budget, case)
    answers = _parse_json_object(attempt["reply"])
    attempt.update({
        "summary_priority": summary_priority,
        "packing_budget": packing,
        "candidate_messages": len(candidate),
        "input_limit": effective_input_budget(profile).input_limit,
        "summary_present_in_payload": summary_present,
        "offline_passed": sum(1 for item in offline if item["passed"]),
        "offline_total": len(offline),
        "offline_probes": offline,
        "parsed": answers is not None,
    })
    if answers is None:
        attempt["live_passed"] = 0
        attempt["live_total"] = len(PROBES)
        attempt["live_probes"] = []
        return attempt
    scored = _score_live(answers)
    attempt["answers"] = answers
    attempt["live_probes"] = scored
    attempt["live_passed"] = sum(1 for item in scored if item["passed"])
    attempt["live_total"] = len(scored)
    return attempt


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #


def _verdict(report: dict[str, Any]) -> dict[str, Any]:
    """Hard gates only. A mechanism gain that does not change behaviour fails."""
    boundary = report["cases"]["boundary"]
    summariser = report["cases"]["summariser"]
    post = report["cases"]["probe_post_r1"]
    pre = report["cases"]["probe_pre_r1"]

    if not boundary.get("accepted"):
        return {"passed": False, "reason": "the provider refused a request our gate accepted"}
    if not summariser.get("parsed"):
        return {"passed": False, "reason": "the real summariser did not return a parseable summary"}
    if not post.get("summary_present_in_payload"):
        return {"passed": False, "reason": "post-R1 payload does not carry the summary"}
    if post.get("offline_passed") != post.get("offline_total"):
        missing = [item["probe_id"] for item in post["offline_probes"] if not item["passed"]]
        return {"passed": False, "reason": f"post-R1 packing lost facts: {missing}"}
    if pre.get("summary_present_in_payload"):
        return {"passed": False, "reason": "pre-R1 payload unexpectedly carries the summary; the arms are not distinct"}
    if post.get("live_passed", 0) <= pre.get("live_passed", 0):
        return {
            "passed": False,
            "reason": (
                f"the priority fix did not change real behaviour: "
                f"post-R1 {post.get('live_passed')}/{post.get('live_total')} "
                f"vs pre-R1 {pre.get('live_passed')}/{pre.get('live_total')}"
            ),
        }
    return {
        "passed": True,
        "reason": (
            f"boundary accepted at H with no false rejection; real summariser retained the facts; "
            f"post-R1 answered {post.get('live_passed')}/{post.get('live_total')} probes vs "
            f"pre-R1 {pre.get('live_passed')}/{pre.get('live_total')}"
        ),
        "live_gain": post.get("live_passed", 0) - pre.get("live_passed", 0),
    }


# --------------------------------------------------------------------------- #
# Preflight (no network, no DB)
# --------------------------------------------------------------------------- #


def _token_utilization(report: dict[str, Any]) -> dict[str, Any]:
    """What the byte bound costs, measured against the provider's own count.

    The counter is a UTF-8 byte upper bound, so until now the project could only
    compare it to itself. The live calls return the provider's prompt-token
    count, which is the first honest denominator. Two payloads are reported: the
    boundary case (inert ASCII padding, the *most* token-dense payload the
    counter can face) and the post-R1 probe (real Chinese conversation).
    """
    measurements: dict[str, Any] = {}
    for name, case in (
        ("boundary_ascii", report["cases"].get("boundary")),
        ("post_r1_chinese", report["cases"].get("probe_post_r1")),
    ):
        case = case or {}
        tokens = (case.get("usage") or {}).get("input_tokens")
        units = case.get("gate_units")
        if not tokens or not units:
            continue
        measurements[name] = {
            "counted_units": units,
            "provider_input_tokens": tokens,
            "byte_bound_over_tokens": round(units / tokens, 3),
            "real_token_utilization_of_H": round(tokens / case["input_limit"], 4),
        }
    if not measurements:
        return {"measured": False, "reason": "no live usage was recorded"}
    return {
        "measured": True,
        "measurements": measurements,
        "note": (
            "counted_units is the byte upper bound the budget contract uses; provider_input_tokens "
            "is what the provider actually billed. byte_bound_over_tokens is therefore the "
            "conservatism factor of the byte counter - worst for ASCII (4 bytes/token), smaller "
            "for Chinese. real_token_utilization_of_H is how much of H real tokens used, which is "
            "the number the byte bound was previously unable to state."
        ),
    }


def _preflight() -> dict[str, Any]:
    approved = os.getenv("R1_LIVE_APPROVED") == "1"
    result: dict[str, Any] = {
        "batch_id": BATCH_ID,
        "status": "READY" if approved else "NOT_AUTHORISED",
        "network_started": False,
        "limits": {"attempts": MAX_ATTEMPTS, "max_seconds": MAX_SECONDS, "fallback_models": 0},
        "frozen_configuration": {
            "acceptance_budget": ACCEPTANCE_BUDGET,
            "min_turns": MIN_TURNS,
            "summary_priority_post_r1": SUMMARY_PRIORITY_POST,
            "summary_priority_pre_r1": SUMMARY_PRIORITY_PRE,
        },
    }
    if not approved:
        result["reason"] = "set R1_LIVE_APPROVED=1 only after approving the live attempt budget"
        return result
    profile = _profile()
    window = hot_window(profile)
    result["profile"] = {
        "provider": profile.provider_name,
        "model": profile.model,
        "base_url": profile.base_url,
        "context_window": profile.context_window,
        "max_output_tokens": profile.max_output_tokens,
        "credential_source": str(_llm_ap_path()),
    }
    result["hot_window"] = window.public_view()
    result["hot_window_matches_acceptance_budget"] = window.input_limit == ACCEPTANCE_BUDGET
    result["adapter_overhead"] = {
        "input_limit": window.input_limit,
        "envelope_units": envelope_overhead(profile),
        "per_message_units": per_message_overhead(profile),
        "per_tool_units": per_tool_overhead(profile),
        "note": (
            "the selector packs against H minus this measured adapter overhead, not against H; "
            "the wire gate counts the finished body (model/stream/stream_options/temperature/"
            "max_tokens/thinking) while the canonical count does not"
        ),
    }
    return result


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


async def _execute(output: Path) -> int:
    profile = _profile()
    budget = BatchBudget()
    report: dict[str, Any] = {**_preflight(), "status": "RUNNING", "cases": {}}
    try:
        report["cases"]["boundary"] = await _run_boundary(profile, budget)
        summariser = await _run_summariser(profile, budget)
        report["cases"]["summariser"] = summariser
        report["cases"]["probe_post_r1"] = await _run_probe(
            profile, budget,
            summary_text=summariser["summary_text"],
            summary_priority=SUMMARY_PRIORITY_POST,
            case="probe_post_r1",
        )
        report["cases"]["probe_pre_r1"] = await _run_probe(
            profile, budget,
            summary_text=summariser["summary_text"],
            summary_priority=SUMMARY_PRIORITY_PRE,
            case="probe_pre_r1",
        )
        report["token_utilization"] = _token_utilization(report)
        report["verdict"] = _verdict(report)
        report["status"] = "PASSED" if report["verdict"]["passed"] else "FAILED"
    except Exception as exc:  # noqa: BLE001 - the report is the deliverable
        report["status"] = "FAILED"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        report.setdefault("verdict", {"passed": False, "reason": str(exc)[:300]})
        if "token_utilization" not in report:
            report["token_utilization"] = _token_utilization(report)
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - budget.started, 3)
        report["network_started"] = bool(budget.attempts)
        report["network_attempts"] = budget.attempts
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if report["status"] == "PASSED" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        report = _preflight()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if os.getenv("R1_LIVE_APPROVED") != "1":
        raise SystemExit(
            "R1 live batch refused: set R1_LIVE_APPROVED=1 after approving the live attempt budget"
        )
    return asyncio.run(_execute(args.output.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
