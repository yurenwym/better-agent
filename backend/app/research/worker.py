from __future__ import annotations

import asyncio
import contextlib
import uuid

from .engine import ResearchCancelled
from .models import ResearchLimits, ResearchRequest
from .service import ResearchConflict


class ManagedResearchWorker:
    def __init__(self, service, *, poll_interval: float = .2, lease_seconds: int = 30) -> None:
        self.service = service; self.owner = f"research-worker-{uuid.uuid4().hex}"; self.poll_interval = poll_interval; self.lease_seconds = lease_seconds
        self._task = None; self._stop = None; self._active_cancel = None
        self._shutdown = False

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
        try:
            sections,sources,evidence,plan=self.service.recovery_context(job.id)
            request = ResearchRequest(job.id, job.topic, job.source_scopes, ResearchLimits(), cancel, sections, sources, evidence, plan)
            async for event in self.service.engine.run_research(request):
                current = self.service.get(job.id)
                if current.cancel_requested_at: cancel.set()
                if event.type == "report": report = event.data
                self.service.apply_event(job.id, self.owner, event)
            if self.service.get(job.id).cancel_requested_at: raise ResearchCancelled("research cancelled")
            if not report: raise RuntimeError("research report missing")
            self.service.complete(job.id, self.owner, report["title"], report["markdown"], int(report["source_count"]), int(report["evidence_count"]))
            notifier = getattr(self.service, "notifications", None)
            if notifier is not None:
                await notifier.deliver_completed(job.id, report["title"], report["markdown"])
        except ResearchCancelled:
            if not self._shutdown:
                self.service.finish_cancelled(job.id, self.owner)
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
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError): await heartbeat
            self._active_cancel = None
        return True

    async def _loop(self):
        while not self._stop.is_set():
            if not await self.run_once():
                try: await asyncio.wait_for(self._stop.wait(), self.poll_interval)
                except asyncio.TimeoutError: pass

    async def _heartbeat(self, job_id, cancel):
        while True:
            await asyncio.sleep(max(self.lease_seconds / 3, .05))
            if not self.service.renew(job_id, self.owner, self.lease_seconds): cancel.set(); return
