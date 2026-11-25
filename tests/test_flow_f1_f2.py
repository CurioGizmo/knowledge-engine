"""Тесты F1 и F2 плана review-fixes (models / flow_parser / flow_engine).

F1 — FlowStageMeta.skills/required_tools (имена, не контент) + next_flows:
- парсинг и валидация новых полей meta (модель extra='forbid' — парсер
  синхронно расширен);
- объявленные skills компилируются в инструкцию этапа через SlotCompiler
  (тот же путь, что бандлинг агентов) под бюджетом TokenGuard;
- отсутствующий скилл — предупреждение в инструкции, этап не падает;
- required_tools валидируется и экспонируется в payload этапа;
- next_flows из frontmatter флоу попадает в completed-ответ step_flow.

F2 — однозначные типы артефакта в validate_plan_gate/_check_deliverable_file:
- строка-путь: файл существует → passed; файла нет → failed с понятной
  причиной (раньше несуществующий путь-строка молча принималась как «текст
  плана» — T1 из verdict_6);
- deliverable={'file_path': ...} — путь; deliverable={'content': ...} — текст;
- target_file не глушит deliverable.file_path: несуществующий целевый файл
  больше не топит корректный file_path (T4 из verdict_6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_skills.errors import InvalidFlowFormatError
from mcp_skills.flow_engine import FlowEngine, step_flow
from mcp_skills.validators import validate_plan as validate_plan_gate
from mcp_skills.flow_parser import merge_flow_override, parse_flow_markdown
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.errors import AssemblyBudgetExceededError
from mcp_skills.models import FlowSession, FlowStage, FlowStageMeta
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer
from mcp_skills.token_guard import TokenGuard


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

_SKILL_MD = """---
name: stage-helper
title: Stage Helper
description: Helper guidance for flow stage
category: general
available_slots: [integration]
---
# Stage Helper

Skeleton guidance with stage-skeleton-marker.

<!-- SLOT: integration -->
"""


def _make_gate_session(root: Path) -> FlowSession:
    return FlowSession(
        id="f2-session",
        project_root=str(root),
        flow_name="f2-flow",
        current_stage_id="s1",
        created_at=1000.0,
        updated_at=1000.0,
    )


def _make_proj(tmp_path: Path, name: str = "f1") -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "pyproject.toml").write_text(f"[project]\nname='{name}'\n", encoding="utf-8")
    return proj


def _write_flow(proj: Path, name: str, content: str) -> None:
    flows = proj / ".agents" / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    (flows / f"{name}.md").write_text(content, encoding="utf-8")


def _make_project_with_skill(
    tmp_path: Path,
    meta_skills: tuple[str, ...] = ("stage-helper",),
    block_body: str | None = None,
) -> tuple[FlowEngine, FlowStorage, Path]:
    """Проект + скилл stage-helper (каркас + блок integration/default) + флоу F1."""
    proj = _make_proj(tmp_path)

    skills_dir = tmp_path / "skills"
    helper = skills_dir / "stage-helper"
    (helper / "blocks" / "integration").mkdir(parents=True, exist_ok=True)
    (helper / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    block = block_body or "Integration default guidance with block-default-marker."
    (helper / "blocks" / "integration" / "default.md").write_text(block, encoding="utf-8")

    fts = SkillsStorage(tmp_path / "skills_fts.db")
    SkillsSynchronizer(fts, [skills_dir]).sync()

    skills_json = ", ".join(f'"{s}"' for s in meta_skills)
    flow_md = f"""# F1 Engine Flow

## 1. Research [id: s_research]
<!-- meta: {{"skills": [{skills_json}], "required_tools": ["search_skills", "load_skill"]}} -->
Собери контекст задачи.

## 2. Done [id: s_done]
Финальный этап.
"""
    _write_flow(proj, "f1-engine", flow_md)

    store = FlowStorage(db_path=proj / "flows.db")
    engine = FlowEngine(project_root=proj, storage=store, skills_storage=fts)
    return engine, store, proj


# ---------------------------------------------------------------------------
# F1: парсинг FlowStageMeta.skills / required_tools / FlowDefinition.next_flows
# ---------------------------------------------------------------------------


def test_f1_parse_stage_meta_skills_and_required_tools() -> None:
    """meta-блок этапа с skills/required_tools парсится (имена, не контент)."""
    md = """# F1 Flow

