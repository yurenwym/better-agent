from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .plan_documents import PlanDocumentVersion
from .runtime import PlanDraft


class PlanCompilationError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionSource:
    document_id: str | None
    version_id: str | None
    content_hash: str | None
    title: str
    markdown: str


class PlanExecutionCompiler:
    """Compile one fixed Markdown snapshot into an uncommitted PlanDraft."""

    def __init__(self, model) -> None:
        self.model = model

    async def compile(
        self,
        *,
        source: ExecutionSource,
        fallback_content: str,
        previous_plan: Any | None = None,
    ) -> PlanDraft:
        markdown = source.markdown or fallback_content
        goal = {
            "title": source.title,
            "description": markdown,
            "source_document_version_id": source.version_id,
        }
        interactions = [markdown]
        if previous_plan is not None:
            interactions.append("previous structured plan: " + str(previous_plan))
        draft = await self.model.plan(goal, interactions)
        if not isinstance(draft, PlanDraft):
            raise PlanCompilationError("structured plan compiler returned an invalid draft")
        return self._validate(draft)

    @staticmethod
    def _validate(draft: PlanDraft) -> PlanDraft:
        if not isinstance(draft.summary, str) or len(draft.summary) > 4000:
            raise PlanCompilationError("structured plan summary is invalid")
        if not isinstance(draft.steps, list) or not draft.steps:
            raise PlanCompilationError("structured plan must contain at least one step")
        seen: set[str] = set()
        normalized: list[dict[str, str]] = []
        allowed_statuses = {"pending", "completed", "cancelled"}
        for index, raw in enumerate(draft.steps):
            if not isinstance(raw, dict):
                raise PlanCompilationError("structured plan step is invalid")
            raw_id = raw.get("id")
            raw_title = raw.get("title", "")
            raw_description = raw.get("description", "")
            raw_status = raw.get("status", "pending")
            if raw_id is not None and not isinstance(raw_id, str):
                raise PlanCompilationError("structured plan step id is invalid")
            if not isinstance(raw_title, str) or not isinstance(raw_description, str) or not isinstance(raw_status, str):
                raise PlanCompilationError("structured plan step fields are invalid")
            step_id = (raw_id or f"step-{index + 1}").strip()
            title = raw_title.strip()
            description = raw_description
            status = raw_status
            if not step_id or len(step_id) > 120 or step_id in seen:
                raise PlanCompilationError("structured plan contains duplicate step ids")
            if not title or len(title) > 240 or len(description) > 4000:
                raise PlanCompilationError("structured plan step length is invalid")
            if status not in allowed_statuses:
                raise PlanCompilationError("structured plan step status is invalid")
            seen.add(step_id)
            normalized.append({"id": step_id, "title": title, "description": description, "status": status})
        return PlanDraft(normalized, draft.summary)


def source_from_document(version: PlanDocumentVersion) -> ExecutionSource:
    return ExecutionSource(
        document_id=version.plan_document_id,
        version_id=version.id,
        content_hash=version.content_hash,
        title=version.title,
        markdown=version.markdown_content,
    )
