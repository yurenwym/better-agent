from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from .api import register_routes
from .config import AppConfig


MAX_JSON_BODY_BYTES = 2 * 1024 * 1024


class JsonBodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.allowed_hosts = set(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        host_header = headers.get(b"host", b"").decode("latin-1")
        hostname = host_header.rsplit("]", 1)[0].lstrip("[") if host_header.startswith("[") else host_header.split(":", 1)[0]
        if hostname not in self.allowed_hosts:
            await self._send_json(send, 400, b'{"detail":"local host required"}')
            return
        content_type = headers.get(b"content-type", b"").lower()
        if scope.get("method") not in {"POST", "PUT", "PATCH"} or not content_type.startswith(b"application/json"):
            await self.app(scope, receive, send)
            return

        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = self.max_bytes + 1
            if content_length < 0 or content_length > self.max_bytes:
                await self._send_json(send, 413, b'{"detail":"JSON request body is too large"}')
                return

        total = 0
        too_large = False
        buffered_messages: list[dict] = []

        async def limited_receive() -> dict:
            nonlocal total, too_large
            if too_large:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    too_large = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def buffered_send(message: dict) -> None:
            buffered_messages.append(message)

        await self.app(scope, limited_receive, buffered_send)
        if too_large:
            await self._send_json(send, 413, b'{"detail":"JSON request body is too large"}')
            return
        for message in buffered_messages:
            await send(message)

    @staticmethod
    async def _send_json(send: Send, status: int, body: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


def create_app(config: AppConfig | None = None, runtime=None, static_dir: str | Path | None = None) -> FastAPI:
    settings = config or AppConfig()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = getattr(app.state, "runtime", None)
        plan_documents = getattr(runtime, "plan_documents", None)
        if plan_documents is not None:
            plan_documents.recover_pending_intents()
        worker = getattr(runtime, "turn_worker", None)
        research_worker = getattr(runtime, "research_worker", None)
        goal_review_worker = getattr(runtime, "goal_review_worker", None)
        scheduler = getattr(runtime, "scheduler", None)
        if worker is not None:
            await worker.start()
        if research_worker is not None:
            await research_worker.start()
        if goal_review_worker is not None:
            await goal_review_worker.start()
        if scheduler is not None:
            await scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            if research_worker is not None:
                await research_worker.stop()
            if goal_review_worker is not None:
                await goal_review_worker.stop()
            if worker is not None:
                await worker.stop()

    app = FastAPI(title="better-agent", version=settings.version, lifespan=lifespan)
    app.state.config = settings
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.runtime = runtime

    app.add_middleware(
        JsonBodySizeLimitMiddleware,
        max_bytes=MAX_JSON_BODY_BYTES,
        allowed_hosts=settings.allowed_hosts,
    )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return settings.public_view()

    register_routes(app)

    if static_dir is not None:
        static_path = Path(static_dir)
        if static_path.is_dir() and (static_path / "index.html").is_file():
            index_path = static_path / "index.html"

            @app.get("/plans", include_in_schema=False)
            @app.get("/plans/{plan_id}", include_in_schema=False)
            @app.get("/today", include_in_schema=False)
            @app.get("/research", include_in_schema=False)
            @app.get("/schedules", include_in_schema=False)
            async def frontend_route(plan_id: str | None = None):
                return FileResponse(index_path)

            app.mount("/", StaticFiles(directory=static_path, html=True), name="frontend")

    return app


app = create_app()
