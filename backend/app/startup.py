from __future__ import annotations

import os
import subprocess
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
from .model_control import ModelControlStore
from .costs import CostService
from .runtime import AgentRuntime, MockModelGateway
from .tools import create_default_registry
from .settings import SettingsService
from .notifications import NotificationService
from .agents import AgentTaskService, ExpertAdvisoryService, LiveExpertModel, ManagedAgentWorker
from .behavior import BehaviorBundleService
from .evolution import EvolutionCandidateGenerator, EvolutionService, LiveBehaviorRunner, LivePromptCandidateProposer, LiveSafetyJudge
from .experience_observer import ExperienceObserver, ManagedExperienceObserver
from .conversation import UnavailableConversationModel


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _code_version() -> str:
    configured = os.getenv("BETTER_AGENT_CODE_VERSION")
    if configured:
        return configured
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parents[2], capture_output=True,
            check=True, text=True, timeout=2,
        ).stdout.strip() or "local"
    except (OSError, subprocess.SubprocessError):
        return "local"


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
    costs = CostService(db)
    gateway = ModelGateway(configured_profile, control_store=ModelControlStore(db, events=events, costs=costs)) if configured_profile else None
    settings = SettingsService(db)
    model = LiveRuntimeModel(gateway, tools.describe()) if gateway else MockModelGateway()
    conversation_model = LiveConversationModel(gateway, settings) if gateway else UnavailableConversationModel()
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
    runtime.costs = costs
    from .research.engine import ResearchEngine
    from .research.live import LiveResearchModel
    from .research.service import ResearchService
    from .research.web import WebSearchRetriever
    from .research.tavily import TavilySearchRetriever
    from .research.retriever import CombinedRetriever, LocalNoteRetriever
    from .research.worker import ManagedResearchWorker

    runtime.memory_store = MemoryStore(db, root / "memory")
    if conversation_model is not None:
        conversation_model.memory_store = runtime.memory_store
    runtime.settings = settings
    runtime.memory_context = MemoryContextProvider(db)
    runtime.archiver = ConversationArchiver(db, runtime.memory_store)
    runtime.memory_store.recover_projections()
    provider=os.getenv("RESEARCH_SEARCH_PROVIDER","duckduckgo").strip().lower()
    if provider=="tavily":web_retriever=TavilySearchRetriever(os.getenv("TAVILY_API_KEY",""))
    elif provider=="duckduckgo":web_retriever=WebSearchRetriever(search_base_url=os.getenv("RESEARCH_SEARCH_BASE_URL", "https://html.duckduckgo.com/html/"))
    else:raise ValueError(f"unsupported RESEARCH_SEARCH_PROVIDER: {provider}")
    research_model = LiveResearchModel(gateway) if gateway else None
    engine = ResearchEngine(research_model, CombinedRetriever(web_retriever, LocalNoteRetriever(root / "research_notes"))) if gateway else None
    runtime.research = ResearchService(db, runtime.conversation.events, engine)
    runtime.research_worker = ManagedResearchWorker(runtime.research) if engine else None
    from .research.scheduler import ManagedScheduler, ScheduleService
    runtime.schedules = ScheduleService(db, runtime.conversation, runtime.research)
    runtime.scheduler = ManagedScheduler(runtime.schedules)
    runtime.notifications = NotificationService(db)
    runtime.research.notifications = runtime.notifications
    runtime.plan_documents.recover_pending_intents()
    runtime.behavior = BehaviorBundleService(db)
    model_manifest = configured_profile.public_view() if configured_profile else {"configured": False}
    model_manifest.pop("api_key_configured", None)
    skill_manifest = {item.name: __import__("hashlib").sha256(item.content.encode("utf-8")).hexdigest() for item in runtime.skills.list()}
    bundle = runtime.behavior.ensure({
        "code": _code_version(),
        "model": model_manifest,
        "skills": skill_manifest,
        "policy": "personal-agent-v1",
        "prompts": "live-model-v1",
        "tools": __import__("hashlib").sha256(__import__("json").dumps(tools.describe(), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "context": {"renderer": "context-v1", "tokenizer": "estimate-v1"},
    })
    try: runtime.behavior.active("stable")
    except KeyError: runtime.behavior.activate("stable", bundle.id, f"startup-stable:{bundle.id}")
    runtime.evolution = EvolutionService(
        db, runtime.behavior, behavior_runner=LiveBehaviorRunner(gateway) if gateway else None,
    )
    prompt_policy = lambda: runtime.behavior.active("stable").manifest.get("prompts", runtime.behavior.active("stable").manifest.get("prompt"))
    if gateway:
        model.runtime_prompt_policy = prompt_policy
        conversation_model.runtime_prompt_policy = prompt_policy
        research_model.runtime_prompt_policy = prompt_policy
        runtime.goal_programs.compiler.runtime_prompt_policy = prompt_policy
    runtime.observer = ExperienceObserver(db, runtime.events, runtime.evolution, thread_events=runtime.conversation.events)
    runtime.candidate_generator = EvolutionCandidateGenerator(
        runtime.evolution, runtime.behavior, LivePromptCandidateProposer(gateway) if gateway else None,
    )
    runtime.observer_worker = ManagedExperienceObserver(runtime.observer, candidate_generator=runtime.candidate_generator)
    runtime.agent_tasks = AgentTaskService(db, thread_events=runtime.conversation.events, evolution=runtime.evolution)
    runtime.agent_worker = ManagedAgentWorker(
        runtime.agent_tasks, LiveExpertModel(gateway) if gateway else None,
        safety_judge=LiveSafetyJudge(gateway) if gateway else None,
    )
    runtime.expert_advisor = ExpertAdvisoryService(runtime.agent_tasks, runtime.behavior) if gateway else None
    if runtime.expert_advisor is not None:
        runtime.goal_programs.expert_advisor = runtime.expert_advisor
        runtime.goal_review_worker.expert_advisor = runtime.expert_advisor
        if runtime.research_worker is not None:
            runtime.research_worker.expert_advisor = runtime.expert_advisor
    return runtime
