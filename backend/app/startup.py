from __future__ import annotations

import os
from pathlib import Path

from .config import load_llm_ap, load_model_profile_from_env
from .db import Database
from .domain import ApprovalService, CheckpointStore, PlanVersionService
from .events import EventStore
from .live_model import LiveConversationModel, LiveRuntimeModel
from .memory import MemoryService
from .memory_v2 import MemoryContextProvider, MemoryStore
from .memory_archive import ConversationArchiver
from .model_gateway import ModelGateway, ModelProfile
from .runtime import AgentRuntime, MockModelGateway
from .tools import create_default_registry
from .settings import SettingsService
from .notifications import NotificationService


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
    settings = SettingsService(db)
    model = LiveRuntimeModel(gateway, tools.describe()) if gateway else MockModelGateway()
    conversation_model = LiveConversationModel(gateway, settings) if gateway else None
    runtime = AgentRuntime(
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
    from .research.engine import ResearchEngine
    from .research.live import LiveResearchModel
    from .research.service import ResearchService
    from .research.web import WebSearchRetriever
    from .research.retriever import CombinedRetriever, LocalNoteRetriever
    from .research.worker import ManagedResearchWorker

    runtime.memory_store = MemoryStore(db, root / "memory")
    if conversation_model is not None:
        conversation_model.memory_store = runtime.memory_store
    runtime.settings = settings
    runtime.memory_context = MemoryContextProvider(db)
    runtime.archiver = ConversationArchiver(db, runtime.memory_store)
    runtime.memory_store.recover_projections()
    engine = ResearchEngine(LiveResearchModel(gateway), CombinedRetriever(WebSearchRetriever(search_base_url=os.getenv("RESEARCH_SEARCH_BASE_URL", "https://html.duckduckgo.com/html/")), LocalNoteRetriever(root / "research_notes"))) if gateway else None
    runtime.research = ResearchService(db, runtime.conversation.events, engine)
    runtime.research_worker = ManagedResearchWorker(runtime.research) if engine else None
    from .research.scheduler import ManagedScheduler, ScheduleService
    runtime.schedules = ScheduleService(db, runtime.conversation, runtime.research)
    runtime.scheduler = ManagedScheduler(runtime.schedules)
    runtime.notifications = NotificationService(db)
    runtime.research.notifications = runtime.notifications
    runtime.plan_documents.recover_pending_intents()
    return runtime
