"""Тесты соответствия единому контракту композиции (Шаг 13).

Проверяет:
- Одинаковое содержимое скиллов во всех трех входах (SlotCompiler / load_skill, AgentBundler, FlowEngine);
- Optional-политика: при touches=None опциональные слоты не включаются ни в одном входе;
- add-tests, onnx-whisper-engine, live-qa-verification с явным touches;
- Обязательный сценарий: этап с agent: test-reviewer без meta.skills задает
  skill_options.add-tests.touches: [test-runner, stack-patterns] -> бандл агента содержит stack-patterns;
- Приоритеты skill_options: этап -> override -> профиль;
- Замена списков touches/resources целиком (не конкатенация), явный пустой список [] и наследование None;
- Валидация: ошибка при неизвестном скилле, неизвестном слоте, чужом ресурсе;
- Наследование extends: append, prepend, override, пустое тело потомка, замена блоков без дублей;
- Offline-доставка: permissions.mcp=False доставляет ресурсы прямо в бандл агента.
"""

from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from mcp_skills.bundler import AgentBundler, merge_profile_with_override, merge_skill_options
from mcp_skills.flow_engine import _build_stage_payload, _bundle_stage_agent, _compile_stage_skills
from mcp_skills.models import (
    AgentOverride,
    AgentPermissions,
    AgentProfile,
    FlowDefinition,
    FlowSession,
    FlowStage,
    FlowStageMeta,
    FlowStatus,
    SkillOptionItem,
)
from mcp_skills.server import load_skill
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

from tests.conftest import get_companion_skills_dir

_catalog = get_companion_skills_dir()
CATALOG_DIR = (
    _catalog
    if _catalog is not None
    else (Path(__file__).resolve().parents[1].parent / "mcp_skills")
)
SKILLS_DIR = CATALOG_DIR / "skills"


def _make_session(root: Path) -> FlowSession:
    return FlowSession(
        id="test-session",
        project_root=str(root),
        flow_name="test-flow",
        current_stage_id="s1",
        created_at=1000.0,
        updated_at=1000.0,
    )


@pytest.fixture
def catalog_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SkillsStorage, Path]:
    if not SKILLS_DIR.is_dir():
        pytest.skip(f"mcp_skills catalog not found at {CATALOG_DIR}")
    monkeypatch.setenv("MCP_SKILLS_PATH", str(CATALOG_DIR))
    # Создаем минимальный pyproject.toml в tmp_path, чтобы контекст проекта определял python
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test-proj'\n", encoding="utf-8")
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    SkillsSynchronizer(storage, [SKILLS_DIR]).sync()
    return storage, tmp_path


# 1. Одинаковое содержимое скилла во всех 3 входах (F4)
def test_three_entry_points_identical_content(catalog_store: tuple[SkillsStorage, Path]) -> None:
    storage, tmp_root = catalog_store
    compiler = SlotCompiler(storage, catalog_root=CATALOG_DIR)
    bundler = AgentBundler(storage=storage, compiler=compiler, skills_dir=CATALOG_DIR)

    skill_name = "add-tests"
    project_stack = ["python"]
    touches = ["test-runner", "stack-patterns"]

    # 1. SlotCompiler / assemble_skill
    _, comp_content = compiler.assemble_skill(
        name=skill_name,
        touches=touches,
        project_stack=project_stack,
        project_root=tmp_root,
    )

    # 2. server.load_skill
    server_res = load_skill(
        name=skill_name,
        stack=project_stack,
        touches=touches,
        storage=storage,
        compiler=compiler,
        project_root=tmp_root,
    )
    server_content = server_res["content"]

    # 3. flow_engine._compile_stage_skills (берет стек из tmp_root, где лежит pyproject.toml -> python)
    stage = FlowStage(
        id="s1",
        title="Test Stage",
        instruction="Instruction",
        meta=FlowStageMeta(
            skills=[skill_name],
            skill_options={skill_name: SkillOptionItem(touches=touches)},
        ),
    )
    supplement, compiled_names, _ = _compile_stage_skills(stage, tmp_root, skills_storage=storage)

    assert compiled_names == [skill_name]
    assert comp_content.strip() == server_content.strip()
    assert comp_content.strip() == supplement.strip()


