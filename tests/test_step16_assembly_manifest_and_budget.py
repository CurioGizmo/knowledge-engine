"""Тесты шага 16: Объяснимая сборка (assembly manifest) и контроль бюджета TokenGuard без потери правил."""

from __future__ import annotations

from pathlib import Path
import pytest

from mcp_skills.errors import AssemblyBudgetExceededError
from mcp_skills.flow_engine import FlowEngine
from mcp_skills.flow_parser import parse_flow_markdown
from mcp_skills.server import load_skill, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer
from mcp_skills.token_guard import TokenGuard


def _setup_catalog(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer, Path]:
    skills_dir = tmp_path / "skills"
    shared_blocks_dir = tmp_path / "blocks"
    shared_blocks_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)

    # Общий блок stack-invariants: default.md и python.md
    inv_dir = shared_blocks_dir / "stack-invariants"
    inv_dir.mkdir(parents=True)
    (inv_dir / "default.md").write_text("# Shared Default Invariants\n- Standard quality gates\n")
    (inv_dir / "python.md").write_text("# Python Invariants\n- Type hints, ruff, pytest\n")

    # Скилл test-plan
    plan_dir = skills_dir / "test-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-plan\n"
        "title: Test Plan Skill\n"
        "category: planning\n"
        "available_slots: [stack-invariants, core-logic]\n"
        "optional_slots: [optional-hints]\n"
        "---\n"
        "# Plan Skeleton\n\n"
        "<!-- SLOT: stack-invariants -->\n\n"
        "<!-- SLOT: core-logic -->\n\n"
        "## Final Mandatory Rule\n"
        "- NEVER BREAK BACKWARD COMPATIBILITY\n"
    )
    core_blocks = plan_dir / "blocks" / "core-logic"
    core_blocks.mkdir(parents=True)
    (core_blocks / "default.md").write_text("# Default Core Logic\n- Generic step execution\n")
    (core_blocks / "python.md").write_text(
        "# Python Core Logic\n- asyncio and pydantic execution\n"
    )

    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    import os

    os.environ["MCP_SKILLS_PATH"] = str(tmp_path)

    return storage, sync, tmp_path


def test_assembly_manifest_unmatched_technology(tmp_path: Path) -> None:
    """Ruby без специфичного варианта получает fallback с unmatched-технологией в assembly."""
    storage, sync, root = _setup_catalog(tmp_path)

    res = load_skill(
        "test-plan",
        stack=["ruby"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )

    manifest = res.get("assembly", {})
    assert "ruby" in manifest.get("requested_stack", [])
    assert manifest.get("matched_technologies") == []
    assert manifest.get("unmatched_technologies") == ["ruby"]

    # Секции получили статус default
    sections = manifest.get("sections", {})
    assert "core-logic" in sections
    assert sections["core-logic"]["status"] == "default"
    assert "local:default.md" in sections["core-logic"]["source"]

    assert "stack-invariants" in sections
    assert sections["stack-invariants"]["status"] == "default"
    assert "shared:default.md" in sections["stack-invariants"]["source"]

    # stack_applied сохранён для совместимости
    assert "ruby" in res["stack_applied"]
    assert res["status"] == "complete"
    assert manifest["status"] == "complete"


def test_assembly_manifest_matched_technology(tmp_path: Path) -> None:
    """Python матчит специфичные блоки и отображается в matched_technologies."""
    storage, sync, root = _setup_catalog(tmp_path)

    res = load_skill(
        "test-plan",
        stack=["python"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )

    manifest = res.get("assembly", {})
    assert "python" in manifest.get("matched_technologies", [])
    assert manifest.get("unmatched_technologies") == []

    sections = manifest.get("sections", {})
    assert sections["core-logic"]["status"] == "matched"
    assert "python" in sections["core-logic"]["matched_technologies"]
    assert "local:python.md" in sections["core-logic"]["source"]

    assert sections["stack-invariants"]["status"] == "matched"
    assert "python" in sections["stack-invariants"]["matched_technologies"]
    assert "shared:python.md" in sections["stack-invariants"]["source"]

    assert res["status"] == "complete"


def test_section_typo_raises_error(tmp_path: Path) -> None:
    """Опечатка в секции (touches) вызывает понятную ошибку с перечислением доступных слотов."""
    storage, sync, root = _setup_catalog(tmp_path)

    with pytest.raises(
        ValueError, match="Неизвестные секции для скилла 'test-plan': \\['core-typo'\\]"
    ):
        load_skill(
            "test-plan",
            touches=["core-typo"],
            storage=storage,
            synchronizer=sync,
            project_root=root,
        )


def test_missing_obligatory_block_reports_incomplete(tmp_path: Path) -> None:
    """Отсутствующий блок для слота даёт неполный статус и исключается из slots_filled."""
    storage, sync, root = _setup_catalog(tmp_path)

    # Создаём скилл с пустым слотом без блоков
    skill_dir = root / "skills" / "empty-slot-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: empty-slot-skill\n"
        "title: Empty Slot Skill\n"
        "category: testing\n"
        "available_slots: [no-such-block-slot]\n"
        "---\n"
        "# Empty Slot\n<!-- SLOT: no-such-block-slot -->\n"
    )
    sync.sync()

    res = load_skill(
        "empty-slot-skill",
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )

    assert res["status"] == "incomplete"
    assert "no-such-block-slot" in res["missing_slots"]
    assert "no-such-block-slot" not in res["slots_filled"]
    assert res["assembly"]["status"] == "incomplete"
    assert res["assembly"]["sections"]["no-such-block-slot"]["status"] == "missing"


def test_required_rule_at_end_not_lost_when_core_exceeds_budget(tmp_path: Path) -> None:
    """При превышении обязательным ядром хвостовое правило не отрезается молча, а возвращается ошибка."""
    storage, sync, root = _setup_catalog(tmp_path)

    # Создаём огромный блок обязательного ядра (> 12000 токенов)
    huge_core_text = "- MANDATORY CORE RULE LINE\n" * 5000
    plan_dir = root / "skills" / "huge-skill"
    plan_dir.mkdir(parents=True)
    (plan_dir / "SKILL.md").write_text(
        "---\n"
        "name: huge-skill\n"
        "title: Huge Skill\n"
        "category: planning\n"
        "available_slots: [huge-core]\n"
        "---\n"
        "# Huge Skeleton\n\n"
        "<!-- SLOT: huge-core -->\n\n"
        "## Critical Final Rule\n"
        "- THIS MUST NEVER BE SILENTLY DROPPED\n"
    )
    blocks = plan_dir / "blocks" / "huge-core"
    blocks.mkdir(parents=True)
    (blocks / "default.md").write_text(huge_core_text)
    sync.sync()

    # Попытка load_skill должна выбросить AssemblyBudgetExceededError,
    # не отрезая тихо 'Critical Final Rule' в конце!
    with pytest.raises(AssemblyBudgetExceededError) as exc_info:
        load_skill(
            "huge-skill",
            storage=storage,
            synchronizer=sync,
            project_root=root,
        )

    err = exc_info.value
    assert err.estimated_tokens > err.budget
    assert err.overflow_tokens > 0
    assert "remediation" in dir(err) and err.remediation


def test_examples_dropped_while_mandatory_tail_rule_preserved(tmp_path: Path) -> None:
    """Если превышение вызвано примерами, примеры опускаются, а хвостовое правило сохраняется."""
    storage, sync, root = _setup_catalog(tmp_path)

    huge_examples = "\n## Примеры\n" + ("```python\n# example code\n```\n" * 4000)
    skill_dir = root / "skills" / "skill-with-examples"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: skill-with-examples\n"
        "title: Skill With Examples\n"
        "category: planning\n"
        "available_slots: []\n"
        "---\n"
        "# Skill With Big Examples\n\n"
        "## Invariant Rule\n"
        "- MUST PRESERVE THIS\n"
        f"{huge_examples}\n\n"
        "## Final Mandatory Rule\n"
        "- MUST PRESERVE THIS TAIL RULE\n"
    )
    sync.sync()

    res = load_skill(
        "skill-with-examples",
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )

    assert res["was_truncated"] is True
    # Финальное хвостовое правило НЕ потеряно!
    assert "## Final Mandatory Rule" in res["content"]
    assert "MUST PRESERVE THIS TAIL RULE" in res["content"]
    assert "Детальные примеры опущены" in res["content"]


