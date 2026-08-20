from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import register_routes
from .config import AppConfig


def create_app(config: AppConfig | None = None, runtime=None, static_dir: str | Path | None = None) -> FastAPI:
    settings = config or AppConfig()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = getattr(app.state, "runtime", None)
        plan_documents = getattr(runtime, "plan_documents", None)
        if plan_documents is not None:
            plan_documents.recover_pending_intents()
        worker = getattr(runtime, "turn_worker", None)
        if worker is not None:
            await worker.start()
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()

    app = FastAPI(title="better-agent", version=settings.version, lifespan=lifespan)
    app.state.config = settings
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.runtime = runtime

    @app.middleware("http")
    async def enforce_local_host(request: Request, call_next):
        hostname = request.url.hostname
        if hostname not in settings.allowed_hosts:
            return JSONResponse({"detail": "local host required"}, status_code=400)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return settings.public_view()

    register_routes(app)

    if static_dir is not None:
        static_path = Path(static_dir)
        if static_path.is_dir() and (static_path / "index.html").is_file():
            app.mount("/", StaticFiles(directory=static_path, html=True), name="frontend")

    return app


app = create_app()