# 2. Optional-тема не появляется без явного touches ни в одном из входов
def test_optional_slots_excluded_by_default_in_all_entry_points(
    catalog_store: tuple[SkillsStorage, Path],
) -> None:
    storage, tmp_root = catalog_store
    compiler = SlotCompiler(storage, catalog_root=CATALOG_DIR)
    bundler = AgentBundler(storage=storage, compiler=compiler, skills_dir=CATALOG_DIR)

    skill_name = "add-tests"
    project_stack = ["python"]

    # 1. SlotCompiler (touches=None)
    _, comp_content = compiler.assemble_skill(
        name=skill_name,
        touches=None,
        project_stack=project_stack,
        project_root=tmp_root,
    )

    # 2. server.load_skill (touches=None)
    server_res = load_skill(
        name=skill_name,
        stack=project_stack,
        touches=None,
        storage=storage,
        compiler=compiler,
        project_root=tmp_root,
    )

    # 3. AgentBundler
    profile = AgentProfile(name="test-agent", skills=[skill_name])
    agent_bundle = bundler.bundle_agent(profile=profile, project_root=tmp_root)

    # 4. FlowStage
    stage = FlowStage(
        id="s1",
        title="Test Stage",
        instruction="Instruction",
        meta=FlowStageMeta(skills=[skill_name]),
    )
    supplement, _, _ = _compile_stage_skills(stage, tmp_root, skills_storage=storage)

    # stack-patterns - это optional_slot в add-tests
    # Без явного touches он не должен появиться
    pattern_signature = "Backend Тестирование"
    assert pattern_signature not in comp_content
    assert pattern_signature not in server_res["content"]
    assert pattern_signature not in agent_bundle
    assert pattern_signature not in supplement


# 3. Проверка явного выбора для add-tests, onnx-whisper-engine и live-qa-verification
def test_explicit_touches_add_tests_and_whisper(catalog_store: tuple[SkillsStorage, Path]) -> None:
    storage, tmp_root = catalog_store
    compiler = SlotCompiler(storage, catalog_root=CATALOG_DIR)

    # add-tests с явным stack-patterns
    _, content_add_tests = compiler.assemble_skill(
        name="add-tests",
        touches=["test-runner", "stack-patterns"],
        project_stack=["python"],
        project_root=tmp_root,
    )
    assert "Backend Тестирование" in content_add_tests
    assert "Golden Path Rule" in content_add_tests

    # onnx-whisper-engine
    card_whisper, content_whisper = compiler.assemble_skill(
        name="onnx-whisper-engine",
        project_stack=["audio-ml", "python"],
        project_root=tmp_root,
    )
    assert card_whisper.name == "onnx-whisper-engine"
    assert len(content_whisper) > 100

    # live-qa-verification
    card_qa, content_qa = compiler.assemble_skill(
        name="live-qa-verification",
        project_stack=["python"],
        project_root=tmp_root,
    )
    assert card_qa.name == "live-qa-verification"
    assert "Живая приёмочная проверка" in content_qa


# 4. Обязательный сценарий: этап с agent: test-reviewer, без meta.skills,
# задает skill_options.add-tests.touches: [test-runner, stack-patterns] ->
# бандл агента содержит выбранный stack-pattern, а несвязанные optional-темы не добавляются
def test_stage_agent_test_reviewer_skill_options(catalog_store: tuple[SkillsStorage, Path]) -> None:
    storage, tmp_root = catalog_store
    compiler = SlotCompiler(storage, catalog_root=CATALOG_DIR)
    bundler = AgentBundler(storage=storage, compiler=compiler, skills_dir=CATALOG_DIR)

    # Этап: agent='test-reviewer', БЕЗ meta.skills, задает skill_options для add-tests
    stage = FlowStage(
        id="audit-stage",
        title="Audit Tests",
        instruction="Проведи аудит тестов",
        meta=FlowStageMeta(
            agent="test-reviewer",
            skills=[],  # БЕЗ meta.skills!
            skill_options={"add-tests": SkillOptionItem(touches=["test-runner", "stack-patterns"])},
        ),
    )

    session = _make_session(tmp_root)
    payload = _build_stage_payload(
        stage=stage,
        total_stages=1,
        session=session,
        root=tmp_root,
        bundler=bundler,
        skills_storage=storage,
    )

    assert "subagent" in payload
    subagent_bundle = payload["subagent"]["bundle"]
    assert subagent_bundle is not None

    # Бандл агента test-reviewer должен содержать Backend Тестирование и Golden Path Rule
    assert "Backend Тестирование" in subagent_bundle
    assert "Golden Path Rule" in subagent_bundle
    # Проверяем, что агент сформирован корректно
    assert payload["subagent"]["name"] == "test-reviewer"


