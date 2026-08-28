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
        expert_advisor=None, evolution=None, safety_judge=None,
    ) -> None:
        self.service = service; self.owner = f"research-worker-{uuid.uuid4().hex}"; self.poll_interval = poll_interval; self.lease_seconds = lease_seconds
        self._task = None; self._stop = None; self._active_cancel = None
        self._shutdown = False
        self.expert_advisor = expert_advisor
        self.evolution = evolution
        self.safety_judge = safety_judge

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

    async def run_once(self):
        job = self.service.claim_next(self.owner, self.lease_seconds)
        if not job: return False
        cancel = asyncio.Event(); self._active_cancel = cancel
        heartbeat = asyncio.create_task(self._heartbeat(job.id, cancel))
        report = None
        gateway = getattr(getattr(self.service.engine, "model", None), "gateway", None)
        context_token = None
        try:
            if getattr(gateway, "control_store", None) is not None:
                from ..model_control import ModelCallContext
                with self.service.db.connection() as connection:
                    turn = connection.execute(
                        "SELECT runtime_bundle_id FROM turns WHERE id=?", (job.source_turn_id,)
                    ).fetchone()
                context_token = gateway.set_call_context(ModelCallContext(
                    role="researcher", purpose="research", thread_id=job.thread_id,
                    turn_id=job.source_turn_id, runtime_bundle_id=turn["runtime_bundle_id"] if turn else None,
                ))
            sections,sources,evidence,plan=self.service.recovery_context(job.id)
            request = ResearchRequest(job.id, job.topic, job.source_scopes, ResearchLimits(), cancel, sections, sources, evidence, plan)
            async for event in self.service.engine.run_research(request):
                current = self.service.get(job.id)
                if current.cancel_requested_at: cancel.set()
                if event.type == "report": report = event.data
                self.service.apply_event(job.id, self.owner, event)
            if self.service.get(job.id).cancel_requested_at: raise ResearchCancelled("research cancelled")
            if not report: raise RuntimeError("research report missing")
            if self.expert_advisor is not None:
                with self.service.db.connection() as connection:
                    source_turn = connection.execute("SELECT runtime_bundle_id FROM turns WHERE id=?", (job.source_turn_id,)).fetchone()
                advice = await self.expert_advisor.advise(
                    purpose="research", source_id=job.id, objective="审阅研究报告的证据覆盖、结论边界和关键风险",
                    context={"topic": job.topic, "report": report["markdown"]}, roles=("researcher", "critic"),
                    thread_id=job.thread_id, runtime_bundle_id=source_turn["runtime_bundle_id"] if source_turn else None,
                )
                if advice is not None:
                    self.service.events.append(job.thread_id, job.source_turn_id, "research.expert_reviewed", "coordinator", {
                        "job_id": job.id, "summary": str(advice.get("summary", ""))[:500],
                        "incomplete": bool(advice.get("incomplete")),
                    })
            self.service.complete(job.id, self.owner, report["title"], report["markdown"], int(report["source_count"]), int(report["evidence_count"]))
            await self._finish_exposure(job, success=True, output=report["markdown"])
            notifier = getattr(self.service, "notifications", None)
            if notifier is not None:
                await notifier.deliver_completed(job.id, report["title"], report["markdown"])
        except ResearchCancelled:
            if not self._shutdown:
                self.service.finish_cancelled(job.id, self.owner)
                await self._finish_exposure(job, success=False)
        except ResearchConflict:
            if self.service.get(job.id).cancel_requested_at:
                self.service.finish_cancelled(job.id,self.owner)
            else:
                raise
        except PermissionError:
            pass
        except Exception as exc:
            reason=getattr(exc,"reason_code",type(exc).__name__.lower())
            diagnostics=getattr(exc,"diagnostics",None)
            retryable=bool(getattr(exc,"retryable",False)) or isinstance(exc,(TimeoutError,ConnectionError,asyncio.TimeoutError)) or getattr(exc,"kind","") in {"timeout","rate_limit","server"}
            with contextlib.suppress(PermissionError): self.service.fail(job.id, self.owner, reason,retryable,diagnostics)
            if not retryable:
                await self._finish_exposure(job, success=False)
        finally:
            if context_token is not None:
                gateway.reset_call_context(context_token)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError): await heartbeat
            self._active_cancel = None
        return True

    async def _finish_exposure(self, job, *, success: bool, output: str = "") -> None:
        if self.evolution is None:
            return
        safety_pass = None
        if self.safety_judge is not None and output:
            with self.service.db.connection() as connection:
                turn = connection.execute("SELECT runtime_bundle_id FROM turns WHERE id=?", (job.source_turn_id,)).fetchone()
            gateway = getattr(self.safety_judge, "gateway", None)
            token = None
            if getattr(gateway, "control_store", None) is not None:
                from ..model_control import ModelCallContext
                token = gateway.set_call_context(ModelCallContext(
                    role="judge_safety", purpose="judge_research_output", thread_id=job.thread_id,
                    turn_id=job.source_turn_id, runtime_bundle_id=turn["runtime_bundle_id"] if turn else None,
                ))
            try:
                safety_pass = await self.safety_judge.judge({"research_job_id": job.id, "output": output})
            except Exception:
                safety_pass = None
            finally:
                if token is not None:
                    gateway.reset_call_context(token)
        self.evolution.finish_run_exposure(job.source_turn_id, success=success, safety_pass=safety_pass)

    async def _loop(self):
        while not self._stop.is_set():
            if not await self.run_once():
                try: await asyncio.wait_for(self._stop.wait(), self.poll_interval)
                except asyncio.TimeoutError: pass

    async def _heartbeat(self, job_id, cancel):
        while True:
            await asyncio.sleep(max(self.lease_seconds / 3, .05))
            if not self.service.renew(job_id, self.owner, self.lease_seconds): cancel.set(); return
