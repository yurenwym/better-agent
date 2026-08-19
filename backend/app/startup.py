from __future__ import annotations

import os
from pathlib import Path

from .config import load_llm_ap, load_model_profile_from_env
from .db import Database
from .domain import ApprovalService, CheckpointStore, PlanVersionService
from .events import EventStore
from .live_model import LiveConversationModel, LiveRuntimeModel
from .memory import MemoryService
from .model_gateway import ModelGateway, ModelProfile
from .runtime import AgentRuntime, MockModelGateway
from .tools import create_default_registry


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def build_runtime(data_root: str | Path, profile: ModelProfile | None = None, llm_ap_path: str | Path | None = None) -> AgentRuntime:
    root = Path(data_root)
    db = Database(root / "agent.db", workspace=root / "artifacts")
    events = EventStore(db)
    approvals = ApprovalService(db)
    tools = create_default_registry(root / "artifacts", db=db, approval_service=approvals)
    configured_profile = profile
    configured_path = llm_ap_path or os.getenv("LLM_AP_PATH")
    if configured_profile is None and configured_path:
        configured_profile = load_llm_ap(configured_path)
    if configured_profile is None and os.getenv("AGENT_MODEL_BASE_URL"):
        configured_profile = load_model_profile_from_env()
    gateway = ModelGateway(configured_profile) if configured_profile else None
    model = LiveRuntimeModel(gateway, tools.describe()) if gateway else MockModelGateway()
    conversation_model = LiveConversationModel(gateway) if gateway else None
    return AgentRuntime(
        db=db,
        events=events,
        plans=PlanVersionService(db),
        approvals=approvals,
        checkpoints=CheckpointStore(db),
        memory=MemoryService(db, events, root / "memory"),
        tools=tools,
        model=model,
        conversation_model=conversation_model,
    )
