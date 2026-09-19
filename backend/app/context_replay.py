"""Offline quality replay for the R1 context budget (M3-01).

Mechanism metrics - retained bytes, archive count, first-token latency, cost -
cannot show that a compaction change is *better*. Only one question matters:
after compaction, does the model still have the facts it needs?

This harness answers that question deterministically, with no provider calls:

* a fixture conversation plants seven facts, one per dimension the plan names
  (constraint adherence, latest corrected value, confirmed parameter, tool
  result, failed attempt, open question, plus a fact that only exists in the
  recent turns), at the *oldest* end of the thread;
* bulk filler turns follow, so a newest-first pack drops the planted facts;
* a deterministic archiver stands in for the LLM summariser and records the
  latest value of each tracked topic;
* a scripted probe model answers each question from the assembled request.

A probe passes only when the expected fact survived **and** no superseded value
survived alongside it. Dropping the summary therefore fails the run even though
every mechanism metric looks healthy.

Run it directly for a report::

    python -m app.context_replay
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .token_budget import DEFAULT_TOKEN_COUNTER, pack_messages_newest
from .transcript import (
    CanonicalTranscript,
    CanonicalTurnTranscriptBuilder,
    ResolvedMemoryScope,
    TranscriptEvent,
    TranscriptTurn,
    _source_hash,
)

# --------------------------------------------------------------------------- #
# Fixture: facts planted at the oldest end, filler after
# --------------------------------------------------------------------------- #

# A turn is one user message plus the assistant response that follows it, so the
# turn boundaries here match what ``pack_messages_newest`` groups as ``turn:<i>``.
# (role, content, call_id, event_type)
_Event = tuple[str, str, str | None, str]
_Turn = list[_Event]

_PLANTED: list[_Turn] = [
    [("user", "我要去杭州玩，预算是 5000 元。", None, "message"),
     ("assistant", "好的，我先记下。", None, "message")],
    [("user", "出发日期定在 3 月 12 日。", None, "message"),
     ("assistant", "明白。", None, "message")],
    [("user", "帮我查一下杭州的高铁票价。", None, "message"),
     ("assistant", "", "call-1", "tool_call"),
     ("tool", "查询结果：高铁票价 553 元。", "call-1", "tool_result")],
    [("user", "我之前试过 A 方案，失败了，因为时间太赶。", None, "message"),
     ("assistant", "了解，那这次避开这个安排。", None, "message")],
    [("user", "预算改成 8000 元。", None, "message"),
     ("assistant", "好的，预算已更新为 8000 元。", None, "message")],
    [("user", "住宿还没决定。", None, "message"),
     ("assistant", "好的，等你想好再说。", None, "message")],
    [("user", "硬约束：总花费不能超过预算。", None, "message"),
     ("assistant", "明白，我会守住这条约束。", None, "message")],
]

_FILLER_TEXT = (
    "顺便聊聊杭州的天气和交通情况，西湖周边周末人流量比较大，"
    "地铁一号线可以直达主要景区，打车高峰期会比较堵，"
    "如果时间充裕可以考虑骑行，沿湖的路线风景不错。"
)

# A fact that exists ONLY in the newest turns, so the "recent turns" channel is
# tested independently of the summary.
_RECENT_FACT = "刚刚确认：这次出发一共 4 人。"
_RECENT_FILLER = "好的，人数我记下了。"

FILLER_TURNS = 220


def build_fixture() -> list[_Turn]:
    """The full conversation: planted facts first, then bulk filler."""
    turns: list[_Turn] = [list(turn) for turn in _PLANTED]
    for index in range(FILLER_TURNS):
        turns.append([
            ("user", f"{_FILLER_TEXT}（第 {index + 1} 次闲聊）", None, "message"),
            ("assistant", "好的，我记下了。", None, "message"),
        ])
    # Newest turns carry the recent-only fact.
    turns.append([("user", _RECENT_FACT, None, "message"),
                  ("assistant", _RECENT_FILLER, None, "message")])
    return turns


# --------------------------------------------------------------------------- #
# Deterministic archiver: the latest value of each tracked topic
# --------------------------------------------------------------------------- #

_TOPIC_PATTERNS: tuple[tuple[str, str], ...] = (
    ("预算", r"预算(?:改成|改为|是)?\s*(\d+)\s*元"),
    ("出发日期", r"出发日期定在\s*([0-9]+\s*月\s*[0-9]+\s*日)"),
    ("高铁票价", r"高铁票价\s*(\d+)\s*元"),
    ("失败尝试", r"(A 方案，失败了)"),
    ("未解问题", r"(住宿还没决定)"),
    ("硬约束", r"(总花费不能超过预算)"),
)


# Filler turns handed to the summariser alongside the planted facts. The live
# batch uses the same prefix so its real summariser sees the same workload the
# deterministic stand-in sees.
ARCHIVE_TURNS = 60


def archive_input() -> list[_Turn]:
    """The turns one archival pass is given: planted facts plus bounded filler."""
    fixture = build_fixture()
    return fixture[:len(_PLANTED) + ARCHIVE_TURNS]


def render_summary(payload: Any) -> str:
    """Render a summariser reply into the Episode text the context carries.

    The shape mirrors :func:`archive` so the live summariser and the
    deterministic stand-in compete for the budget at a comparable size. An
    unparseable reply renders to an empty string, which the caller must treat
    as "no summary" rather than as a passing summary.
    """
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""

    def items(name: str) -> list[str]:
        value = payload.get(name)
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item).strip()]
        return []

    parts = [f"历史归档摘要 synopsis：{payload.get('synopsis', '')}"]
    for name in ("decisions", "outcomes", "open_loops"):
        parts.append(f"{name}：" + "；".join(items(name)))
    facts = payload.get("facts")
    if isinstance(facts, dict):
        parts.append("facts：" + "；".join(f"{key}={value}" for key, value in facts.items()))
    elif isinstance(facts, (list, tuple)):
        parts.append("facts：" + "；".join(str(item) for item in facts))
    return "\n".join(parts)


def archive(turns: list[_Turn]) -> str:
    """Stand-in for the LLM summariser.

    Records the *latest* value seen for each tracked topic, which is exactly
    what a summariser instructed to "record current values" must do. It never
    emits a superseded value. It reads the whole archived transcript - user
    messages, assistant messages and tool results alike - because the real
    summariser is given the full transcript, not just the user's turns.

    The output is padded to roughly the real Episode ceiling
    (``MAX_SUMMARY_OUTPUT_TOKENS = 1600`` bytes) so the summary competes for the
    budget at a realistic size instead of being a trivially cheap item.
    """
    latest: dict[str, str] = {}
    transcript: list[str] = []
    for turn in turns:
        for _role, content, _call_id, event_type in turn:
            if event_type not in {"message", "tool_result"}:
                continue
            transcript.append(content)
            for label, pattern in _TOPIC_PATTERNS:
                found = re.findall(pattern, content)
                if found:
                    latest[label] = found[-1]
    if not latest:
        return ""

    facts = "；".join(f"{label}={value}" for label, value in latest.items())
    synopsis = "".join(transcript)
    while len(synopsis.encode("utf-8")) < 1100:
        synopsis += "".join(transcript)
    synopsis = synopsis.encode("utf-8")[:1100].decode("utf-8", errors="ignore")
    return (
        f"历史归档摘要 synopsis：{synopsis}\n"
        "decisions：沿用既有安排\n"
        "outcomes：已确认的关键取值见下\n"
        "open_loops：见未解问题\n"
        f"facts：{facts}"
    )


# --------------------------------------------------------------------------- #
# Competing optional context
# --------------------------------------------------------------------------- #
#
# The priority ladder only decides anything when the *message* layer has to
# drop something. History alone rarely fills the budget, so the replay also
# assembles the other optional blocks a real request carries - skill, goal and
# plan - at realistic sizes. That is what makes the summary's priority the
# deciding factor rather than an academic detail.

_SKILL_BASE = (
    "技能：行程规划助手。步骤一，确认目的地与日期；步骤二，核算预算上限；"
    "步骤三，比选交通与住宿；步骤四，输出可执行清单并逐项核对约束。"
)
_GOAL_BASE = (
    "目标：在不超预算的前提下完成一次杭州出行安排，保留可回退的备选方案，"
    "并在每次调整后重新核对已确认参数是否仍然成立。"
)
_PLAN_BASE = (
    "计划：先定交通，再定住宿，最后核对总花费；每完成一步记录一次结论，"
    "未确定的项目统一放进未解问题，不在摘要里写成已确认。"
)


def _pad(base: str, target_bytes: int) -> str:
    text = base
    while len(text.encode("utf-8")) < target_bytes:
        text += base
    return text.encode("utf-8")[:target_bytes].decode("utf-8", errors="ignore")


SKILL_BLOCK = ("skill-context", 70, False, _pad(_SKILL_BASE, 8000))
GOAL_BLOCK = ("goal-context", 55, True, _pad(_GOAL_BASE, 6000))
PLAN_BLOCK = ("plan-context", 50, False, _pad(_PLAN_BASE, 5000))


def _block(spec: tuple[str, int, bool, str]) -> dict[str, Any]:
    group, priority, required, text = spec
    return {
        "role": "system",
        "content": text,
        "_context_required": required,
        "_context_priority": priority,
        "_context_group": group,
    }


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Probe:
    probe_id: str
    dimension: str
    expected: str
    superseded: tuple[str, ...] = ()


PROBES: tuple[Probe, ...] = (
    Probe("constraint", "约束遵守", "不能超过预算"),
    Probe("latest_value", "最新纠正值", "8000", superseded=("5000",)),
    Probe("confirmed_param", "已确认参数", "3 月 12 日"),
    Probe("tool_result", "工具结果", "553"),
    Probe("failed_attempt", "失败尝试", "A 方案"),
    Probe("open_question", "未解问题", "住宿还没决定"),
    Probe("recent_turns", "最近几轮对话", "4 人"),
)


def _blob(messages: list[dict[str, Any]]) -> str:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True)


def _ordered_text(messages: list[dict[str, Any]]) -> str:
    """The context as the model reads it: message by message, oldest first."""
    parts: list[str] = []
    for message in messages:
        parts.append(str(message.get("content", "")))
        for call in message.get("tool_calls") or []:
            parts.append(json.dumps(call, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def run_probes(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Answer every probe from the assembled request alone.

    A superseded value is only contamination when it appears **after** the last
    mention of the current value. Raw history legitimately contains "5000" and
    then "8000"; a reader resolves that by recency. What must never happen is a
    stale value outliving the correction that replaced it.
    """
    text = _ordered_text(messages)
    results = []
    for probe in PROBES:
        retained = probe.expected in text
        last_expected = text.rfind(probe.expected)
        contaminated = [
            value for value in probe.superseded
            if value in text and text.rfind(value) > last_expected
        ]
        results.append({
            "probe_id": probe.probe_id,
            "dimension": probe.dimension,
            "passed": retained and not contaminated,
            "retained": retained,
            "contaminated": contaminated,
        })
    return results


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _to_transcript(turns: list[_Turn]) -> CanonicalTranscript:
    scope = ResolvedMemoryScope(owner_id="local-user", thread_id="replay", project_id=None)
    built: list[TranscriptTurn] = []
    sequence = 0
    for index, turn in enumerate(turns):
        events: list[TranscriptEvent] = []
        for role, content, call_id, event_type in turn:
            sequence += 1
            events.append(TranscriptEvent(
                event_type, f"turn-{index}", f"msg-{sequence}", call_id, role, content, sequence,
            ))
        built.append(TranscriptTurn(
            f"turn-{index}", "completed", events[0].sequence, events[-1].sequence, tuple(events),
        ))
    return CanonicalTranscript(scope, tuple(built), _source_hash(scope, built, ()))


