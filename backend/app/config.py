from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


def load_llm_ap(path: str | Path, api_key_env: str = "AGENT_MODEL_API_KEY"):
    """Load the local three-line live-test file without returning the secret."""
    values: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in {"LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID"}:
            raise ValueError("invalid LLM_AP entry")
        values[key] = value
    required = {"LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID"}
    if set(values) != required or any(not values[key] for key in required):
        raise ValueError("LLM_AP must define LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL_ID")
    os.environ[api_key_env] = values["LLM_API_KEY"]
    from .model_gateway import ModelProfile

    return ModelProfile(
        base_url=values["LLM_BASE_URL"],
        model=values["LLM_MODEL_ID"],
        api_key_env=api_key_env,
    )