def test_flow_combined_instruction_budget_exceeded(tmp_path: Path) -> None:
    """Синтетическая длинная инструкция этапа + скил не получают skills_truncated=False при оверфлоу."""
    storage, sync, root = _setup_catalog(tmp_path)

    # Создаём инструкцию на ~8000 токенов и скилл на ~6000 токенов (суммарно > 12000 токенов)
    long_instruction = "Инструкция для этапа flow: проверить архитектурные контракты. " * 1500
    flow_dir = root / "flows"
    flow_dir.mkdir(parents=True)
    flow_file = flow_dir / "test-flow.md"
    flow_file.write_text(
        "---\n"
        "name: test-flow\n"
        "title: Long Instruction Flow\n"
        "category: test\n"
        "---\n\n"
        "# Flow Definition\n\n"
        "## 1. Stage Research [id: s1]\n"
        '<!-- meta: {"skills": ["test-plan"]} -->\n'
        f"{long_instruction}\n"
    )

    engine = FlowEngine(project_root=root, skills_storage=storage, skills_dir=root)

    with pytest.raises(AssemblyBudgetExceededError):
        engine.start_flow("test-flow")
    # Ошибка подготовки не оставляет активную сессию с недоставленным этапом.
    assert engine._get_storage(root).find_active_sessions(str(root)) == []


def test_token_budget_measurements_and_no_200_promise(tmp_path: Path) -> None:
    """Измерение реальных токенов пустого/одиночного/смешанного стека и отсутствие обещания ~200 токенов."""
    storage, sync, root = _setup_catalog(tmp_path)
    guard = TokenGuard()

    # 1. Пустой стек
    res_empty = load_skill(
        "test-plan", stack=[], storage=storage, synchronizer=sync, project_root=root
    )
    tokens_empty = guard.count_tokens(res_empty["content"])

    # 2. Одиночный стек (python)
    res_single = load_skill(
        "test-plan", stack=["python"], storage=storage, synchronizer=sync, project_root=root
    )
    tokens_single = guard.count_tokens(res_single["content"])

    # 3. Смешанный стек (python, sqlite, mcp)
    res_mixed = load_skill(
        "test-plan",
        stack=["python", "sqlite", "mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    tokens_mixed = guard.count_tokens(res_mixed["content"])

    # Проверяем, что размеры реальны и измеримы (>0 токенов, укладываются в бюджет)
    assert 10 < tokens_empty < 1000
    assert 10 < tokens_single < 2000
    assert 10 < tokens_mixed < 3000

    # 4. Проверяем overview search_skills
    overview = search_skills(storage=storage, synchronizer=sync, project_root=root)
    assert overview["mode"] == "overview"
    # В docstring инструмента и в overview нет устаревшего обещания "~200 токенов"
    from mcp_skills.server import create_server

    server = create_server(project_root=root, skills_dir=root / "skills")
    search_tool = server._tool_manager._tools["search_skills"]
    assert "~200 токенов" not in (search_tool.description or "")