def _messages_from(turns: tuple[TranscriptTurn, ...]) -> list[dict[str, Any]]:
    return CanonicalTurnTranscriptBuilder.render_history(
        CanonicalTranscript(
            ResolvedMemoryScope(owner_id="local-user", thread_id="replay", project_id=None),
            turns, "",
        )
    )


def leading_context(summary_text: str, *, summary_priority: int | None) -> list[dict[str, Any]]:
    """The optional context blocks a request carries ahead of the history.

    ``summary_priority`` is the only knob under test: the memory-context block
    is placed on the same ladder as skill/goal/plan so the priority decides
    whether it survives, exactly as it does in the live request.
    """
    leading: list[dict[str, Any]] = [_block(SKILL_BLOCK), _block(GOAL_BLOCK), _block(PLAN_BLOCK)]
    if summary_priority is not None and summary_text:
        leading.append({
            "role": "system",
            "content": summary_text,
            "_context_required": False,
            "_context_priority": summary_priority,
            "_context_group": "memory-context",
        })
    return leading


def assemble(
    *,
    summary_priority: int | None,
    budget: int | None,
    min_turns: int = 0,
) -> list[dict[str, Any]]:
    """Build the request a variant would send.

    ``budget is None`` means "nothing dropped" - the uncompressed reference.
    """
    return assemble_with_summary(
        archive([list(turn) for turn in build_fixture()[:len(_PLANTED)]]),
        summary_priority=summary_priority,
        budget=budget,
        min_turns=min_turns,
    )


