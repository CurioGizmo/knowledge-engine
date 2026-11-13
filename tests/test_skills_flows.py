"""Комплексные тесты для интерактивной стейт-машины сценариев (Flow State Machine)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from mcp_core.paths import get_flows_dir, safe_read_text
from mcp_skills.error_envelope import SKILLS_ERROR_RULES, _handle_tool_error
from mcp_skills.errors import (
    FlowError,
    FlowNotFoundError,
    GateValidationError,
    InvalidFlowFormatError,
    SessionConflictError,
    SkillsError,
)
from mcp_skills.flow_engine import (
    GATE_REGISTRY,
    FlowEngine,
    abort_flow,
    advance_flow,
    get_flow_status,
    list_flows,
    resolve_flow_escalation,
    start_flow,
)
from mcp_skills.flow_parser import (
    list_available_flows,
    load_flow_definition,
    merge_flow_override,
    parse_flow_markdown,
)
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.installer import SKILLS_TOOLS_LIST, get_skills_tool_schemas
from mcp_skills.models import FlowSession, FlowStatus, FlowVerdict


# ==========================================
# 1. PARSER & FENCES (M1, N7, N17)
# ==========================================


def test_parse_flow_markdown_basic_and_fences() -> None:
    """Проверяет корректность парсинга этапов и игнорирование ## внутри кодовых блоков."""
    md_content = """# Test Flow
Сценарий для тестирования

## 1. Шаг анализа [id: step_one]
<!-- meta: {"loop_max": 2} -->
Инструкция к первому шагу.

```python
## Это заголовок внутри блока кода, он не должен стать этапом
def foo():
    pass
```

## 2. Шаг проверки [id: step_two]
<!-- meta: {"agent": "reviewer", "loop_max": 3} -->
Инструкция ко второму шагу.
"""
    flow = parse_flow_markdown(md_content, "test-flow")
    assert flow.name == "test-flow"
    assert flow.title == "Test Flow"
    assert flow.description == "Сценарий для тестирования"
    assert flow.stage_order == ["step_one", "step_two"]

    stage1 = flow.stages["step_one"]
    assert stage1.id == "step_one"
    assert stage1.title == "1. Шаг анализа"
    assert stage1.meta.loop_max == 2
    assert "Инструкция к первому шагу." in stage1.instruction
    assert "## Это заголовок внутри блока кода" in stage1.instruction
    assert "<!-- meta:" not in stage1.instruction

    stage2 = flow.stages["step_two"]
    assert stage2.id == "step_two"
    assert stage2.meta.agent == "reviewer"
    assert stage2.meta.loop_max == 3


def test_parse_canonical_feature_delivery() -> None:
    """Проверяет self-parse канонического feature-delivery.md (6 этапов, сбалансированные fences)."""
    flow = load_flow_definition("feature-delivery")
    assert flow.name == "feature-delivery"
    assert len(flow.stage_order) == 6
    assert flow.stage_order == [
        "spec_analysis",
        "plan_draft",
        "plan_review_loop",
        "dag_split",
        "parallel_coding",
        "qa_verification",
    ]

    assert flow.stages["spec_analysis"].meta.agent is None
    assert flow.stages["spec_analysis"].meta.require_human_confirm is True
    assert flow.stages["plan_draft"].meta.gate == "validate_plan"
    assert flow.stages["plan_draft"].meta.target_file == "implementation_plan.md"
    assert flow.stages["plan_draft"].meta.deliverable_must_exist is True
    assert flow.stages["plan_review_loop"].meta.agent == "plan-reviewer"
    assert flow.stages["plan_review_loop"].meta.loop_max == 3
    assert flow.stages["dag_split"].meta.require_verdict is True
    assert flow.stages["parallel_coding"].meta.dispatch_agents is True
    assert "реализация фичи" in flow.aliases
    assert "разработка" in flow.aliases
    assert flow.intent_group == "feature-delivery"
    assert "реализация новой функциональности или фичи" in flow.when_to_use


