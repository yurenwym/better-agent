from __future__ import annotations

import asyncio
import contextlib
import uuid

from .engine import ResearchCancelled
from .models import ResearchLimits, ResearchRequest
from .service import ResearchConflict


class ManagedResearchWorker:
    def __init__(
        self, service, *, poll_interval: float = .2, lease_seconds: int = 30,
        expert_advisor=None, evolution=None, safety_judge=None, limits=None,
    ) -> None:
        self.service = service; self.owner = f"research-worker-{uuid.uuid4().hex}"; self.poll_interval = poll_interval; self.lease_seconds = lease_seconds
        self._task = None; self._stop = None; self._active_cancel = None
        self._shutdown = False
        self.expert_advisor = expert_advisor
        self.evolution = evolution
        self.safety_judge = safety_judge
        self.limits = limits or ResearchLimits()

    async def start(self):
        if self._task: return
        self._stop = asyncio.Event(); self._task = asyncio.create_task(self._loop(), name="research-worker")

    async def stop(self):
        if not self._task: return
        self._stop.set()
        self._shutdown = True
        if self._active_cancel: self._active_cancel.set()
        task, self._task = self._task, None
        with contextlib.suppress(asyncio.CancelledError): await task

    async def run_once(self, job_id: str | None = None):
        job = (
            self.service.claim(job_id, self.owner, self.lease_seconds)
            if job_id is not None else self.service.claim_next(self.owner, self.lease_seconds)
        )
        if not job: return False
        cancel = asyncio.Event(); self._active_cancel = cancel
        heartbeat = asyncio.create_task(self._heartbeat(job.id, cancel, job.lease_epoch))
        report = None
        gateway = getattr(getattr(self.service.engine, "model", None), "gateway", None)
        context_token = None
        runtime_bundle_id = None
        try:
            from ..harness_context_store import HarnessContextStore
            source_context = HarnessContextStore(self.service.db).load_turn_context(job.source_turn_id)
            with self.service.db.connection() as connection:
                turn = connection.execute(
                    "SELECT t.runtime_bundle_id,th.owner_id FROM turns t "
                    "JOIN threads th ON th.id=t.thread_id WHERE t.id=?", (job.source_turn_id,)
                ).fetchone()
            if turn is None or not turn["owner_id"]:
                raise PermissionError("research source turn is missing an authorized owner")
            runtime_bundle_id = turn["runtime_bundle_id"] if turn else None
            if self.evolution is not None:
                runtime_bundle_id, _ = self.evolution.assign_role_task(
                    job.id, job.thread_id, role="researcher", purpose="write_research_section",
                    owner_id=turn["owner_id"],
                )
            if getattr(self, "learning", None) is not None:
                with self.service.db.connection() as connection:
                    project = connection.execute("SELECT project_id FROM threads WHERE id=?", (job.thread_id,)).fetchone()
                runtime_bundle_id = self.learning.resolve_prompt(turn["owner_id"], "researcher", "write_research_section", runtime_bundle_id)
                runtime_bundle_id = self.learning.resolve_research_policy(turn["owner_id"], project[0] if project else None, runtime_bundle_id)
            if getattr(gateway, "control_store", None) is not None:
                from ..model_control import ModelCallContext
                from ..execution_context import create_child_context
                from dataclasses import replace
                harness = replace(create_child_context(source_context), run_id=job.id,
                    runtime_bundle_id=runtime_bundle_id,
                    root_budget_id=self._root_budget_id(job.id),
                )
                context_token = gateway.set_call_context(ModelCallContext.from_harness(
                    harness, role="researcher", purpose="research",
                ))
            sections,sources,evidence,plan=self.service.recovery_context(job.id)
            limits = self.limits
            policy = None
            if runtime_bundle_id:
                from ..behavior import BehaviorBundleService
                from ..task_policy import research_limits
                policy = BehaviorBundleService(self.service.db).get(runtime_bundle_id).manifest.get("task_policy")
                if policy is not None:
                    limits = research_limits(limits, policy)
                    from dataclasses import asdict
                    self.service.events.append(job.thread_id, job.source_turn_id, "task_policy.executed", "research_worker", {
                        "job_id": job.id, "bundle_id": runtime_bundle_id, "effective_limits": asdict(limits),
                    })
            request = ResearchRequest(job.id, job.topic, job.source_scopes, limits, cancel, sections, sources, evidence, plan)
            if policy:
                from dataclasses import replace
                request = replace(request, stop_condition=policy.get("research_stop_condition", "none"))
            async for event in self._research_events(request, job.lease_epoch):
                current = self.service.get(job.id)
                if current.cancel_requested_at: cancel.set()
                if event.type == "report": report = event.data
                if event.type == "policy_decision":
                    self.service.events.append(job.thread_id, job.source_turn_id, "task_policy.research_decision", "research_worker", {"job_id": job.id, **event.data})
                self.service.apply_event(job.id, self.owner, event, epoch=job.lease_epoch)
            if self.service.get(job.id).cancel_requested_at: raise ResearchCancelled("research cancelled")
            if not report: raise RuntimeError("research report missing")
            if self.expert_advisor is not None:
                with self.service.db.connection() as connection:
                    source_turn = connection.execute(
                        "SELECT t.runtime_bundle_id,th.owner_id FROM turns t "
                        "JOIN threads th ON th.id=t.thread_id WHERE t.id=?", (job.source_turn_id,),
                    ).fetchone()
                if source_turn is None or not source_turn["owner_id"]:
                    raise PermissionError("research source turn is missing an authorized owner")
                from ..send_authority import send_authority
                def require_attempt():
                    with self.service.db.transaction() as connection:
                        self.service._owned(job.id, self.owner, connection, epoch=job.lease_epoch)
                with send_authority(require_attempt):
                    advice = await self.expert_advisor.advise(
                        purpose="research", source_id=job.id, objective="审阅研究报告的证据覆盖、结论边界和关键风险",
                        context={"topic": job.topic, "report": report["markdown"]}, roles=("researcher", "critic"),
                        thread_id=job.thread_id, runtime_bundle_id=source_turn["runtime_bundle_id"],
                        owner_id=source_turn["owner_id"],
                        root_budget_id=self._root_budget_id(job.id),
                    )
                if advice is not None:
                    with self.service.db.transaction() as connection:
                        self.service._owned(job.id, self.owner, connection, epoch=job.lease_epoch)
                        self.service.events.append(job.thread_id, job.source_turn_id, "research.expert_reviewed", "coordinator", {
                            "job_id": job.id, "summary": str(advice.get("summary", ""))[:500],
                            "incomplete": bool(advice.get("incomplete")),
                        }, connection=connection)
            if report.get("completion_status") == "PARTIAL":
                self.service.complete_partial(
                    job.id, self.owner, report["title"], report["markdown"],
                    int(report["source_count"]), int(report["evidence_count"]),
                    report.get("traceability", ()), report.get("missing_requirements", ()), epoch=job.lease_epoch,
                )
            else:
                self.service.complete(
                    job.id, self.owner, report["title"], report["markdown"],
                    int(report["source_count"]), int(report["evidence_count"]),
                    report.get("traceability", ()), epoch=job.lease_epoch,
                )
            await self._finish_exposure(job, success=True, output=report["markdown"], runtime_bundle_id=runtime_bundle_id)
            notifier = getattr(self.service, "notifications", None)
            if notifier is not None:
                await notifier.deliver_completed(job.id, report["title"], report["markdown"])
        except ResearchCancelled:
            if not self._shutdown:
                self.service.finish_cancelled(job.id, self.owner, epoch=job.lease_epoch)
                await self._finish_exposure(job, success=False, runtime_bundle_id=runtime_bundle_id)
        except ResearchConflict:
            if self.service.get(job.id).cancel_requested_at:
                self.service.finish_cancelled(job.id, self.owner, epoch=job.lease_epoch)
            else:
                raise
        except PermissionError:
            pass
        except Exception as exc:
            if self.service.get(job.id).cancel_requested_at:
                self.service.finish_cancelled(job.id, self.owner, epoch=job.lease_epoch)
                await self._finish_exposure(job, success=False, runtime_bundle_id=runtime_bundle_id)
                return True
            reason=(getattr(exc,"reason_code",None) or getattr(exc,"kind",None) or type(exc).__name__.lower())
            diagnostics=getattr(exc,"diagnostics",None)
            retryable=bool(getattr(exc,"retryable",False)) or isinstance(exc,(TimeoutError,ConnectionError,asyncio.TimeoutError)) or getattr(exc,"kind","") in {"timeout","rate_limit","server"}
            from ..outcome_adapters import error_from_exception
            from ..execution_outcome import ExecutionOutcome, Scope, Status
            with contextlib.suppress(PermissionError):
                self.service.fail(job.id, self.owner, reason, retryable, diagnostics,
                    epoch=job.lease_epoch, outcome=ExecutionOutcome(Scope.TASK, Status.FAILED, error=error_from_exception(exc)))
            if not retryable:
                await self._finish_exposure(job, success=False, runtime_bundle_id=runtime_bundle_id)
        finally:
            if context_token is not None:
                gateway.reset_call_context(context_token)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError): await heartbeat
            self._active_cancel = None
        return True

    async def _research_events(self, request, epoch):
        from ..send_authority import send_authority
        def check():
            with self.service.db.transaction() as connection:
                row = self.service._owned(request.job_id, self.owner, connection, epoch=epoch, allow_cancelled=True)
                if row["cancel_requested_at"] is not None:
                    raise ResearchCancelled("research cancelled")
        async with contextlib.aclosing(self.service.engine.run_research(request)) as events:
            while True:
                try:
                    with send_authority(check):
                        event = await anext(events)
                except StopAsyncIteration:
                    return
                yield event

    async def _finish_exposure(self, job, *, success: bool, output: str = "", runtime_bundle_id: str | None = None) -> None:
        if self.evolution is None:
            return
        safety_pass = None
        if self.safety_judge is not None and output:
            gateway = getattr(self.safety_judge, "gateway", None)
            token = None
            if getattr(gateway, "control_store", None) is not None:
                from ..model_control import ModelCallContext
                from ..harness_context_store import HarnessContextStore
                from ..execution_context import create_child_context
                from dataclasses import replace
                root = HarnessContextStore(self.service.db).load_turn_context(job.source_turn_id)
                harness = replace(create_child_context(root), run_id=job.id,
                    runtime_bundle_id=runtime_bundle_id or root.runtime_bundle_id,
                    root_budget_id=self._root_budget_id(job.id))
                token = gateway.set_call_context(ModelCallContext.from_harness(
                    harness, role="judge_safety", purpose="judge_research_output"))
            try:
                safety_pass = await self.safety_judge.judge({"research_job_id": job.id, "output": output})
            except Exception:
                safety_pass = None
            finally:
                if token is not None:
                    gateway.reset_call_context(token)
        self.evolution.finish_run_exposure(job.id, success=success, safety_pass=safety_pass)
        if getattr(self, "learning", None) is not None and runtime_bundle_id:
            with self.service.db.connection() as connection:
                scope = connection.execute("SELECT owner_id FROM threads WHERE id=?", (job.thread_id,)).fetchone()
            if scope:
                self.learning.record_prompt_result(scope["owner_id"], runtime_bundle_id, job.id, success=success, safety_pass=safety_pass)

    def _root_budget_id(self, job_id: str):
        with self.service.db.connection() as connection:
            row = connection.execute("SELECT root_budget_id FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        return row["root_budget_id"] if row else None

    async def _loop(self):
        while not self._stop.is_set():
            if not await self.run_once():
                try: await asyncio.wait_for(self._stop.wait(), self.poll_interval)
                except asyncio.TimeoutError: pass

    async def _heartbeat(self, job_id, cancel, epoch):
        while True:
            await asyncio.sleep(max(self.lease_seconds / 3, .05))
            if not self.service.renew(job_id, self.owner, self.lease_seconds, epoch=epoch): cancel.set(); return
