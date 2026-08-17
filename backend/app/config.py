from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    version: str = "1.0.0"
    api_key_env: str = "AGENT_MODEL_API_KEY"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")

    @property
    def api_key_configured(self) -> bool:
        return bool(os.getenv(self.api_key_env))

    def public_view(self) -> dict[str, object]:
        return {
            "status": "ok",
            "service": "better-agent",
            "version": self.version,
            "api_key_configured": self.api_key_configured,
        }

