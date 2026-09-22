from __future__ import annotations

import hashlib
import os
import json
import subprocess
from pathlib import Path

from .config import goal_tools_enabled, load_llm_ap, load_model_price, load_model_profile_from_env, load_model_profile_from_environment
from .db import Database
from .domain import ApprovalService, CheckpointStore, PlanVersionService
from .events import EventStore
from .live_model import LiveConversationModel, LiveRuntimeModel
from .memory import MemoryService
from .memory_v2 import MemoryContextProvider, MemoryStore
from .memory_archive import (
    ConversationArchiver, LiveEpisodeSummarizer, ManagedArchiveWorker,
    archive_jobs_per_minute_from_env, foreground_turn_pending,
)
from .model_gateway import ModelGateway, ModelProfile
from .model_control import ModelControlStore, RoutedModelGateway
from .model_admin import ModelAdminService, ROLES
from .costs import CostService, PriceSnapshot
from .runtime import AgentRuntime, MockModelGateway
from .tools import create_default_registry
from .trusted_connectors import TrustedConnectorService
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


def _has_valid_model_routing(db: Database, bundle) -> bool:
    routing = bundle.manifest.get("model_routing") or {}
    policy_id = routing.get("policy_id")
    digest = routing.get("digest")
    if not policy_id or policy_id == "unconfigured" or not digest:
        return False
    with db.connection() as connection:
        policy = connection.execute(
            "SELECT roles_json,policy_digest FROM model_routing_policies WHERE id=? AND owner_id='local-user'",
            (policy_id,),
        ).fetchone()
        if policy is None or policy["policy_digest"] != digest:
            return False
        roles = json.loads(policy["roles_json"])
        # The runtime cannot serve conversations without these routes. A policy
        # that only covers expert/text roles must not keep an old bundle active
        # while /api/model-readiness reports MODEL_ROUTE_MISSING.
        if not {"conversation", "ask"}.issubset(roles):
            return False
        version_ids = {
            version_id
            for route in roles.values()
            for version_id in [route.get("primary"), *route.get("fallback", [])]
            if version_id
        }
        if not version_ids:
            return False
        placeholders = ",".join("?" for _ in version_ids)
        profiles = connection.execute(
            f"SELECT id,status,context_window,max_output_tokens FROM model_profile_versions WHERE id IN ({placeholders})",
            tuple(version_ids),
        ).fetchall()
    return len(profiles) == len(version_ids) and all(
        row["status"] == "ACTIVE"
        and int(row["context_window"]) > int(row["max_output_tokens"]) > 0
        for row in profiles
    )


#: The rollout gate for the V3 learning pipeline (V3 §65 steps 17/20/21/22).
#: `OFF` leaves the legacy branches in `learning_legacy` in charge, which is the
#: pre-cut-over state the frozen regression baseline was pinned to.
LEARNING_V3_MODES: dict[str, str] = {"SHADOW": "SHADOW", "ACTIVE": "ACTIVE"}


