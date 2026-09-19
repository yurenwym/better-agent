def test_context_selects_only_confirmed_scope_memories_in_priority_order() -> None:
    from app.context import ContextAssembler, MemoryForContext

    assembler = ContextAssembler()
    result = assembler.assemble(
        user_instruction="finish this week",
        goal="Ship a proposal",
        plan="Draft -> review",
        step="Draft",
        skill="goal-planning",
        history=["old", "current"],
        tool_results=[{"summary": "tool result", "artifact_ref": None}],
        memories=[
            MemoryForContext("global-1", "global habit", "global", "confirmed"),
            MemoryForContext("project-1", "project preference", "project", "confirmed", project_id="p1"),
            MemoryForContext("candidate", "not allowed", "global", "proposed"),
            MemoryForContext("other", "wrong project", "project", "confirmed", project_id="p2"),
            MemoryForContext("skill-1", "skill preference", "skill", "confirmed", skill_name="goal-planning"),
        ],
        project_id="p1",
        max_chars=10000,
    )

    assert [memory.id for memory in result.memories] == ["global-1", "project-1", "skill-1"]
    assert "not allowed" not in result.text
    assert "wrong project" not in result.text
    assert result.blocks[0].name == "security"
    assert result.blocks[1].name == "user_instruction"


def test_context_crops_once_and_preserves_current_instruction_and_snapshot_hash() -> None:
    from app.context import ContextAssembler

    assembler = ContextAssembler()
    result = assembler.assemble(
        user_instruction="keep this instruction",
        goal="goal",
        plan="plan",
        step="step",
        skill="skill",
        history=["old " * 100, "current"],
        tool_results=[{"summary": "summary", "artifact_ref": "artifact-1", "raw": "large" * 100}],
        memories=[],
        project_id=None,
        max_chars=180,
    )

    assert result.cropped is True
    assert result.crop_count == 1
    assert "keep this instruction" in result.text
    assert result.snapshot_hash
    assert result.snapshot_hash == assembler.assemble(
        user_instruction="keep this instruction",
        goal="goal",
        plan="plan",
        step="step",
        skill="skill",
        history=["old " * 100, "current"],
        tool_results=[{"summary": "summary", "artifact_ref": "artifact-1", "raw": "large" * 100}],
        memories=[],
        project_id=None,
        max_chars=180,
    ).snapshot_hash


def test_context_hard_cap_includes_protected_blocks() -> None:
    from app.context import ContextAssembler
    from app.token_budget import DEFAULT_TOKEN_COUNTER

    result = ContextAssembler().assemble(
        user_instruction="current request",
        goal="goal " * 100,
        plan="plan " * 100,
        step="step " * 100,
        skill="skill " * 100,
        history=["old " * 100],
        tool_results=[],
        memories=[],
        project_id=None,
        max_tokens=240,
    )

    assert DEFAULT_TOKEN_COUNTER.count_text(result.text) <= 240
    assert "current request" in result.text
    assert result.overflow is False
    assert result.cropped is True


def test_context_drops_episode_before_memory_and_keeps_sources_atomic() -> None:
    from app.context import ContextAssembler, MemoryForContext

    result = ContextAssembler().assemble(
        user_instruction="current request",
        goal="goal",
        plan="plan",
        step="step",
        skill="skill",
        history=["recent history"],
        tool_results=[],
        memories=[
            MemoryForContext("revision-1", "important preference " * 8, "global", "confirmed"),
            MemoryForContext("episode-1", "lossy history " * 8, "global", "confirmed", source_type="episode"),
        ],
        project_id=None,
        max_tokens=260,
    )

    assert "episode-1" not in [memory.id for memory in result.memories]
    assert "lossy history" not in result.text
    assert "current request" in result.text