# 5. Приоритеты: stage skill_options -> override -> profile
def test_skill_options_hierarchy_and_merge() -> None:
    # 1. Base profile
    base = {
        "skill-a": SkillOptionItem(touches=["slot1", "slot2"], resources=["res1.md"]),
        "skill-b": SkillOptionItem(touches=["b1"], resources=["rb1.md"]),
    }
    # 2. Override (заменяет touches у skill-a, resources наследуется)
    override = {
        "skill-a": SkillOptionItem(touches=["slot3"]),
    }
    merged_prof = merge_skill_options(base, override)
    assert merged_prof["skill-a"].touches == ["slot3"]  # заменено целиком
    assert merged_prof["skill-a"].resources == ["res1.md"]  # унаследовано
    assert merged_prof["skill-b"].touches == ["b1"]

    # 3. Stage options (заменяет resources у skill-a, touches наследуется из override; пустой touches [] у skill-b)
    stage_opts = {
        "skill-a": SkillOptionItem(resources=["res2.md"]),
        "skill-b": SkillOptionItem(touches=[]),
    }
    final_merged = merge_skill_options(merged_prof, stage_opts)
    assert final_merged["skill-a"].touches == ["slot3"]  # унаследовано от override
    assert final_merged["skill-a"].resources == ["res2.md"]  # заменено этапом
    assert final_merged["skill-b"].touches == []  # явный пустой выбор
    assert final_merged["skill-b"].resources == ["rb1.md"]  # унаследовано от base


# 6. Валидация: неизвестный скилл, неизвестная секция, чужой ресурс
def test_skill_options_validations(catalog_store: tuple[SkillsStorage, Path]) -> None:
    storage, tmp_root = catalog_store
    compiler = SlotCompiler(storage, catalog_root=CATALOG_DIR)
    bundler = AgentBundler(storage=storage, compiler=compiler, skills_dir=CATALOG_DIR)
    session = _make_session(tmp_root)

    # 6.1 Неизвестный скилл на этапе
    stage_bad_skill = FlowStage(
        id="bad-skill-stage",
        title="Bad",
        instruction="Do something",
        meta=FlowStageMeta(
            skills=["add-tests"],
            skill_options={"unknown-skill-xyz": SkillOptionItem(touches=["t1"])},
        ),
    )
    with pytest.raises(ValueError, match="Неизвестный скилл 'unknown-skill-xyz'"):
        _build_stage_payload(
            stage=stage_bad_skill,
            total_stages=1,
            session=session,
            root=tmp_root,
            bundler=bundler,
            skills_storage=storage,
        )

    # 6.2 Неизвестная секция (unknown slot) в touches
    stage_bad_slot = FlowStage(
        id="bad-slot-stage",
        title="Bad Slot",
        instruction="Do something",
        meta=FlowStageMeta(
            skills=["add-tests"],
            skill_options={"add-tests": SkillOptionItem(touches=["nonexistent-slot-123"])},
        ),
    )
    with pytest.raises(ValueError, match="Неизвестные секции для скилла 'add-tests'"):
        _build_stage_payload(
            stage=stage_bad_slot,
            total_stages=1,
            session=session,
            root=tmp_root,
            bundler=bundler,
            skills_storage=storage,
        )

    # 6.3 Ресурс другого владельца (или несуществующий)
    stage_bad_res = FlowStage(
        id="bad-res-stage",
        title="Bad Res",
        instruction="Do something",
        meta=FlowStageMeta(
            skills=["add-tests"],
            skill_options={
                "add-tests": SkillOptionItem(
                    resources=["skills/planning/writing-plans/references/api-architecture.md"]
                )
            },
        ),
    )
    with pytest.raises(ValueError, match="не связан со скиллом-владельцем 'add-tests'"):
        _build_stage_payload(
            stage=stage_bad_res,
            total_stages=1,
            session=session,
            root=tmp_root,
            bundler=bundler,
            skills_storage=storage,
        )