def _wire_learning_pipeline(runtime: AgentRuntime, gateway) -> None:
    """Attach the one Learning Pipeline, if this deployment has asked for it.

    Wiring is deliberately a deployment decision rather than a per-owner one:
    the pipeline needs a JEV credential and a generator, and a half-wired
    pipeline would silently learn nothing. It therefore fails loudly instead of
    degrading, and stays off unless `BETTER_AGENT_LEARNING_V3` names a mode.
    """
    requested = (os.getenv("BETTER_AGENT_LEARNING_V3") or "OFF").strip().upper()
    if requested == "OFF":
        return
    if requested not in LEARNING_V3_MODES:
        raise ValueError(f"invalid BETTER_AGENT_LEARNING_V3: {requested!r}; "
                         f"expected OFF or one of {sorted(LEARNING_V3_MODES)}")
    if gateway is None:
        raise RuntimeError(f"BETTER_AGENT_LEARNING_V3={requested} requires a model gateway")
    if not os.getenv("TYPESAFE_API_KEY"):
        raise RuntimeError(f"BETTER_AGENT_LEARNING_V3={requested} requires TYPESAFE_API_KEY")

    from .learning_agent import LearningAgent
    from .learning_decision import DEFAULT_MODEL, JevDecisionService, TypesafeClient
    from .learning_eval import LearningJudge
    from .learning_pipeline import build_pipeline
    from .learning_replay import RuntimeLearningReplay
    from .learning_promotion import PromotionPolicy

    runtime.learning.pipeline = build_pipeline(
        runtime, mode=LEARNING_V3_MODES[requested],
        decisions=JevDecisionService(
            db=runtime.db, client=TypesafeClient(model=os.getenv("TYPESAFE_MODEL", DEFAULT_MODEL))),
        agent=LearningAgent(gateway), judge=LearningJudge(gateway),
        replay=RuntimeLearningReplay(runtime, gateway, os.getenv("BETTER_AGENT_LEARNING_REPLAY_FILE")))
    canary_budget = os.getenv("BETTER_AGENT_LEARNING_CANARY_BUDGET_MICROUSD")
    if canary_budget is not None:
        if not canary_budget.isdigit() or int(canary_budget) <= 0:
            raise ValueError("BETTER_AGENT_LEARNING_CANARY_BUDGET_MICROUSD must be a positive integer")
        runtime.learning.pipeline.gate.policy = PromotionPolicy(canary_budget_microusd=int(canary_budget))