def test_parse_flow_markdown_frontmatter_aliases() -> None:
    """Проверяет парсинг YAML frontmatter с алиасами (список и скалярное значение)."""
    # 1. Frontmatter со списком алиасов
    md_list = """---
title: Custom Plan Runner
aliases:
  - "выполни план"
  - "plan execution"
---
# Custom Plan Runner
Описание процесса.
## 1. Step [id: s1]
Instruction
"""
    flow1 = parse_flow_markdown(md_list, "plan-runner")
    assert flow1.name == "plan-runner"
    assert flow1.title == "Custom Plan Runner"
    assert flow1.aliases == ["выполни план", "plan execution"]

    # 2. Frontmatter со скалярной строкой алиасов (коэрцит в список)
    md_scalar = """---
aliases: выполни задачу
---
# Scalar Alias Flow
## 1. Step [id: s1]
Instruction
"""
    flow2 = parse_flow_markdown(md_scalar, "scalar-flow")
    assert flow2.aliases == ["выполни задачу"]

    # 3. Frontmatter с пустыми значениями (YAML None не превращается в строку "None")
    md_empty_yaml = """---
title:
description:
aliases:
---
# Body Title
Body description here.
## 1. Step [id: s1]
Instruction
"""
    flow3 = parse_flow_markdown(md_empty_yaml, "empty-yaml-flow")
    assert flow3.title == "Body Title"
    assert flow3.description == "Body description here."
    assert flow3.aliases == []


def test_parse_fail_closed_on_errors() -> None:
    """Проверяет fail-closed поведение при некорректном формате Markdown или метаданных."""
    # 1. Нет этапов
    with pytest.raises(InvalidFlowFormatError, match="не содержит ни одного этапа"):
        parse_flow_markdown("# Empty Flow\nБез секций h2", "empty")

    # 2. Несбалансированные кодовые блоки
    with pytest.raises(InvalidFlowFormatError, match="Несбалансированные кодовые блоки"):
        parse_flow_markdown(
            "# Bad Fences\n## 1. Step [id: s1]\n```python\ndef test():\npass\n",
            "bad-fence",
        )

    # 3. Пустая инструкция
    with pytest.raises(InvalidFlowFormatError, match="не может быть пустой"):
        parse_flow_markdown("# Bad Flow\n## 1. Step [id: s1]\n   \n", "empty-inst")

    # 4. Дубликаты stage ID
    with pytest.raises(InvalidFlowFormatError, match="Дубликат ID этапа"):
        parse_flow_markdown(
            "# Dup Flow\n## 1. Step [id: s1]\nInst 1\n## 2. Step [id: s1]\nInst 2",
            "dup",
        )

    # 5. extra="forbid" на неизвестные ключи в meta
    bad_meta = """# Bad Meta
## 1. Step [id: s1]
<!-- meta: {"unknown_field": 123} -->
Inst
"""
    with pytest.raises(InvalidFlowFormatError, match="Некорректные метаданные"):
        parse_flow_markdown(bad_meta, "bad-meta")

    # 6. Невалидный loop_max <= 0
    bad_loop = """# Bad Loop
## 1. Step [id: s1]
<!-- meta: {"loop_max": 0} -->
Inst
"""
    with pytest.raises(InvalidFlowFormatError, match="Некорректные метаданные"):
        parse_flow_markdown(bad_loop, "bad-loop")


# ==========================================
# 2. MERGER & OVERRIDES
# ==========================================


def test_merge_flow_override_sectional() -> None:
    """Проверяет посекционный оверрайд этапа с сохранением остальных."""
    base_md = """---
aliases:
  - "базовый алиас"
---
# Base Flow
## 1. Base One [id: step1]
Base instruction 1
## 2. Base Two [id: step2]
Base instruction 2
"""
    override_md = """---
aliases:
  - "оверрайд алиас"
---
## Override Two [id: step2]
<!-- meta: {"loop_max": 5} -->
Overridden instruction 2
"""
    base = parse_flow_markdown(base_md, "flow-x")
    override = parse_flow_markdown(override_md, "flow-x")
    merged = merge_flow_override(base, override)

    assert merged.stage_order == ["step1", "step2"]
    assert merged.stages["step1"].instruction == "Base instruction 1"
    assert merged.stages["step2"].instruction == "Overridden instruction 2"
    assert merged.stages["step2"].meta.loop_max == 5
    assert set(merged.aliases) == {"базовый алиас", "оверрайд алиас"}