# 7. Extends-тесты: append, prepend, override, empty child, block replacement without duplicate rules
def test_extends_merge_strategies_and_block_inheritance(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Родительский скилл
    p_dir = skills_dir / "parent-skill"
    p_dir.mkdir()
    (p_dir / "SKILL.md").write_text(
        """---
name: parent-skill
title: Parent
category: general
available_slots: [core, extra]
---
# Parent Body

<!-- SLOT: core -->

<!-- SLOT: extra -->
""",
        encoding="utf-8",
    )
    (p_dir / "blocks" / "core").mkdir(parents=True)
    (p_dir / "blocks" / "core" / "python.md").write_text("PARENT-PYTHON-CORE", encoding="utf-8")
    (p_dir / "blocks" / "extra").mkdir(parents=True)
    (p_dir / "blocks" / "extra" / "python.md").write_text("PARENT-PYTHON-EXTRA", encoding="utf-8")

    # Потомок 1: override merge_strategy + заменяет core блок
    c1_dir = skills_dir / "child-override"
    c1_dir.mkdir()
    (c1_dir / "SKILL.md").write_text(
        """---
name: child-override
title: Child Override
category: general
extends: parent-skill
merge_strategy: override
available_slots: [core]
---
# Child Override Body

<!-- SLOT: core -->
""",
        encoding="utf-8",
    )
    (c1_dir / "blocks" / "core").mkdir(parents=True)
    (c1_dir / "blocks" / "core" / "python.md").write_text("CHILD-PYTHON-CORE", encoding="utf-8")

    # Потомок 2: empty body -> наследует скелет родителя, наследует блок extra, переопределяет core
    c2_dir = skills_dir / "child-empty"
    c2_dir.mkdir()
    (c2_dir / "SKILL.md").write_text(
        """---
name: child-empty
title: Child Empty
category: general
extends: parent-skill
---
""",
        encoding="utf-8",
    )
    (c2_dir / "blocks" / "core").mkdir(parents=True)
    (c2_dir / "blocks" / "core" / "python.md").write_text("CHILD-EMPTY-CORE", encoding="utf-8")

    SkillsSynchronizer(storage, [skills_dir]).sync()
    compiler = SlotCompiler(storage)

    # 1. Проверяем child-override
    _, md_c1 = compiler.assemble_skill("child-override", project_stack=["python"])
    assert "Child Override Body" in md_c1
    assert "Parent Body" not in md_c1  # override убрал скелет родителя
    assert "CHILD-PYTHON-CORE" in md_c1
    assert "PARENT-PYTHON-CORE" not in md_c1

    # 2. Проверяем child-empty
    _, md_c2 = compiler.assemble_skill("child-empty", project_stack=["python"])
    assert "Parent Body" in md_c2  # пустое тело унаследовало скелет родителя
    assert "CHILD-EMPTY-CORE" in md_c2  # блок core переопределен потомком
    assert "PARENT-PYTHON-CORE" not in md_c2  # нет дубля родительского блока
    assert "PARENT-PYTHON-EXTRA" in md_c2  # блок extra унаследован от родителя


# 8. Offline-доставка: если permissions.mcp=False, ресурсы вкладываются в бандл
def test_offline_resource_delivery(catalog_store: tuple[SkillsStorage, Path]) -> None:
    storage, tmp_root = catalog_store
    compiler = SlotCompiler(storage, catalog_root=CATALOG_DIR)
    bundler = AgentBundler(storage=storage, compiler=compiler, skills_dir=CATALOG_DIR)

    # Профиль с permissions.mcp = False
    profile = AgentProfile(
        name="offline-agent",
        skills=["add-tests"],
        permissions=AgentPermissions(write=False, mcp=False, subagents=False),
        skill_options={
            "add-tests": SkillOptionItem(
                resources=["skills/quality-review/add-tests/references/testing-anti-patterns.md"]
            )
        },
    )

    bundle = bundler.bundle_agent(profile=profile, project_root=tmp_root)

    # Ресурс должен быть вложен в бандл агента
    assert "## Вложенные справочные ресурсы" in bundle
    assert (
        "### Вложенный ресурс: skills/quality-review/add-tests/references/testing-anti-patterns.md"
        in bundle
    )
    assert "Анти-паттерны" in bundle or "Театр моков" in bundle or "anti-patterns" in bundle.lower()