## 1. Stage [id: s1]
<!-- meta: {"skills": ["writing-plans", "add-tests"], "required_tools": ["search_skills", "load_skill"]} -->
Instruction
"""
    flow = parse_flow_markdown(md, "f1-flow")
    meta = flow.stages["s1"].meta
    assert meta.skills == ["writing-plans", "add-tests"]
    assert meta.required_tools == ["search_skills", "load_skill"]
    assert "<!-- meta:" not in flow.stages["s1"].instruction

    # Скаляр коэрцитится в список (как aliases)
    md_scalar = """# F1 Scalar

## 1. Stage [id: s1]
<!-- meta: {"skills": "writing-plans, add-tests"} -->
Instruction
"""
    flow_scalar = parse_flow_markdown(md_scalar, "f1-scalar")
    assert flow_scalar.stages["s1"].meta.skills == ["writing-plans", "add-tests"]

    # Дефолты — пустые списки, метакомментарий вырезается из инструкции
    flow_plain = parse_flow_markdown("# P\n\n## 1. S [id: s1]\nInstruction\n", "f1-plain-meta")
    assert flow_plain.stages["s1"].meta.skills == []
    assert flow_plain.stages["s1"].meta.required_tools == []


def test_f1_rejects_unsafe_names_in_stage_lists() -> None:
    """Имена скиллов/тулов с разделителями путей отвергаются при парсинге."""
    md = """# F1 Bad

## 1. Stage [id: s1]
<!-- meta: {"skills": ["../outside"]} -->
Instruction
"""
    with pytest.raises(InvalidFlowFormatError, match="Недопустимое имя"):
        parse_flow_markdown(md, "f1-bad")

    md_tools = """# F1 Bad Tools

## 1. Stage [id: s1]
<!-- meta: {"required_tools": ["tools/search"]} -->
Instruction
"""
    with pytest.raises(InvalidFlowFormatError, match="Недопустимое имя"):
        parse_flow_markdown(md_tools, "f1-bad-tools")


def test_f1_parse_flow_next_flows() -> None:
    """next_flows читается из frontmatter (список/скаляр), дефолт — []."""
    md = """---
title: F1 Next
next_flows:
  - plan-drafting-flow
  - feature-delivery
---
# F1 Next

## 1. Stage [id: s1]
Instruction
"""
    flow = parse_flow_markdown(md, "f1-next")
    assert flow.next_flows == ["plan-drafting-flow", "feature-delivery"]

    md_scalar = """---
next_flows: brainstorming-flow
---
# F1 Next Scalar

