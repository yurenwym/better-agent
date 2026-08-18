from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_SKILL_TOOLS: dict[str, frozenset[str]] = {
    "goal-planning": frozenset({"local_time", "calculator", "read_note"}),
    "reflection": frozenset({"local_time", "calculator", "read_note"}),
}


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    title: str
    description: str
    content: str
    allowed_tools: frozenset[str] | None = None

    def public_view(self) -> dict[str, object]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "enabled": True,
        }


class SkillCatalog:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else Path(__file__).resolve().parents[2] / "skills"
        self._skills = self._load()

    def list(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def validate(self, names: Iterable[str]) -> tuple[str, ...]:
        selected: list[str] = []
        for name in names:
            if not isinstance(name, str) or name not in self._skills:
                raise ValueError(f"unknown skill: {name}")
            if name not in selected:
                selected.append(name)
        return tuple(selected)

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def context_text(self, selected_names: Iterable[str], phase_name: str) -> str:
        names = list(dict.fromkeys([phase_name, *selected_names]))
        blocks = []
        for name in names:
            skill = self.get(name)
            if skill is None:
                continue
            blocks.append(f"## {skill.title} ({skill.name})\n{skill.content}")
        return "\n\n".join(blocks) or phase_name

    def allowed_tools(self, name: str) -> frozenset[str] | None:
        skill = self.get(name)
        return skill.allowed_tools if skill else None

    def _load(self) -> dict[str, SkillDefinition]:
        if not self.root.is_dir():
            return {}
        skills: dict[str, SkillDefinition] = {}
        for skill_dir in sorted(self.root.iterdir(), key=lambda path: path.name):
            document = skill_dir / "SKILL.md"
            if not skill_dir.is_dir() or not document.is_file() or not _safe_name(skill_dir.name):
                continue
            content = document.read_text(encoding="utf-8").strip()
            if not content:
                continue
            title = _first_heading(content) or skill_dir.name.replace("-", " ").title()
            description = _purpose_description(content) or "本地可选工作技能。"
            skills[skill_dir.name] = SkillDefinition(
                name=skill_dir.name,
                title=title,
                description=description,
                content=content,
                allowed_tools=_SKILL_TOOLS.get(skill_dir.name),
            )
        return skills


def _safe_name(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value))


def _first_heading(content: str) -> str | None:
    for line in content.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return None


def _purpose_description(content: str) -> str | None:
    lines = content.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip().lower() == "## purpose") + 1
    except StopIteration:
        return None
    for line in lines[start:]:
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            return None
        return text.lstrip("- ")
    return None