def assemble_with_summary(
    summary_text: str,
    *,
    summary_priority: int | None,
    budget: int | None,
    min_turns: int = 0,
    newest_user: str | None = None,
) -> list[dict[str, Any]]:
    """The same two-layer assembly, with a caller-supplied summary.

    The live batch uses this so the summary under test is produced by the real
    provider instead of the deterministic stand-in, while everything else - the
    fixture, the hot-window floor, the priority ladder and the final pack - is
    byte-for-byte the same code the offline replay exercises.
    """
    fixture = build_fixture()
    transcript = _to_transcript(fixture)

    if budget is None:
        return _messages_from(transcript.turns)

    # Turn-level hot window first (the ``min_turns`` floor lives here), then the
    # message-level priority pack - the same two layers the live path uses.
    # ``pack_recent`` reads no instance state, so it is called unbound.
    hot = CanonicalTurnTranscriptBuilder.pack_recent(
        None, transcript, budget, min_turns=min_turns,
    )
    history = _messages_from(hot.turns)

    leading = leading_context(summary_text, summary_priority=summary_priority)
    messages = [*leading, *history]
    if newest_user is not None:
        messages.append({"role": "user", "content": newest_user})
    return pack_messages_newest(messages, budget=budget)


# --------------------------------------------------------------------------- #
# Variants and verdict
# --------------------------------------------------------------------------- #