## 1. Stage [id: s1]
Instruction
"""
    flow_scalar = parse_flow_markdown(md_scalar, "f1-next-scalar")
    assert flow_scalar.next_flows == ["brainstorming-flow"]

    flow_plain = parse_flow_markdown("# P\n\n## 1. S [id: s1]\nInstruction\n", "f1-plain")
    assert flow_plain.next_flows == []


def test_f1_merge_override_next_flows() -> None:
    """next_flows мержится при секционном оверрайде (дедупликация, порядок)."""
    base = parse_flow_markdown(
        "---\nnext_flows:\n  - flow-a\n---\n# B\n\n## 1. S [id: s1]\nI\n", "b"
    )
    ov = parse_flow_markdown(
        "---\nnext_flows:\n  - flow-b\n  - flow-a\n---\n# O\n\n## 1. S [id: s1]\nI\n", "o"
    )
    merged = merge_flow_override(base, ov)
    assert merged.next_flows == ["flow-a", "flow-b"]

    # Оверрайд без next_flows сохраняет значения базового флоу
    ov_empty = parse_flow_markdown("# E\n\n## 1. S [id: s1]\nI\n", "e")
    merged_empty = merge_flow_override(base, ov_empty)
    assert merged_empty.next_flows == ["flow-a"]


# ---------------------------------------------------------------------------
# F1: компиляция skills в инструкцию этапа + экспозиция required_tools
# ---------------------------------------------------------------------------


def test_f1_stage_skills_compiled_into_instruction(tmp_path: Path) -> None:
    """Инструкция этапа дополняется собранным скиллом (каркас + блок слота)."""
    engine, _store, _proj = _make_project_with_skill(tmp_path)

    res = engine.start_flow("f1-engine")
    stage = res["stage"]
    assert stage["id"] == "s_research"

    # Инструкция дополнена: исходный текст + каркас скилла + блок слота
    assert "Собери контекст задачи." in stage["instruction"]
    assert "stage-skeleton-marker" in stage["instruction"]
    assert "block-default-marker" in stage["instruction"]
    assert stage["skills_compiled"] == ["stage-helper"]
    assert stage["skills_truncated"] is False

    # required_tools экспонируется: в meta и на верхнем уровне payload
    assert stage["meta"]["required_tools"] == ["search_skills", "load_skill"]
    assert stage["meta"]["skills"] == ["stage-helper"]
    assert res["required_tools"] == ["search_skills", "load_skill"]

    # Этап без skills: инструкция не меняется, служебных ключей нет
    res2 = engine.advance_flow(session_id=res["session_id"])
    assert res2["stage"]["id"] == "s_done"
    assert res2["stage"]["instruction"] == "Финальный этап."
    assert "skills_compiled" not in res2["stage"]
    assert "required_tools" not in res2


def test_f1_stage_skills_token_guard_budget(tmp_path: Path) -> None:
    """Объединённая инструкция укладывается в бюджет TokenGuard, флаг усечения."""
    huge_block = "Единица длинного руководства для проверки бюджета. " * 4000
    engine, _store, _proj = _make_project_with_skill(tmp_path, block_body=huge_block)

    with pytest.raises(AssemblyBudgetExceededError):
        engine.start_flow("f1-engine")
    assert _store.find_active_sessions(str(_proj)) == []


def test_f1_missing_skill_warns_not_fails(tmp_path: Path) -> None:
    """Отсутствующий скилл — предупреждение в инструкции, этап не падает."""
    engine, _store, _proj = _make_project_with_skill(tmp_path, meta_skills=("no-such-skill-xyz",))

    res = engine.start_flow("f1-engine")
    assert res["status"] == "active"
    assert res["stage"]["id"] == "s_research"
    assert "не найден в реестре" in res["stage"]["instruction"]
    assert res["stage"]["skills_compiled"] == []


# ---------------------------------------------------------------------------
# F1: next_flows в completed-ответе
# ---------------------------------------------------------------------------


def test_f1_next_flows_in_completed_response(tmp_path: Path) -> None:
    """Completed-ответ содержит next_flows завершённого флоу и подсказку."""
    proj = _make_proj(tmp_path, name="f1n")
    _write_flow(
        proj,
        "f1-chain",
        """---
title: F1 Chain
next_flows:
  - plan-drafting-flow
---
# F1 Chain