def build_runtime(    data_root: str | Path,
    profile: ModelProfile | None = None,
    llm_ap_path: str | Path | None = None,
    *,
    database_url: str | None = None,
    conversation_model=None,
    activate_stable: bool = True,
) -> AgentRuntime:
    root = Path(data_root)
    configured_database = database_url or os.getenv("DATABASE_URL")
    if configured_database and not configured_database.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("DATABASE_URL must use PostgreSQL; PostgreSQL is the authoritative database")
    if not configured_database and os.getenv("BETTER_AGENT_TEST_ALLOW_SQLITE") != "1":
        raise RuntimeError("DATABASE_URL is required; PostgreSQL is the authoritative database")
    db = Database(
        configured_database if configured_database else root / "agent.db",
        workspace=root / "artifacts",
    )
    events = EventStore(db)
    approvals = ApprovalService(db)
    connectors = TrustedConnectorService(db)
    tools = create_default_registry(root / "artifacts", db=db, approval_service=approvals, connectors=connectors)
    configured_profile = profile
    # Explicit environment configuration wins over a stale desktop path.
    configured_path = llm_ap_path or (
        None if any(os.getenv(name) for name in ("AGENT_MODEL_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"))
        else os.getenv("LLM_AP_PATH")
    )
    if configured_profile is None and configured_path:
        configured_profile = load_llm_ap(configured_path)
    if configured_profile is None:
        configured_profile = load_model_profile_from_environment()
    configured_fallback = (
        load_model_profile_from_env("AGENT_FALLBACK_MODEL")
        if os.getenv("AGENT_FALLBACK_MODEL_BASE_URL") else None
    )
    costs = CostService(db)
    model_admin = ModelAdminService(db)
    registered_profile = model_admin.ensure_profile(configured_profile) if configured_profile else None
    registered_fallback = model_admin.ensure_profile(
        configured_fallback, capabilities_env="AGENT_FALLBACK_MODEL_CAPABILITIES",
    ) if configured_fallback else None
    if registered_profile is None and registered_fallback is not None:
        registered_profile, registered_fallback = registered_fallback, None
    for raw_profile, registered, prefix in (
        (configured_profile, registered_profile, "AGENT_MODEL"),
        (configured_fallback, registered_fallback, "AGENT_FALLBACK_MODEL"),
    ):
        if raw_profile is None or registered is None:
            continue
        price = load_model_price(raw_profile, prefix)
        if price is None:
            continue
        payload = json.dumps({
            "profile_version_id": registered.registered_profile_version_id,
            "rates": [price.uncached_input_rate, price.cache_read_rate, price.cache_write_rate, price.output_rate, price.reasoning_rate],
            "effective_at": price.effective_at,
            "source_url": price.source_url,
        }, sort_keys=True, separators=(",", ":"))
        price_id = "model_price_" + hashlib.sha256(payload.encode()).hexdigest()[:32]
        costs.register_price(
            registered.registered_profile_version_id,
            PriceSnapshot(
                price_id, price.uncached_input_rate, price.cache_read_rate,
                price.cache_write_rate, price.output_rate, price.reasoning_rate,
            ),
            price.effective_at,
            price.source_url,
        )
    control_store = ModelControlStore(db, events=events, costs=costs)
    gateway = RoutedModelGateway(db, control_store) if registered_profile else None
    settings = SettingsService(db)
    model = LiveRuntimeModel(gateway, tools.describe()) if gateway else MockModelGateway()
    conversation_model = conversation_model or (
        LiveConversationModel(gateway, settings) if gateway else UnavailableConversationModel()
    )
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
    # MCP is built here but not connected: connecting is I/O and belongs to the
    # application lifespan, so a slow or dead optional server delays the first
    # chat turn at most, never process startup. With no server configured the
    # SDK is never imported.
    from .mcp_client import load_manager_from_env
    from .mcp_tools import McpToolRegistrySync

    mcp_manager = load_manager_from_env()
    runtime.mcp_manager = mcp_manager
    runtime.mcp_sync = McpToolRegistrySync(mcp_manager, tools, mcp_manager.configs())
    # Keep the selected adapter explicit for API preflight checks.  The
    # ConversationService also owns this object as ``route_model``, but the
    # runtime-level reference makes the configured/unconfigured distinction
    # stable without reaching into service internals.
    runtime.conversation_model = conversation_model
    runtime.model_api_key_env = (
        registered_profile.api_key_env if registered_profile is not None else "AGENT_MODEL_API_KEY"
    )
    runtime.costs = costs
    runtime.model_admin = model_admin
    runtime.connectors = connectors
    from .real_evaluation import LiveEvaluationRunner, ManagedEvaluationWorker, RealEvaluator
    runtime.real_evaluator = RealEvaluator(root / "evaluations", db=db)
    runtime.real_evaluator.runner_factory = LiveEvaluationRunner(runtime.model_admin, ModelControlStore(db, events=events, costs=costs)).runners
    runtime.evaluation_worker = ManagedEvaluationWorker(runtime.real_evaluator)
    from .research.engine import ResearchEngine
    from .research.live import DEFAULT_EVIDENCE_STATEMENT, LiveResearchModel
    from .research.service import ResearchService
    from .research.web import WebSearchRetriever
    from .research.tavily import TavilySearchRetriever
    from .research.retriever import CombinedRetriever, LocalNoteRetriever
    from .research.worker import ManagedResearchWorker

    runtime.memory_store = MemoryStore(db, root / "memory")
    from .learning import LearningService
    runtime.learning = LearningService(db, runtime.memory_store, costs)
    runtime.memory_store.learning_assets = runtime.learning.assets
    control_store.learning_assets = runtime.learning.assets
    runtime.learning.skill_root = runtime.skill_platform.root
    if gateway is not None:
        gateway.learning = runtime.learning
        from .learning_extraction import ConstraintExtractor
        runtime.learning.constraint_extractor = ConstraintExtractor(gateway)
    runtime.goal_programs.learning = runtime.learning
    # Goal business tools are registered only after their services exist, then
    # the model's tool schema snapshot is refreshed so the new run can see them.
    if goal_tools_enabled():
        from .goal_tools import register_goal_tools
        register_goal_tools(
            tools,
            goal_programs=runtime.goal_programs,
            plan_documents=runtime.plan_documents,
        )
        if hasattr(model, "tool_schemas"):
            model.tool_schemas = tools.describe()
    if conversation_model is not None:
        conversation_model.memory_store = runtime.memory_store
    runtime.settings = settings
    runtime.embedding_worker = None
    embedding_client = None
    from .embedding import embedding_api_key_env_from_env
    embedding_key_configured = embedding_api_key_env_from_env() is not None
    if db.backend == "postgresql" and embedding_key_configured:
        from .embedding import OpenAICompatibleEmbeddingClient, load_embedding_profile_from_env
        from .embedding_worker import EmbeddingWorker, ManagedEmbeddingWorker

        embedding_client = OpenAICompatibleEmbeddingClient(load_embedding_profile_from_env())
        runtime.embedding_worker = ManagedEmbeddingWorker(
            EmbeddingWorker(db, embedding_client, owner=f"embedding-{os.getpid()}")
        )
    runtime.embedding_client = embedding_client
    runtime.memory_context = MemoryContextProvider(db, embedding_provider=embedding_client)
    runtime.archiver = ConversationArchiver(
        db, runtime.memory_store, LiveEpisodeSummarizer(gateway) if gateway else None,
    )
    # R2-03: the background pass is bounded and yields to the foreground. The
    # rate cap is optional (unset means "as often as the static line allows"),
    # while the foreground probe is always wired: a queued user turn must never
    # compete with a background archival call for the same model budget.
    runtime.archive_worker = ManagedArchiveWorker(
        runtime.archiver,
        max_jobs_per_minute=archive_jobs_per_minute_from_env(),
        foreground_probe=lambda: foreground_turn_pending(db),
    ) if gateway else None
    runtime.memory_store.recover_projections()
    provider=os.getenv("RESEARCH_SEARCH_PROVIDER","bing").strip().lower()
    if provider=="tavily":web_retriever=TavilySearchRetriever(os.getenv("TAVILY_API_KEY",""))
    elif provider=="bing":web_retriever=WebSearchRetriever(search_base_url=os.getenv("RESEARCH_SEARCH_BASE_URL", WebSearchRetriever.DEFAULT_SEARCH_URL))
    elif provider=="duckduckgo":web_retriever=WebSearchRetriever(search_base_url=os.getenv("RESEARCH_SEARCH_BASE_URL", "https://html.duckduckgo.com/html/"),fallback_search_base_url=WebSearchRetriever.DEFAULT_SEARCH_URL)
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
    model_manifest = registered_profile.public_view() if registered_profile else {"configured": False}
    model_manifest.pop("api_key_configured", None)
    fallback_manifests = []
    if registered_fallback is not None:
        fallback_manifest = registered_fallback.public_view()
        fallback_manifest.pop("api_key_configured", None)
        fallback_manifests.append(fallback_manifest)
    skill_manifest = {item.name: __import__("hashlib").sha256(item.content.encode("utf-8")).hexdigest() for item in runtime.skills.list()}
    fallback_ids = (
        [registered_fallback.registered_profile_version_id]
        if registered_fallback is not None
        and registered_fallback.registered_profile_version_id != registered_profile.registered_profile_version_id
        else []
    )
    # A configured profile may intentionally expose only a subset of role
    # capabilities (for example, text-only local test models).  Keep startup
    # usable and let the gateway reject unsupported roles when they are used.
    routing_roles = {}
    if registered_profile:
        profile_capabilities = {
            name for name, enabled in model_admin.version(
                registered_profile.registered_profile_version_id
            )["capabilities"].items() if enabled
        }
        from .model_admin import ROLE_CAPABILITIES
        routing_roles = {
            role: {"primary": registered_profile.registered_profile_version_id, "fallback": fallback_ids}
            for role in ROLES
            if ROLE_CAPABILITIES[role].issubset(profile_capabilities)
        }
    routing_policy = model_admin.ensure_policy("默认运行时路由", routing_roles) if routing_roles else None
    bundle = runtime.behavior.ensure({
        "code": _code_version(),
        "model": model_manifest,
        "fallback_models": fallback_manifests,
        "skills": skill_manifest,
        "policy": "personal-agent-v1",
        "prompts": {
            "version": "live-model-v1",
            "researcher": {
                "write_research_section": {
                    "evidence_statement": DEFAULT_EVIDENCE_STATEMENT,
                }
            },
        },
        "tools": __import__("hashlib").sha256(__import__("json").dumps(tools.describe(), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "context": {"renderer": "context-v1", "tokenizer": "estimate-v1"},
        "model_routing": ({"policy_id": routing_policy["id"], "digest": routing_policy["policy_digest"]}
                          if routing_policy else {"policy_id": "unconfigured", "digest": "unconfigured"}),
        "model_role_bindings": routing_policy["roles"] if routing_policy else {},
    })
    try:
        stable = runtime.behavior.active("stable")
    except KeyError:
        stable = None
    configured_model_changed = bool(
        registered_profile
        and stable is not None
        and (
            stable.manifest.get("model") != model_manifest
            or stable.manifest.get("fallback_models", []) != fallback_manifests
        )
    )
    if activate_stable and (stable is None or configured_model_changed or (registered_profile and not _has_valid_model_routing(db, stable))):
        runtime.behavior.activate("stable", bundle.id, f"startup-stable:{bundle.id}")
    runtime.evolution = EvolutionService(
        db, runtime.behavior, evaluator=runtime.real_evaluator,
        behavior_runner=LiveBehaviorRunner(gateway) if gateway else None,
    )
    runtime.learning.evolution = runtime.evolution
    runtime.evolution.learning_assets = runtime.learning.assets
    runtime.evolution.recover_generation_batches()
    runtime.evolution.maintain_canaries()
    if runtime.research_worker is not None:
        runtime.research_worker.evolution = runtime.evolution
        runtime.research_worker.learning = runtime.learning
        runtime.research_worker.safety_judge = LiveSafetyJudge(gateway) if gateway else None
    if gateway:
        prompt_policy = gateway.prompt_policy
        model.runtime_prompt_policy = prompt_policy
        conversation_model.runtime_prompt_policy = prompt_policy
        research_model.runtime_prompt_policy = prompt_policy
        runtime.goal_programs.compiler.runtime_prompt_policy = prompt_policy
    runtime.observer = ExperienceObserver(db, runtime.events, runtime.evolution, thread_events=runtime.conversation.events)
    runtime.observer.learning = runtime.learning
    runtime.candidate_generator = EvolutionCandidateGenerator(
        runtime.evolution, runtime.behavior, LivePromptCandidateProposer(gateway) if gateway else None,
    )
    from .learning_prompt import PromptLearning
    runtime.learning.prompt_learning = PromptLearning(runtime.learning, runtime.candidate_generator, root / "learning_suites", gateway)
    _wire_learning_pipeline(runtime, gateway)
    # Observation is a local projection only. Paid proposal generation is
    # started exclusively through an explicitly approved generation batch.
    runtime.observer_worker = ManagedExperienceObserver(runtime.observer)
    runtime.agent_tasks = AgentTaskService(db, thread_events=runtime.conversation.events, evolution=runtime.evolution)
    runtime.agent_worker = ManagedAgentWorker(
        runtime.agent_tasks, LiveExpertModel(gateway, thinking=False) if gateway else None,
        safety_judge=LiveSafetyJudge(gateway) if gateway else None,
    )
    runtime.safety_judge = LiveSafetyJudge(gateway) if gateway else None
    from .model_readiness import ModelReadinessService
    runtime.model_readiness = ModelReadinessService(db, runtime.behavior)
    runtime.expert_advisor = ExpertAdvisoryService(runtime.agent_tasks, runtime.behavior) if gateway else None
    runtime.goal_programs.automatic_expert_advice = False
    if runtime.expert_advisor is not None:
        runtime.goal_programs.expert_advisor = runtime.expert_advisor
        if runtime.research_worker is not None:
            runtime.research_worker.expert_advisor = runtime.expert_advisor
    return runtime
