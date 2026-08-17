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


def load_model_profile_from_env():
    """Load a single named OpenAI-compatible profile without exposing its key."""
    base_url = os.getenv("AGENT_MODEL_BASE_URL")
    model = os.getenv("AGENT_MODEL_ID")
    api_key_env = os.getenv("AGENT_MODEL_API_KEY_ENV", "AGENT_MODEL_API_KEY")
    if not base_url or not model:
        raise ValueError("AGENT_MODEL_BASE_URL and AGENT_MODEL_ID are required")
    if not os.getenv(api_key_env):
        raise ValueError(f"model key is not configured in {api_key_env}")
    from .model_gateway import ModelProfile

    return ModelProfile(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=float(os.getenv("AGENT_MODEL_TIMEOUT_SECONDS", "60")),
        max_attempts=int(os.getenv("AGENT_MODEL_MAX_ATTEMPTS", "4")),
        network_retries=int(os.getenv("AGENT_MODEL_NETWORK_RETRIES", "2")),
    )
