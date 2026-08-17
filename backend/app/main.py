from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import AppConfig


def create_app(config: AppConfig | None = None) -> FastAPI:
    settings = config or AppConfig()
    app = FastAPI(title="better-agent", version=settings.version)
    app.state.config = settings

    @app.middleware("http")
    async def enforce_local_host(request: Request, call_next):
        hostname = request.url.hostname
        if hostname not in settings.allowed_hosts:
            return JSONResponse({"detail": "local host required"}, status_code=400)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return settings.public_view()

    return app


app = create_app()

