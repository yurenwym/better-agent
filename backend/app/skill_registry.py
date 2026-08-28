from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .skill_platform import SkillPlatform


BUILTIN_TOOL_POLICIES = {
    "goal-planning": ["local_time", "calculator", "read_note"],
    "reflection": ["local_time", "calculator", "read_note"],
}
BUILTIN_PHASES = {
    "goal-planning": ["ask", "planner", "executor"],
    "reflection": ["reflector"],
}


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    title: str
    description: str
    content: str
    version_id: str
    package_digest: str
    grant_digest: str
    allowed_tools: frozenset[str]

    def public_view(self) -> dict[str, object]:
        return {
            "name": self.name, "title": self.title, "description": self.description,
            "version_id": self.version_id, "package_digest": self.package_digest, "enabled": True,
        }


class SkillCatalog:
    def __init__(self, root: str | Path | None = None, platform: SkillPlatform | None = None) -> None:
        self.root = Path(root) if root else Path(__file__).resolve().parents[2] / "skills"
        self.platform = platform
        if self.platform is not None:
            self._bootstrap_builtins()

    def list(self) -> list[SkillDefinition]:
        if self.platform is None:
            return self._legacy_load()
        return [self._definition(item) for item in self.platform.enabled_versions()]

    def validate(self, names: Iterable[str]) -> tuple[str, ...]:
        installed = {item.name for item in self.list()}
        selected: list[str] = []
        for name in names:
            if not isinstance(name, str) or name not in installed:
                raise ValueError(f"unknown skill: {name}")
            if name not in selected:
                selected.append(name)
        return tuple(selected)

    def version_ids(self, names: Iterable[str]) -> list[str]:
        if self.platform is None:
            return []
        return [self.platform.default_version(name)["version_id"] for name in self.validate(names)]

    def get(self, name: str) -> SkillDefinition | None:
        return next((item for item in self.list() if item.name == name), None)

    def context_text(self, selected_names: Iterable[str], phase_name: str) -> str:
        blocks = []
        for name in dict.fromkeys([phase_name, *selected_names]):
            skill = self.get(name)
            if skill is not None:
                blocks.append(f"## {skill.title} ({skill.name})\n{skill.content}")
        return "\n\n".join(blocks) or phase_name

    def allowed_tools(self, name: str) -> frozenset[str] | None:
        skill = self.get(name)
        return skill.allowed_tools if skill else None

    def _bootstrap_builtins(self) -> None:
        if not self.root.is_dir(): return
        for directory in sorted(self.root.iterdir(), key=lambda path: path.name):
            document = directory / "SKILL.md"
            if not directory.is_dir() or not document.is_file() or directory.name not in BUILTIN_TOOL_POLICIES: continue
            content = document.read_text(encoding="utf-8").strip()
            self.platform.bootstrap_builtin(
                directory.name, _first_heading(content) or directory.name,
                _purpose_description(content) or "本地内置技能。", content,
                BUILTIN_TOOL_POLICIES[directory.name], BUILTIN_PHASES[directory.name],
            )

    @staticmethod
    def _definition(item: dict) -> SkillDefinition:
        return SkillDefinition(
            item["name"], item["title"], item["description"], item["content"], item["version_id"],
            item["package_digest"], item["grant_digest"], frozenset(item["granted_tools"]),
        )

    def _legacy_load(self) -> list[SkillDefinition]:
        if not self.root.is_dir(): return []
        items = []
        for directory in sorted(self.root.iterdir(), key=lambda path: path.name):
            document = directory / "SKILL.md"
            if not directory.is_dir() or not document.is_file() or directory.name not in BUILTIN_TOOL_POLICIES: continue
            content = document.read_text(encoding="utf-8").strip()
            items.append(SkillDefinition(
                directory.name, _first_heading(content) or directory.name,
                _purpose_description(content) or "本地内置技能。", content,
                directory.name, "legacy", "legacy", frozenset(BUILTIN_TOOL_POLICIES[directory.name]),
            ))
        return items


def _first_heading(content: str) -> str | None:
    for line in content.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match: return match.group(1)
    return None


def _purpose_description(content: str) -> str | None:
    lines = content.splitlines()
    try: start = next(index for index, line in enumerate(lines) if line.strip().lower() == "## purpose") + 1
    except StopIteration: return None
    for line in lines[start:]:
        text = line.strip()
        if not text: continue
        if text.startswith("#"): return None
        return text.lstrip("- ")
    return None