def test_override_project_over_cache(tmp_path: Path) -> None:
    """Проверяет приоритет локального оверрайда проекта над шаблоном."""
    flows_dir = get_flows_dir(tmp_path)
    flows_dir.mkdir(parents=True, exist_ok=True)
    custom_override = flows_dir / "feature-delivery.override.md"
    custom_override.write_text(
        """# Custom Feature Delivery
## 1. Custom Spec [id: spec_analysis]
Custom spec instructions.
""",
        encoding="utf-8",
    )

    flow = load_flow_definition("feature-delivery", project_root=tmp_path)
    # Этап 1 заменён через оверрайд, остальные 5 этапов унаследованы из базового шаблона
    assert flow.stages["spec_analysis"].instruction == "Custom spec instructions."
    assert len(flow.stage_order) == 6


# ==========================================
# 3. STORAGE & CONCURRENCY (C3, N5, N6, N15)
# ==========================================


def test_flow_storage_optimistic_locking(tmp_path: Path) -> None:
    """Проверяет работу оптимистической блокировки версий (SessionConflictError)."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)

    session = FlowSession(
        id="flow-concurrency",
        project_root=str(tmp_path),
        flow_name="feature-delivery",
        current_stage_id="spec_analysis",
        iteration_count=1,
        status=FlowStatus.ACTIVE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.save_session(session)

    # Первая параллельная транзакция обновляет запись (версия 1 -> 2)
    s1 = store.get_session("flow-concurrency")
    assert s1 is not None
    s1.iteration_count = 2
    store.update_session(s1)
    assert s1.version == 2

    # Вторая транзакция пытается обновить со старой версией 1 -> конфликт!
    s2 = FlowSession(
        id="flow-concurrency",
        project_root=str(tmp_path),
        flow_name="feature-delivery",
        current_stage_id="spec_analysis",
        iteration_count=3,
        status=FlowStatus.ACTIVE,
        version=1,  # Устаревшая версия!
        created_at=1000.0,
        updated_at=1000.0,
    )
    with pytest.raises(SessionConflictError, match="Конфликт параллельного изменения"):
        store.update_session(s2)


def test_flow_storage_null_safe_git_branch(tmp_path: Path) -> None:
    """Проверяет NULL-safe поиск активных сессий (IS ?) при отсутствии git_branch (N5)."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)

    session = FlowSession(
        id="flow-no-git",
        project_root=str(tmp_path),
        git_branch=None,  # Нет git ветки
        flow_name="feature-delivery",
        current_stage_id="spec_analysis",
        iteration_count=1,
        status=FlowStatus.ACTIVE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.save_session(session)

    # Поиск с git_branch=None должен корректно сработать через IS ?
    found = store.find_active_sessions(str(tmp_path), git_branch=None)
    assert len(found) == 1
    assert found[0].id == "flow-no-git"


def test_find_active_sessions_branch_isolation(tmp_path: Path) -> None:
    """Проверяет строгую изоляцию сессий по веткам git: нет фоллбэка на чужие ветки (H1)."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)

    session_main = FlowSession(
        id="flow-on-main",
        project_root=str(tmp_path),
        git_branch="main",
        flow_name="feature-delivery",
        current_stage_id="spec_analysis",
        iteration_count=1,
        status=FlowStatus.ACTIVE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.save_session(session_main)

    # 1. Поиск по main находит сессию
    assert len(store.find_active_sessions(str(tmp_path), git_branch="main")) == 1

    # 2. Поиск по feature-x НЕ должен возвращать сессию из main (строгая изоляция ветки)
    assert store.find_active_sessions(str(tmp_path), git_branch="feature-x") == []

    # 3. Поиск вне git (git_branch=None) при отсутствии сессий без ветки находит сессию через fallback
    assert len(store.find_active_sessions(str(tmp_path), git_branch=None)) == 1


# ==========================================
# 4. STATE MACHINE & CIRCUIT BREAKER (C2, N3, N4, N19)
# ==========================================


def test_state_machine_full_lifecycle(tmp_path: Path) -> None:
    """Проверяет полный цикл флоу: автопереход, валидация гейта, ревью, завершение."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)
    engine = FlowEngine(project_root=tmp_path, storage=store)

    # 1. start_flow
    res1 = engine.start_flow(name="feature-delivery")
    session_id = res1["session_id"]
    assert res1["status"] == "active"
    assert res1["stage"]["id"] == "spec_analysis"

    # 2. advance_flow на этапе spec_analysis (без агента) -> автопереход на plan_draft
    # Этап spec_analysis требует подтверждения (require_human_confirm=True)
    res2 = engine.advance_flow(session_id=session_id, human_confirmed=True)
    assert res2["stage"]["id"] == "plan_draft"

    # 3. advance_flow на plan_draft без созданного implementation_plan.md -> шлюз блокирует
    res3 = engine.advance_flow(session_id=session_id)
    assert res3["status"] == "gate_blocked"
    assert "не найден в проекте" in res3["blockers"][0]

    # Создаём валидный план
    plan_file = tmp_path / "implementation_plan.md"
    plan_file.write_text("# Feature Plan\n\n## План\nДетали реализации\n", encoding="utf-8")

    # Теперь advance_flow на plan_draft проходит (гейт пройден, без агента -> автопереход на plan_review_loop)
    res4 = engine.advance_flow(session_id=session_id)
    assert res4["stage"]["id"] == "plan_review_loop"
    assert res4["stage"]["meta"]["agent"] == "plan-reviewer"

    # 4. Ревью loop: отправка verdict="needs_work"
    res5 = engine.advance_flow(
        session_id=session_id, verdict="needs_work", notes="Доработать детали"
    )
    assert res5["status"] == "active"
    assert res5["iteration_count"] == 2
    assert "Требуется доработка" in res5["message"]

    # 5. Ревью loop: одобрение verdict="approved" -> переход на dag_split
    res6 = engine.advance_flow(session_id=session_id, verdict="approved", notes="Отлично")
    assert res6["stage"]["id"] == "dag_split"


def test_circuit_breaker_tripped_and_escalation_resolution(tmp_path: Path) -> None:
    """Проверяет срабатывание Circuit Breaker при превышении loop_max и его снятие."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)
    engine = FlowEngine(project_root=tmp_path, storage=store)

    res_start = engine.start_flow(name="feature-delivery")
    session_id = res_start["session_id"]

    # Быстро переводим сессию на этап plan_review_loop
    plan_file = tmp_path / "implementation_plan.md"
    plan_file.write_text("# Plan\n## План\nКонтент", encoding="utf-8")
    engine.advance_flow(session_id=session_id, human_confirmed=True)  # spec -> plan_draft
    engine.advance_flow(session_id=session_id)  # plan_draft -> plan_review_loop (loop_max=3)

    # Итерация 1 -> 2 (needs_work)
    r1 = engine.advance_flow(session_id=session_id, verdict="needs_work")
    assert r1["iteration_count"] == 2
    assert r1["status"] == "active"

    # Итерация 2 -> 3 (needs_work, ровно loop_max — допустимо)
    r2 = engine.advance_flow(session_id=session_id, verdict="needs_work")
    assert r2["iteration_count"] == 3
    assert r2["status"] == "active"

    # Итерация 3 -> 4 > loop_max -> срабатывает Circuit Breaker!
    r3 = engine.advance_flow(session_id=session_id, verdict="needs_work")
    assert r3["status"] == "blocked_escalate"
    assert "Достигнут лимит итераций ревью" in r3["directive"]

    # Попытка advance_flow на заблокированной сессии отвергается
    with pytest.raises(SessionConflictError, match="заблокирована Circuit Breaker"):
        engine.advance_flow(session_id=session_id, verdict="approved")

    # Снятие блокировки через retry
    r_retry = engine.resolve_flow_escalation(session_id=session_id, resolution="retry")
    assert r_retry["status"] == "active"
    assert r_retry["stage"]["id"] == "plan_review_loop"

    # Сессия снова активна, одобряем через override_approved
    # Симулируем повторное срабатывание
    engine.advance_flow(session_id=session_id, verdict="needs_work")
    engine.advance_flow(session_id=session_id, verdict="needs_work")
    engine.advance_flow(session_id=session_id, verdict="needs_work")
    sess = store.get_session(session_id)
    assert sess is not None and sess.status == FlowStatus.BLOCKED_ESCALATE

    # Принудительный пропуск через override_approved -> переход на dag_split
    r_override = engine.resolve_flow_escalation(
        session_id=session_id, resolution="override_approved"
    )
    assert r_override["stage"]["id"] == "dag_split"


def test_abort_flow(tmp_path: Path) -> None:
    """Проверяет принудительную отмену флоу через abort_flow."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)
    engine = FlowEngine(project_root=tmp_path, storage=store)

    res = engine.start_flow(name="feature-delivery")
    session_id = res["session_id"]

    res_abort = engine.abort_flow(session_id=session_id, reason="Задача отменена")
    assert res_abort["status"] == "aborted"

    sess = store.get_session(session_id)
    assert sess is not None and sess.status == FlowStatus.ABORTED

    # Повторный аборт или продвижение отвергаются
    with pytest.raises(FlowError, match="в терминальном статусе"):
        engine.abort_flow(session_id=session_id)
    with pytest.raises(FlowError, match="в терминальном статусе"):
        engine.advance_flow(session_id=session_id)


# ==========================================
# 5. SECURITY & GATES (C7, N1)
# ==========================================


def test_safe_read_text_path_traversal(tmp_path: Path) -> None:
    """Проверяет защиту от path traversal в safe_read_text."""
    proj = tmp_path / "project"
    proj.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")

    # Попытка выхода вверх блокируется (возвращается None)
    assert safe_read_text(proj, "../secret.txt") is None

    # Абсолютный путь вне проекта блокируется (возвращается None)
    assert safe_read_text(proj, str(secret)) is None

    # Корректный файл внутри проекта читается успешно
    valid_file = proj / "file.txt"
    valid_file.write_text("ok", encoding="utf-8")
    assert safe_read_text(proj, "file.txt") == "ok"


def test_deliverable_size_limit(tmp_path: Path) -> None:
    """Проверяет отказ при превышении лимита deliverable 64КБ."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)
    engine = FlowEngine(project_root=tmp_path, storage=store)

    res = engine.start_flow(name="feature-delivery")
    session_id = res["session_id"]

    huge_payload = "a" * (65 * 1024)
    with pytest.raises(ValueError, match="Превышен лимит deliverable 64КБ"):
        engine.advance_flow(session_id=session_id, deliverable=huge_payload)


# ==========================================
# 6. ERROR ENVELOPE RULES ORDER (N11, N17)
# ==========================================


def test_skills_error_rules_order() -> None:
    """Проверяет, что новые ошибки флоу маппятся в envelope ДО базового SkillsError."""
    rule_types = [r.exc for r in SKILLS_ERROR_RULES]

    idx_flow_not_found = rule_types.index(FlowNotFoundError)
    idx_flow_format = rule_types.index(InvalidFlowFormatError)
    idx_conflict = rule_types.index(SessionConflictError)
    idx_gate = rule_types.index(GateValidationError)
    idx_base = rule_types.index(SkillsError)

    # Все специфичные правила обязаны предшествовать общему SkillsError
    assert idx_flow_not_found < idx_base
    assert idx_flow_format < idx_base
    assert idx_conflict < idx_base
    assert idx_gate < idx_base

    # Проверка вызова через envelope
    resp = _handle_tool_error(FlowNotFoundError("Сценарий не найден"), "test_tool")
    assert resp["error_type"] == "FlowNotFoundError"
    assert resp["status"] == "rejected"


def test_validate_plan_gate_safety_and_verification(tmp_path: Path) -> None:
    from mcp_skills.flow_engine import validate_plan_gate
    from mcp_skills.models import FlowSession, FlowStage, FlowStageMeta

    session = FlowSession(
        id="test-session",
        project_root=str(tmp_path),
        flow_name="test-flow",
        current_stage_id="s1",
        created_at=1000.0,
        updated_at=1000.0,
    )

    # 1. Target file traversal attempt (fails safe: blocked as missing file)
    stage_traversal = FlowStage(
        id="s1",
        title="Draft",
        instruction="Plan",
        meta=FlowStageMeta(target_file="../outside.md"),
    )
    res_traversal = validate_plan_gate(
        session, stage_traversal, deliverable=None, project_root=tmp_path
    )
    assert res_traversal.passed is False
    assert any("не найден" in b for b in res_traversal.blockers)

    # 2. Target file missing
    stage_missing = FlowStage(
        id="s2",
        title="Draft",
        instruction="Plan",
        meta=FlowStageMeta(target_file="missing_plan.md"),
    )
    res_missing = validate_plan_gate(
        session, stage_missing, deliverable=None, project_root=tmp_path
    )
    assert res_missing.passed is False
    assert any("не найден" in b for b in res_missing.blockers)

    # 3. Empty plan content
    stage_generic = FlowStage(
        id="s3",
        title="Draft",
        instruction="Plan",
    )
    res_empty = validate_plan_gate(session, stage_generic, deliverable="   ", project_root=tmp_path)
    assert res_empty.passed is False
    assert any("пуст" in b for b in res_empty.blockers)

    # 4. Valid plan content passed
    (tmp_path / "plan.md").write_text("# Feature Plan\nDetails here.", encoding="utf-8")
    stage_valid = FlowStage(
        id="s4",
        title="Draft",
        instruction="Plan",
        meta=FlowStageMeta(target_file="plan.md"),
    )
    res_valid = validate_plan_gate(session, stage_valid, deliverable=None, project_root=tmp_path)
    assert res_valid.passed is True
    assert res_valid.blockers == []


def test_parse_flow_markdown_intent_group_and_when_to_use() -> None:
    """Проверяет парсинг intent_group и when_to_use из frontmatter с валидацией и коэрцингом."""
    # 1. Полноценный frontmatter со списком when_to_use
    md = """---
title: Plan Flow
intent_group: "plan-execution"
when_to_use:
  - "автоматическое выполнение плана"
  - "пакетная реализация"
---
# Plan Flow
## 1. Step [id: s1]
Instruction
"""
    flow = parse_flow_markdown(md, "test-flow")
    assert flow.intent_group == "plan-execution"
    assert flow.when_to_use == ["автоматическое выполнение плана", "пакетная реализация"]

    # 2. Скалярное значение when_to_use коэрцитится в список
    md_scalar = """---
intent_group: "brainstorming"
when_to_use: исследование архитектуры
---
# Brainstorm Flow
## 1. Step [id: s1]
Instruction
"""
    flow_scalar = parse_flow_markdown(md_scalar, "scalar-flow")
    assert flow_scalar.intent_group == "brainstorming"
    assert flow_scalar.when_to_use == ["исследование архитектуры"]

    # 3. Отсутствующие поля имеют дефолты None и []
    md_none = """# Simple Flow
## 1. Step [id: s1]
Instruction
"""
    flow_none = parse_flow_markdown(md_none, "none-flow")
    assert flow_none.intent_group is None
    assert flow_none.when_to_use == []


def test_merge_flow_override_intent_group_and_when_to_use() -> None:
    """Проверяет слияние intent_group и when_to_use при оверрайде флоу."""
    base_md = """---
title: Base Flow
intent_group: "base-group"
when_to_use:
  - "условие 1"
  - "условие 2"
---
# Base Flow
## 1. Step [id: s1]
Base instruction
"""
    override_md = """---
title: Override Flow
intent_group: "override-group"
when_to_use:
  - "условие 2"
  - "условие 3"
---
# Override Flow
## 1. Step [id: s1]
Override instruction
"""
    base_def = parse_flow_markdown(base_md, "flow")
    ov_def = parse_flow_markdown(override_md, "flow")
    merged = merge_flow_override(base_def, ov_def)

    assert merged.intent_group == "override-group"
    # Дедупликация с сохранением порядка: условие 1, условие 2, условие 3
    assert merged.when_to_use == ["условие 1", "условие 2", "условие 3"]

    # Если оверрайд не задает intent_group, сохраняется значение base
    ov_empty_md = """# Empty Override\n## 1. Step [id: s1]\nInstruction"""
    ov_empty_def = parse_flow_markdown(ov_empty_md, "flow")
    merged2 = merge_flow_override(base_def, ov_empty_def)
    assert merged2.intent_group == "base-group"


def test_declarative_quality_gates(tmp_path: Path) -> None:
    """Проверяет все декларативные гейты (require_verdict, require_human_confirm, deliverable_must_exist)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    flows_dir = proj / ".agents" / "flows"
    flows_dir.mkdir(parents=True)

    # Флоу с этапами, проверяющими каждое правило
    flow_content = """---
title: Gate Test Flow
intent_group: "test-gates"
---
# Gate Test Flow

## 1. Stage Verdict [id: s_verdict]
<!-- meta: {"require_verdict": true} -->
Instruction 1

## 2. Stage Confirm [id: s_confirm]
<!-- meta: {"require_human_confirm": true} -->
Instruction 2

## 3. Stage Deliverable File [id: s_file]
<!-- meta: {"deliverable_must_exist": true, "target_file": "output.txt"} -->
Instruction 3

## 4. Stage Combined [id: s_combined]
<!-- meta: {"gate": "validate_plan", "target_file": "plan.md", "require_verdict": true} -->
Instruction 4
"""
    (flows_dir / "gate-flow.md").write_text(flow_content, encoding="utf-8")
    engine = FlowEngine(project_root=proj)

    # --- 1. require_verdict ---
    sess = engine.start_flow("gate-flow", project_root=proj)
    assert sess["stage"]["id"] == "s_verdict"

    # Advance без вердикта -> блокировка
    res_blocked = engine.advance_flow(sess["session_id"], deliverable="done", project_root=proj)
    assert res_blocked["status"] == "gate_blocked"
    assert "Этап требует явный вердикт (approved/needs_work)" in res_blocked["blockers"]
    assert "declarative_checks" in res_blocked["message"]
    assert "None" not in res_blocked["message"]

    # Advance со строковым вердиктом "approved" -> нормализация и успешный переход
    res_ok1 = engine.advance_flow(
        sess["session_id"], deliverable="done", verdict="approved", project_root=proj
    )
    assert res_ok1["status"] != "gate_blocked"
    assert res_ok1["stage"]["id"] == "s_confirm"

    # --- 2. require_human_confirm ---
    # Advance без human_confirmed (даже с непустыми notes) -> блокировка:
    # непустые notes больше не прокси-подтверждение человека (D4)
    res_blocked_notes = engine.advance_flow(
        sess["session_id"],
        deliverable="done",
        verdict="approved",
        notes="Человек подтвердил требования",
        project_root=proj,
    )
    assert res_blocked_notes["status"] == "gate_blocked"
    assert any("human_confirmed" in b for b in res_blocked_notes["blockers"])

    # Advance с явным human_confirmed=True -> успех
    res_ok2 = engine.advance_flow(
        sess["session_id"],
        deliverable="done",
        verdict="approved",
        human_confirmed=True,
        project_root=proj,
    )
    assert res_ok2["status"] != "gate_blocked"
    assert res_ok2["stage"]["id"] == "s_file"

    # --- 3. deliverable_must_exist ---
    # Файл output.txt пока не существует -> блокировка
    res_blocked_file = engine.advance_flow(
        sess["session_id"], deliverable="done", project_root=proj
    )
    assert res_blocked_file["status"] == "gate_blocked"
    assert any("не найден в проекте" in b for b in res_blocked_file["blockers"])

    # Файл создан, но пуст -> блокировка
    (proj / "output.txt").write_text("   \n", encoding="utf-8")
    res_blocked_empty = engine.advance_flow(
        sess["session_id"], deliverable="done", project_root=proj
    )
    assert res_blocked_empty["status"] == "gate_blocked"
    assert any("пуст" in b for b in res_blocked_empty["blockers"])

    # Файл заполнен -> успех
    (proj / "output.txt").write_text("Valid content", encoding="utf-8")
    res_ok3 = engine.advance_flow(sess["session_id"], deliverable="done", project_root=proj)
    assert res_ok3["status"] != "gate_blocked"
    assert res_ok3["stage"]["id"] == "s_combined"

    # --- 4. Комбинация регистрового gate и require_verdict ---
    # Ни файла plan.md, ни verdict -> оба блокера агрегируются
    res_combined = engine.advance_flow(sess["session_id"], deliverable="done", project_root=proj)
    assert res_combined["status"] == "gate_blocked"
    assert res_combined["message"].startswith("Шлюз качества 'validate_plan' не пройден")
    assert any("Этап требует явный вердикт" in b for b in res_combined["blockers"])
    assert any("Целевой файл плана 'plan.md' не найден" in b for b in res_combined["blockers"])

    # Создаем plan.md и передаем verdict -> успех
    (proj / "plan.md").write_text("# Implementation Plan\nStep 1...", encoding="utf-8")
    res_ok4 = engine.advance_flow(
        sess["session_id"], deliverable="plan.md", verdict="approved", project_root=proj
    )
    assert res_ok4["status"] == FlowStatus.COMPLETED.value


def test_deliverable_must_exist_via_dict_deliverable(tmp_path: Path) -> None:
    """Проверяет deliverable_must_exist без target_file через deliverable={'file_path': ...}."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    flows_dir = proj / ".agents" / "flows"
    flows_dir.mkdir(parents=True)

    flow_content = """# Dynamic File Flow
## 1. Dynamic File [id: s1]
<!-- meta: {"deliverable_must_exist": true} -->
Instruction
## 2. Done [id: s2]
Finished
"""
    (flows_dir / "dynamic-flow.md").write_text(flow_content, encoding="utf-8")
    engine = FlowEngine(project_root=proj)
    sess = engine.start_flow("dynamic-flow", project_root=proj)

    # Несуществующий файл через словарь -> блокировка
    res_fail = engine.advance_flow(
        sess["session_id"], deliverable={"file_path": "nonexistent.md"}, project_root=proj
    )
    assert res_fail["status"] == "gate_blocked"
    assert any("nonexistent.md" in b for b in res_fail["blockers"])

    # Существующий файл через словарь -> успех
    (proj / "artifact.md").write_text("Hello World", encoding="utf-8")
    res_ok = engine.advance_flow(
        sess["session_id"], deliverable={"file_path": "artifact.md"}, project_root=proj
    )
    assert res_ok["status"] != "gate_blocked"
    assert res_ok["stage"]["id"] == "s2"


def test_declarative_gate_hint(tmp_path: Path) -> None:
    """Проверяет предупреждение gate_hint для декларативных требований."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    flows_dir = proj / ".agents" / "flows"
    flows_dir.mkdir(parents=True)

    flow_content = """# Hint Flow
## 1. Complex Gate [id: s1]
<!-- meta: {"require_verdict": true, "require_human_confirm": true, "deliverable_must_exist": true} -->
Instruction
## 2. Next [id: s2]
Next instruction
"""
    (flows_dir / "hint-flow.md").write_text(flow_content, encoding="utf-8")
    engine = FlowEngine(project_root=proj)
    sess = engine.start_flow("hint-flow", project_root=proj)

    gate_hint = sess.get("gate_hint", "")
    assert "этап требует явный verdict" in gate_hint
    assert "этап требует подтверждения человека" in gate_hint
    assert "deliverable должен быть существующим файлом" in gate_hint


def test_list_available_flows_intent_group_and_when_to_use(tmp_path: Path) -> None:
    """Проверяет возвращение intent_group и when_to_use в list_available_flows."""
    flows = list_available_flows(tmp_path)
    feat = next((f for f in flows if f["name"] == "feature-delivery"), None)
    assert feat is not None
    assert feat["intent_group"] == "feature-delivery"
    assert isinstance(feat["when_to_use"], list)
    assert len(feat["when_to_use"]) >= 1