## 1. Only [id: s_only]
Instruction
""",
    )
    _write_flow(proj, "f1-plain", "# P\n\n## 1. Only [id: s_only]\nInstruction\n")

    store = FlowStorage(db_path=proj / "flows.db")
    engine = FlowEngine(project_root=proj, storage=store)

    res = engine.start_flow("f1-chain")
    done = engine.advance_flow(session_id=res["session_id"])
    assert done["status"] == "completed"
    assert done["next_flows"] == ["plan-drafting-flow"]
    assert "plan-drafting-flow" in done["next_flow_hint"]

    # Тот же путь через фасад step_flow; флоу без next_flows — ключа нет
    res2 = engine.start_flow("f1-plain")
    done2 = step_flow(
        action="advance", session_id=res2["session_id"], storage=store, project_root=proj
    )
    assert done2["status"] == "completed"
    assert "next_flows" not in done2


# ---------------------------------------------------------------------------
# F2: validate_plan_gate — однозначные типы артефакта
# ---------------------------------------------------------------------------


def test_f2_plan_string_path_with_file_passes(tmp_path: Path) -> None:
    """Строка-путь + существующий файл → passed (читается файл)."""
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "plan.md").write_text("# План\nШаг 1", encoding="utf-8")
    stage = FlowStage(id="s1", title="Draft", instruction="Plan")

    res = validate_plan_gate(
        _make_gate_session(tmp_path),
        stage,
        deliverable="docs/plans/plan.md",
        project_root=tmp_path,
    )
    assert res.passed is True
    assert res.blockers == []


def test_f2_plan_string_path_missing_fails_clearly(tmp_path: Path) -> None:
    """Строка-путь без файла → failed с явной причиной (не «текст плана»)."""
    stage = FlowStage(id="s1", title="Draft", instruction="Plan")

    res = validate_plan_gate(
        _make_gate_session(tmp_path),
        stage,
        deliverable="docs/plans/missing.md",
        project_root=tmp_path,
    )
    assert res.passed is False
    assert any("docs/plans/missing.md" in b and "не найден" in b for b in res.blockers)


def test_f2_plan_absolute_and_outside_paths(tmp_path: Path) -> None:
    """Абсолютные и внешние пути валидируются корректно (aud-skills-9e3f)."""
    stage = FlowStage(id="s1", title="Draft", instruction="Plan")
    session = _make_gate_session(tmp_path)

    # 1. Несуществующий абсолютный путь -> passed=False с блокером
    res_nonexistent = validate_plan_gate(
        session,
        stage,
        deliverable="/nonexistent/plan.md",
        project_root=tmp_path,
    )
    assert res_nonexistent.passed is False
    assert any("/nonexistent/plan.md" in b for b in res_nonexistent.blockers)

    # 2. Путь вне project_root (/etc/passwd) -> passed=False с блокером
    res_outside = validate_plan_gate(
        session,
        stage,
        deliverable="/etc/passwd",
        project_root=tmp_path,
    )
    assert res_outside.passed is False
    assert any("/etc/passwd" in b for b in res_outside.blockers)

    # 3. Валидный абсолютный путь внутри project_root -> passed=True
    valid_file = tmp_path / "valid_plan.md"
    valid_file.write_text("# Реальный план\nШаг 1", encoding="utf-8")
    res_valid = validate_plan_gate(
        session,
        stage,
        deliverable=str(valid_file),
        project_root=tmp_path,
    )
    assert res_valid.passed is True
    assert res_valid.blockers == []


def test_f2_plan_text_still_works(tmp_path: Path) -> None:
    """Текст плана (многострочный и однострочный с пробелами) — как раньше."""
    stage = FlowStage(id="s1", title="Draft", instruction="Plan")

    plan_text = "# План реализации\n\n## Шаг 1\nСделать X.\n## Шаг 2\nПроверить Y."
    res = validate_plan_gate(
        _make_gate_session(tmp_path), stage, deliverable=plan_text, project_root=tmp_path
    )
    assert res.passed is True

    res_short = validate_plan_gate(
        _make_gate_session(tmp_path),
        stage,
        deliverable="Краткий однострочный план без маркеров пути",
        project_root=tmp_path,
    )
    assert res_short.passed is True


def test_f2_target_file_does_not_silence_deliverable_file_path(tmp_path: Path) -> None:
    """Несуществующий target_file + корректный file_path → passed (T4)."""
    (tmp_path / "real_plan.md").write_text("# Реальный план\nКонтент", encoding="utf-8")
    stage = FlowStage(
        id="s1",
        title="Draft",
        instruction="Plan",
        meta=FlowStageMeta(target_file="missing_target.md"),
    )

    res = validate_plan_gate(
        _make_gate_session(tmp_path),
        stage,
        deliverable={"file_path": "real_plan.md"},
        project_root=tmp_path,
    )
    assert res.passed is True
    assert res.blockers == []


def test_f2_target_and_file_path_both_missing_fail(tmp_path: Path) -> None:
    """Оба файловых источника отсутствуют → оба блокера видны."""
    stage = FlowStage(
        id="s1",
        title="Draft",
        instruction="Plan",
        meta=FlowStageMeta(target_file="missing_target.md"),
    )

    res = validate_plan_gate(
        _make_gate_session(tmp_path),
        stage,
        deliverable={"file_path": "also_missing.md"},
        project_root=tmp_path,
    )
    assert res.passed is False
    assert any("missing_target.md" in b for b in res.blockers)
    assert any("also_missing.md" in b for b in res.blockers)


def test_f2_deliverable_content_is_text(tmp_path: Path) -> None:
    """deliverable={'content': ...} — текст плана (приоритетнее file_path)."""
    stage = FlowStage(id="s1", title="Draft", instruction="Plan")

    res = validate_plan_gate(
        _make_gate_session(tmp_path),
        stage,
        deliverable={"content": "# План из content"},
        project_root=tmp_path,
    )
    assert res.passed is True

    # content-строка исключает file_path: отсутствующий файл не блокирует
    res_priority = validate_plan_gate(
        _make_gate_session(tmp_path),
        stage,
        deliverable={"content": "# План из content", "file_path": "missing.md"},
        project_root=tmp_path,
    )
    assert res_priority.passed is True


def test_f2_target_file_valid_still_wins(tmp_path: Path) -> None:
    """Существующий target_file по-прежнему приоритетен (текст игнорируется)."""
    (tmp_path / "target.md").write_text("# Целевой план", encoding="utf-8")
    stage = FlowStage(
        id="s1",
        title="Draft",
        instruction="Plan",
        meta=FlowStageMeta(target_file="target.md"),
    )

    res = validate_plan_gate(
        _make_gate_session(tmp_path),
        stage,
        deliverable="какой-то текст плана",
        project_root=tmp_path,
    )
    assert res.passed is True


# ---------------------------------------------------------------------------
# F2: _check_deliverable_file (deliverable_must_exist) — согласованность
# ---------------------------------------------------------------------------


def test_f2_deliverable_must_exist_file_path_rescues_missing_target(tmp_path: Path) -> None:
    """deliverable_must_exist: file_path спасает при ненайденном target_file."""
    proj = _make_proj(tmp_path, name="f2r")
    _write_flow(
        proj,
        "f2-rescue",
        """# F2 Rescue