VARIANTS: dict[str, dict[str, Any]] = {
    "reference": {"summary_priority": None, "budget": None, "min_turns": 0},
    # Same budget in both, so the only difference is the priority fix (and the
    # min_turns floor). Otherwise the comparison would conflate two changes.
    "pre_r1": {"summary_priority": 20, "budget": 23_348, "min_turns": 0},
    "post_r1": {"summary_priority": 60, "budget": 23_348, "min_turns": 5},
}


def utilization() -> dict[str, Any]:
    """How conservative the byte bound is, measured honestly.

    The counter is a UTF-8 byte upper bound, so the only offline comparison
    available is counted units vs the real serialised payload, using the same
    compact separators the counter uses. True *token* utilization needs a
    provider tokenizer and is deliberately **not** claimed here - a
    budget-fill percentage is not real utilization.
    """
    messages = assemble(**VARIANTS["post_r1"])
    compact = json.dumps(
        {"messages": messages, "tools": []},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    counted = DEFAULT_TOKEN_COUNTER.count_payload(messages)
    payload_bytes = len(compact.encode("utf-8"))
    characters = len(compact)
    return {
        "counted_units": counted,
        "payload_bytes": payload_bytes,
        "exact_byte_bound": counted == payload_bytes,
        "payload_characters": characters,
        "bytes_per_character": round(payload_bytes / characters, 3) if characters else None,
        "budget": VARIANTS["post_r1"]["budget"],
        "budget_fill_ratio": round(counted / VARIANTS["post_r1"]["budget"], 4),
        "note": (
            "counted_units == payload_bytes exactly: for the byte-only tier the "
            "count is the payload size, not an estimate. The conservatism is "
            "relative to *tokens*: bytes_per_character shows how far a "
            "non-ASCII conversation inflates the bound."
        ),
        "true_token_utilization": "not measured offline; requires a provider tokenizer",
    }


def evaluate() -> dict[str, Any]:
    report: dict[str, Any] = {"variants": {}, "min_turns_sweep": {}}

    for name, config in VARIANTS.items():
        messages = assemble(**config)
        results = run_probes(messages)
        report["variants"][name] = {
            "config": config,
            "messages": len(messages),
            "size": len(_blob(messages).encode("utf-8")),
            "passed": sum(1 for item in results if item["passed"]),
            "total": len(results),
            "probes": results,
        }

    for min_turns in (0, 1, 3, 5, 8, 12):
        messages = assemble(summary_priority=60, budget=23_348, min_turns=min_turns)
        results = run_probes(messages)
        report["min_turns_sweep"][str(min_turns)] = {
            "passed": sum(1 for item in results if item["passed"]),
            "total": len(results),
            "failed": [item["probe_id"] for item in results if not item["passed"]],
        }

    report["verdict"] = _verdict(report)
    report["utilization"] = utilization()
    report["packing_note"] = (
        "Both variants pack against the frozen legacy H (23348), so the only difference "
        "between them is the priority (and the min_turns floor). The live path packs "
        "against token_budget.packing_limit instead, which is H minus the adapter "
        "overhead measured for the request (23149 for the 32k openai_compatible "
        "profile); a live request of the same shape is smaller by that reservation. "
        "This replay has no profile, so it cannot and does not model that difference."
    )
    return report


def _verdict(report: dict[str, Any]) -> dict[str, Any]:
    """Quality must not regress. Mechanism gains do not buy a pass."""
    reference = report["variants"]["reference"]
    pre = report["variants"]["pre_r1"]
    post = report["variants"]["post_r1"]

    if reference["passed"] != reference["total"]:
        return {
            "passed": False,
            "reason": "the uncompressed reference itself fails; the fixture is not "
                      "a valid upper bound and no comparison is meaningful",
        }

    regressions = [
        item["probe_id"] for item, before in zip(post["probes"], pre["probes"])
        if before["passed"] and not item["passed"]
    ]
    if regressions:
        return {
            "passed": False,
            "reason": f"post-R1 lost probes that pre-R1 passed: {regressions}",
        }

    if post["passed"] < reference["passed"]:
        missing = [
            item["probe_id"] for item, ref in zip(post["probes"], reference["probes"])
            if ref["passed"] and not item["passed"]
        ]
        return {
            "passed": False,
            "reason": f"post-R1 is worse than the uncompressed reference: {missing}",
        }

    return {
        "passed": True,
        "reason": f"post-R1 retains {post['passed']}/{post['total']} probes; "
                  f"pre-R1 retained {pre['passed']}/{pre['total']}",
        "improved": post["passed"] - pre["passed"],
    }


def main() -> int:
    report = evaluate()
    print(json.dumps(report, ensure_ascii=False, indent=2))

    print("\n=== 质量回放 ===")
    for name in ("reference", "pre_r1", "post_r1"):
        item = report["variants"][name]
        print(f"{name:10s} {item['passed']}/{item['total']} passed  size={item['size']}")
    print("\nmin_turns sweep:")
    for key, item in report["min_turns_sweep"].items():
        print(f"  min_turns={key:>2s}  {item['passed']}/{item['total']}  failed={item['failed']}")
    print(f"\nverdict: {'PASS' if report['verdict']['passed'] else 'FAIL'} - {report['verdict']['reason']}")
    return 0 if report["verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
