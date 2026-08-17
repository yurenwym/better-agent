from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import register_routes
from .config import AppConfig


def create_app(config: AppConfig | None = None, runtime=None) -> FastAPI:
    settings = config or AppConfig()
    app = FastAPI(title="better-agent", version=settings.version)
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

    return app


app = create_app()
