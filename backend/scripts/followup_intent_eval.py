"""Real-model intent probe plus unchanged production conversation adapter replay.

No business tools execute. Gold labels never enter model requests. Each paid
request/response is saved separately; failures remain in denominators.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import load_user_model_environment, load_model_profile_from_environment
from app.live_model import LiveConversationModel, conversation_instruction
from app.model_gateway import ModelGateway, ModelRequest, ModelResponse, UsageBuckets, Timing, GatewayError

DATA = ROOT / "docs/evaluation/followup-intent-v1/cases.json"
PROBE = """本次为意图观测，替换前述输出格式：不生成攻略，不执行工具，只返回一个 JSON 对象。
字段必须为 tracking、review、document、reason。
tracking: none（没有新增或取消持续跟踪请求，或保留原状）、enable（请求持续跟进，含主动周期复盘）、disable（取消跟踪）、clarify（是否跟踪不清楚）。
review: none（无本次复盘或变更复盘服务的请求）、once（单次复盘）、daily（要求每日主动复盘）、weekly（要求每周主动复盘）、stop（取消既有复盘）、clarify（复盘意图不明确）。
document: answer（只需回答或非文档操作）、save（创建或保存文档）、modify（修改现有计划内容）。
reason: 一句说明判断所依据的用户表达。
标签针对最新请求涉及的目标和本次操作，不是用户全部历史偏好的汇总。不把缺少日期等操作参数当作意图不明。
"""


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tracking: Literal["none", "enable", "disable", "clarify"]
    review: Literal["none", "once", "daily", "weekly", "stop", "clarify"]
    document: Literal["answer", "save", "modify"]
    reason: str


def write_json(path, value):
    def encode(item):
        if is_dataclass(item):
            return asdict(item)
        raise TypeError(f"Unsupported report type: {type(item).__name__}")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=encode) + "\n", encoding="utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def probe_messages(case):
    # Deliberately allowlist input fields: no id, domain, expected, or rationale.
    return [{"role": "system", "content": conversation_instruction()},
            {"role": "system", "content": PROBE},
            *case["history"], {"role": "user", "content": case["input"]}]


def score(rows):
    probes = [r for r in rows if r["mode"] == "probe"]
    n = len(probes)
    fields = ("tracking", "review", "document")
    exact = sum(all(r.get("prediction", {}).get(k) == r["expected"][k] for k in fields) for r in probes)
    tp = sum(r["expected"]["review"] == "daily" and r.get("prediction", {}).get("review") == "daily" for r in probes)
    fp = sum(r["expected"]["review"] != "daily" and r.get("prediction", {}).get("review") == "daily" for r in probes)
    fn = sum(r["expected"]["review"] == "daily" and r.get("prediction", {}).get("review") != "daily" for r in probes)
    confusion = {}
    for r in probes:
        key = r["expected"]["review"] + " -> " + r.get("prediction", {}).get("review", "ERROR")
        confusion[key] = confusion.get(key, 0) + 1
    return dict(n=n, exact_correct=exact, exact_accuracy=exact/n if n else None,
                fields={k: sum(r.get("prediction", {}).get(k) == r["expected"][k] for r in probes)/n if n else None for k in fields},
                daily=dict(tp=tp, fp=fp, fn=fn, precision=tp/(tp+fp) if tp+fp else None,
                           recall=tp/(tp+fn) if tp+fn else None),
                errors=sum(r["status"] != "ok" for r in probes), review_confusion=confusion,
                failed_ids=[r["id"] for r in probes if any(r.get("prediction", {}).get(k) != r["expected"][k] for k in fields)])


class RecordedGateway(ModelGateway):
    def __init__(self, profile, out, prefix, semaphore, budget, replay_only=False):
        super().__init__(profile)
        self.out, self.prefix, self.semaphore, self.budget = out, prefix, semaphore, budget
        self.records = []
        self.replay_only = replay_only

    async def complete(self, request, **kwargs):
        if self.replay_only:
            path = self.out / f"{self.prefix}-call-{len(self.records)+1:02}.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["request"] != asdict(request):
                raise RuntimeError("recovery_request_mismatch")
            self.records.append(record)
            if record["status"] != "ok":
                raise GatewayError("recorded call failed", record.get("error_kind") or "unknown")
            data = dict(record["response"])
            data["usage"] = UsageBuckets(**data["usage"])
            data["timing"] = Timing(**data["timing"])
            return ModelResponse(**data)
        async with self.semaphore:
            if self.budget["calls"] >= self.budget["limit"]:
                raise RuntimeError("evaluation_call_limit")
            self.budget["calls"] += 1
            index = len(self.records) + 1
            record = {"request": asdict(request), "status": "started", "started_at": datetime.now(timezone.utc).isoformat()}
            self.records.append(record)
            path = self.out / f"{self.prefix}-call-{index:02}.json"
            write_json(path, record)
            started = time.perf_counter()
            try:
                response = await super().complete(request, **kwargs)
                record.update(status="ok", response=asdict(response))
                return response
            except Exception as exc:
                record.update(status="error", error_type=type(exc).__name__, error_kind=getattr(exc, "kind", None))
                raise
            finally:
                record["elapsed_seconds"] = time.perf_counter() - started
                write_json(path, record)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=DATA)
    parser.add_argument("--mode", choices=["both", "probe", "conversation"], default="both")
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--recover", action="store_true", help="rebuild missing rows from saved responses only; never call provider")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute required for paid real-model calls")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    assert len(cases) >= 30 and len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        Intent.model_validate({**c["expected"], "reason": "gold"})
    if args.ids:
        cases = [c for c in cases if c["id"] in args.ids]
        if len(cases) != len(set(args.ids)):
            parser.error("unknown case id")
    load_user_model_environment()
    profile = load_model_profile_from_environment()
    if profile is None:
        raise RuntimeError("No configured real model")
    profile = replace(profile, max_attempts=1, network_retries=0, timeout_seconds=90, max_output_tokens=1800)
    if args.recover:
        cases = json.loads((args.out / "cases.snapshot.json").read_text(encoding="utf-8"))
        if (args.out / "production-prompt.txt").read_text(encoding="utf-8") != conversation_instruction():
            raise RuntimeError("production_prompt_changed")
    else:
        args.out.mkdir(parents=True, exist_ok=False)
        write_json(args.out / "cases.snapshot.json", cases)
        (args.out / "production-prompt.txt").write_text(conversation_instruction(), encoding="utf-8")
        (args.out / "probe-instruction.txt").write_text(PROBE, encoding="utf-8")
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(),
                  model=profile.model, provider=profile.provider_name, host=urlsplit(profile.base_url).hostname,
                  configuration=dict(temperature=0, thinking=False, max_output_tokens=1800, probe_max_tokens=450,
                                     max_attempts=1, network_retries=0, timeout_seconds=90, concurrency=3),
                  dataset_hash=digest(args.cases.read_bytes()),
                  source_hashes={f: digest((ROOT/f).read_bytes()) for f in [
                      "backend/app/live_model.py", "backend/app/ask.py", "backend/app/model_gateway.py",
                      "backend/scripts/followup_intent_eval.py"]},
                  cost_usd=None, cost_note="Token usage recorded; no verified billing rate/supplier invoice available. Cost is unknown, not zero.",
                  limitations=["Probe adds an observation-only output protocol; it is not the production classifier.",
                               "Conversation mode calls LiveConversationModel but does not execute business tools, workers, approvals or DB writes.",
                               "Configured model alias recorded; gateway does not expose supplier resolved model revision.",
                               "One run per scenario/mode, no statistical production reliability claim."], rows=[])
    if args.recover:
        report = json.loads((args.out / "results.json").read_text(encoding="utf-8"))
        report["recovery"] = {"reason": "nested UsageBuckets serialization in adapter SimpleNamespace", "provider_calls": 0,
                              "script_hash": digest(Path(__file__).read_bytes())}
    write_json(args.out / "results.json", report)
    semaphore = asyncio.Semaphore(3)
    budget = {"calls": 0, "limit": len(cases)*5}
    modes = ["probe", "conversation"] if args.mode == "both" else [args.mode]

    async def run_case(case, mode):
        if args.recover and any(r["id"] == case["id"] and r["mode"] == mode for r in report["rows"]):
            return
        gateway = RecordedGateway(profile, args.out, f'{case["id"]}-{mode}', semaphore, budget, replay_only=args.recover)
        row = dict(id=case["id"], mode=mode, expected=case["expected"], status="started")
        started = time.perf_counter()
        try:
            if mode == "probe":
                response = await gateway.complete(ModelRequest(messages=probe_messages(case), temperature=0,
                    max_tokens=450, thinking=False, response_format={"type": "json_object"}, purpose="followup_intent_probe"))
                if response.finish_reason == "length":
                    raise ValueError("truncated_probe")
                row["prediction"] = Intent.model_validate_json(response.message).model_dump()
            else:
                model = LiveConversationModel(gateway)
                response = await model.route_and_respond(content=case["input"], history=case["history"], skill_names=[],
                    on_text_delta=None, on_text_reset=None, cancel_event=asyncio.Event(), owner_id="intent-eval-user")
                row["response"] = asdict(response) if is_dataclass(response) else vars(response)
                calls = [t for rec in gateway.records for t in rec.get("response", {}).get("tool_calls", [])]
                row["observed_tools"] = [t.get("function", {}).get("name") for t in calls]
                row["truncated_calls"] = sum(rec.get("response", {}).get("finish_reason") == "length" for rec in gateway.records)
                row["manual_behavior_review_required"] = True
            row["status"] = "ok"
        except Exception as exc:
            row.update(status="error", error_type=type(exc).__name__, error_kind=getattr(exc, "kind", None))
        row["elapsed_seconds"] = time.perf_counter()-started
        row["model_calls"] = len(gateway.records)
        row["usage"] = {k: sum(rec.get("response", {}).get("usage", {}).get(k) or 0 for rec in gateway.records)
                        for k in ["uncached_input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens"]}
        row["usage_missing_calls"] = sum("response" not in rec or any(v is None for v in rec["response"]["usage"].values()) for rec in gateway.records)
        report["rows"].append(row)
        report["probe_metrics"] = score(report["rows"])
        write_json(args.out / "results.json", report)
        print(f'{case["id"]} {mode} {row["status"]} calls={row["model_calls"]}', flush=True)

    # Bounded live calls; modes/cases are all predeclared and gold is frozen.
    await asyncio.gather(*(run_case(c, mode) for c in cases for mode in modes))
    report["rows"].sort(key=lambda r: (r["id"], r["mode"]))
    report.update(status="completed", completed_at=datetime.now(timezone.utc).isoformat(), total_model_calls=len(list(args.out.glob("*-call-*.json"))))
    report["usage"] = {k: sum(r["usage"][k] for r in report["rows"]) for k in report["rows"][0]["usage"]}
    report["usage_missing_calls"] = sum(r["usage_missing_calls"] for r in report["rows"])
    write_json(args.out / "results.json", report)
    print(json.dumps(report["probe_metrics"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
