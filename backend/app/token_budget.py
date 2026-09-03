from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol, TypeVar


class TokenCounter(Protocol):
    version: str

    def count_text(self, value: str) -> int: ...

    def count_payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int: ...


class Utf8UpperBoundTokenCounter:
    """A provider-independent upper bound for byte-fallback tokenizers.

    One token cannot encode less than one UTF-8 byte in the supported provider
    tokenizers. Counting canonical payload bytes therefore deliberately
    overestimates instead of risking a context overflow.
    """

    version = "utf8-upper-bound-v1"

    def count_text(self, value: str) -> int:
        return len(value.encode("utf-8"))

    def count_payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
        payload = {"messages": messages, "tools": tools or []}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self.count_text(encoded)


DEFAULT_TOKEN_COUNTER = Utf8UpperBoundTokenCounter()


@dataclass(frozen=True)
class TokenBudget:
    context_window: int
    reserved_output: int
    safety_margin: int

    @property
    def input_limit(self) -> int:
        return max(self.context_window - self.reserved_output - self.safety_margin, 0)


class ContextOverflow(ValueError):
    pass


def request_budget(profile: Any, *, safety_margin: int | None = None) -> TokenBudget:
    context_window = int(getattr(profile, "context_window", 0) or 0)
    reserved_output = int(getattr(profile, "max_output_tokens", 0) or 0)
    if context_window <= 0 or reserved_output <= 0 or reserved_output >= context_window:
        raise ContextOverflow("model profile has no valid context budget")
    available = context_window - reserved_output
    if safety_margin is None:
        # Keep a proportional guard for normal models, but never let the
        # default margin consume the entire input budget of a small profile.
        margin = min(2048, max(1, available // 20))
    else:
        margin = max(0, min(int(safety_margin), max(available - 1, 0)))
    return TokenBudget(context_window, reserved_output, margin)


T = TypeVar("T")


def pack_newest(
    items: Iterable[T],
    *,
    budget: int,
    render: Callable[[T], str],
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> tuple[list[T], list[T]]:
    """Pack newest atomic items, returning selected items chronologically."""

    ordered = list(items)
    selected_reversed: list[T] = []
    dropped: list[T] = []
    used = 0
    for item in reversed(ordered):
        cost = counter.count_text(render(item))
        if used + cost <= budget:
            selected_reversed.append(item)
            used += cost
        else:
            dropped.append(item)
    return list(reversed(selected_reversed)), list(reversed(dropped))


def pack_messages_newest(
    messages: Iterable[dict[str, Any]],
    *,
    budget: int,
    tools: list[dict[str, Any]] | None = None,
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> list[dict[str, Any]]:
    """Keep a bounded request while retaining instructions and latest user turn."""
    ordered = [dict(message) for message in messages]
    if budget <= 0:
        return []
    mandatory_indexes = {
        index for index, message in enumerate(ordered)
        if message.get("role") == "system"
    }
    for index in range(len(ordered) - 1, -1, -1):
        if ordered[index].get("role") == "user":
            mandatory_indexes.add(index)
            break
    selected_indexes = set(mandatory_indexes)
    selected = [ordered[index] for index in sorted(selected_indexes)]
    if counter.count_payload(selected, tools) > budget:
        return selected
    used = counter.count_payload(selected, tools)
    for index in range(len(ordered) - 1, -1, -1):
        if index in selected_indexes:
            continue
        candidate = ordered[index]
        # A tool result is only meaningful with its assistant tool-call.  It
        # is safer to omit the pair than to send an invalid partial exchange.
        if candidate.get("role") == "tool":
            continue
        candidate_cost = counter.count_payload([candidate], None)
        if used + candidate_cost > budget:
            continue
        selected_indexes.add(index)
        if candidate.get("tool_calls"):
            call_ids = {
                call.get("id") for call in candidate.get("tool_calls", [])
                if isinstance(call, dict) and call.get("id")
            }
            for tool_index in range(index + 1, len(ordered)):
                tool = ordered[tool_index]
                if tool.get("role") == "tool" and tool.get("tool_call_id") in call_ids:
                    selected_indexes.add(tool_index)
        selected = [ordered[item] for item in sorted(selected_indexes)]
        used = counter.count_payload(selected, tools)
        if used > budget:
            selected_indexes.discard(index)
            selected_indexes.difference_update(
                tool_index for tool_index in range(index + 1, len(ordered))
                if tool_index in selected_indexes and ordered[tool_index].get("role") == "tool"
            )
            selected = [ordered[item] for item in sorted(selected_indexes)]
            used = counter.count_payload(selected, tools)
    return selected


def assert_request_fits(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    profile: Any,
    *,
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> int:
    budget = request_budget(profile)
    count = counter.count_payload(messages, tools)
    if count > budget.input_limit:
        raise ContextOverflow(f"model context requires {count} tokens but input budget is {budget.input_limit}")
    return count