## 1. Artifact [id: s_artifact]
<!-- meta: {"deliverable_must_exist": true, "target_file": "gone.txt"} -->
Создай артефакт.

## 2. Done [id: s_done]
Финал.
""",
    )
    (proj / "artifact.md").write_text("Real artifact content", encoding="utf-8")

    engine = FlowEngine(project_root=proj)
    res = engine.start_flow("f2-rescue")
    r = engine.advance_flow(session_id=res["session_id"], deliverable={"file_path": "artifact.md"})
    assert r["status"] != "gate_blocked"
    assert r["stage"]["id"] == "s_done"


def test_f2_deliverable_must_exist_path_string(tmp_path: Path) -> None:
    """Строка-путь в deliverable_must_exist: файл есть → путь; нет/текст → блок."""
    proj = _make_proj(tmp_path, name="f2p")
    _write_flow(
        proj,
        "f2-path",
        """# F2 Path

## 1. Artifact [id: s_artifact]
<!-- meta: {"deliverable_must_exist": true} -->
Создай артефакт.

## 2. Done [id: s_done]
Финал.
""",
    )
    (proj / "docs").mkdir()
    (proj / "docs" / "artifact.md").write_text("Path artifact", encoding="utf-8")

    engine = FlowEngine(project_root=proj)

    res = engine.start_flow("f2-path")
    r_ok = engine.advance_flow(session_id=res["session_id"], deliverable="docs/artifact.md")
    assert r_ok["stage"]["id"] == "s_done"

    res2 = engine.start_flow("f2-path")
    r_miss = engine.advance_flow(session_id=res2["session_id"], deliverable="docs/nope.md")
    assert r_miss["status"] == "gate_blocked"
    assert any("docs/nope.md" in b and "не найден" in b for b in r_miss["blockers"])

    res3 = engine.start_flow("f2-path")
    r_text = engine.advance_flow(
        session_id=res3["session_id"], deliverable="текстовый отчёт, не путь"
    )
    assert r_text["status"] == "gate_blocked"
    assert any("не является существующим файлом" in b for b in r_text["blockers"])
