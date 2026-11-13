"""Тесты Шага 7: артефакты приёмки вместо пустого approved, gate отчёта, min_engine_version."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from mcp_skills.errors import SessionConflictError
from mcp_skills.flow_engine import GATE_REGISTRY, FlowEngine, GateResult, safe_read_text
from mcp_skills.models import FlowSession, FlowStage, FlowStageMeta, FlowStatus, FlowVerdict
from mcp_skills.flow_storage import FlowStorage

# Test catalog location: env-var override, default to the local checkout.
# Tests that need the real catalog skip when it is not available.
from tests.conftest import get_companion_skills_dir

_catalog = get_companion_skills_dir()
CATALOG_DIR = (
    _catalog
    if _catalog is not None
    else Path(
        os.environ.get(
            "MCP_SKILLS_TEST_CATALOG",
            str(Path(__file__).resolve().parents[1].parent / "mcp_skills"),
        )
    )
)


def test_gate_registry_contains_validate_qa_report():
    """GATE_REGISTRY содержит шлюз validate_qa_report."""
    assert "validate_qa_report" in GATE_REGISTRY
    assert callable(GATE_REGISTRY["validate_qa_report"])


def test_validate_qa_report_missing_deliverable(tmp_path: Path):
    """Шлюз отклоняет отсутствие deliverable."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-1",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    res = gate_fn(session, stage, None, tmp_path, None)
    assert res.passed is False
    assert any("не предоставлен" in b.lower() or "пуст" in b.lower() for b in res.blockers)


def test_validate_qa_report_empty_content(tmp_path: Path):
    """Шлюз отклоняет пустой текст отчёта."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-1",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    res = gate_fn(session, stage, "   ", tmp_path, None)
    assert res.passed is False
    assert any("пуст" in b.lower() for b in res.blockers)


def test_validate_qa_report_missing_checks_and_evidence(tmp_path: Path):
    """Шлюз отклоняет отчёт без перечня проверок и evidence."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-1",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    empty_report = "# Отчёт живой приёмки\nВсё проверено, всё работает. Вердикт: QA_PASSED."
    res = gate_fn(session, stage, empty_report, tmp_path, None)
    assert res.passed is False
    assert any("проверок" in b.lower() or "сценари" in b.lower() for b in res.blockers)


def test_validate_qa_report_failed_checks_block_completion(tmp_path: Path):
    """Наличие FAILED проверок блокирует успешное прохождение гейта."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-1",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    failed_report = """# Отчёт живой приёмки

**Вердикт:** QA_FAILED

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | GET /api/v1/health | 200 OK | 500 Error | logs/test.log | FAIL |
"""
    res = gate_fn(session, stage, failed_report, tmp_path, None)
    assert res.passed is False
    assert any("failed" in b.lower() or "непройден" in b.lower() for b in res.blockers)


def test_validate_qa_report_blocked_checks_block_completion(tmp_path: Path):
    """Статус BLOCKED не допускает успешного завершения."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-1",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    blocked_report = """# Отчёт живой приёмки

**Вердикт:** BLOCKED

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | GET /api/v1/health | 200 OK | Timeout | logs/net.log | BLOCKED |
"""
    res = gate_fn(session, stage, blocked_report, tmp_path, None)
    assert res.passed is False
    assert any("blocked" in b.lower() or "заблокирован" in b.lower() for b in res.blockers)


def test_validate_qa_report_na_without_reason_blocked(tmp_path: Path):
    """Статус N/A без указания причины блокируется."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-1",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    na_no_reason = """# Отчёт живой приёмки

**Вердикт:** QA_PASSED

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Проверка UI в браузере | Отображение кнопки | Не проверялось | logs/run.log | N/A |
"""
    res = gate_fn(session, stage, na_no_reason, tmp_path, None)
    assert res.passed is False
    assert any(
        "n/a" in b.lower() and "причин" in b.lower() or "обоснован" in b.lower()
        for b in res.blockers
    )


def test_validate_qa_report_valid_full_report_passes(tmp_path: Path):
    """Корректный отчёт со всеми проверками, evidence и обоснованным N/A проходит."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-1",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    valid_report = """# Отчёт живой приёмки

**Вердикт:** QA_PASSED
**Сессия:** qa-session-1

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Запуск CLI tests | Exit 0 | Exit 0, 15 tests passed | tests/output.log | PASS |
| TC-2 | Проверка микрофона | Запись звука | Не применимо в headless CI | docs/testing.md | N/A (headless CI без аудиоустройства) |

## Ограничения
TC-2 отмечен как N/A, так как тестовый стенд запущен в headless CI окружении.
"""
    res = gate_fn(session, stage, valid_report, tmp_path, None)
    assert res.passed is True
    assert res.blockers == []


def test_validate_qa_report_gate_does_not_mutate_session_context(tmp_path: Path):
    """Gate не меняет qa_report_bound/контекст до успешной проверки (шаг 7, строка 92).

    Проваленный gate не должен оставлять флаг в сессии: он ставится только
    в advance-логике после успешной проверки, вместе с атомарным
    update_session перехода.
    """
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-ctx",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        context_data={},
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    # Проваленный отчёт (FAILED проверка)
    failed_report = """# Отчёт живой приёмки
**Сессия:** qa-session-ctx

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Run CLI | exit 0 | exit 1 | logs/cli.log | FAIL |
"""
    res = gate_fn(session, stage, failed_report, tmp_path, None)
    assert res.passed is False
    # Gate не мутировал контекст сессии во время валидации
    assert session.context_data.get("qa_report_bound") is None


def test_validate_qa_report_missing_session_id_blocked(tmp_path: Path):
    """Отчёт с полными проверками, QA_PASSED, но БЕЗ session_id → gate_blocked.

    Отсутствие session_id — ошибка, а не разрешение принять старый отчёт
    (шаг 7, план строка 92).
    """
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-session-nosid",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    # Полный корректный отчёт, но без упоминания session_id
    report_no_sid = """# Отчёт живой приёмки

**Вердикт:** QA_PASSED

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Run CLI tests | Exit 0 | Exit 0 | tests/output.log | PASS |
"""
    res = gate_fn(session, stage, report_no_sid, tmp_path, None)
    assert res.passed is False
    assert any("session" in b.lower() or "сесси" in b.lower() for b in res.blockers)


def test_validate_qa_report_task_id_binding_not_accepted(tmp_path: Path):
    """task_id сам по себе не отличает повторные запуски: совпадение только с session.id.

    Отчёт, привязанный к task_id текущей сессии (а не к session.id),
    должен блокироваться (шаг 7, план строка 92).
    """
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="sess-A",
        task_id="task-X",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    # Отчёт привязан к task_id текущей сессии, а не к session.id
    report_bound_to_task = """# Отчёт живой приёмки

**Вердикт:** QA_PASSED
**Сессия:** task-X

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Run CLI tests | Exit 0 | Exit 0 | tests/output.log | PASS |
"""
    res = gate_fn(session, stage, report_bound_to_task, tmp_path, None)
    assert res.passed is False
    assert any("task" in b.lower() or "сесси" in b.lower() for b in res.blockers)


def test_min_engine_version_check():
    """Проверка совместимости min_engine_version с версией engine."""
    from mcp_skills.catalog_version import check_engine_version_compatibility

    # Совместимая версия
    assert check_engine_version_compatibility("0.2.0", "0.3.0") is None
    assert check_engine_version_compatibility("0.3.0", "0.3.0") is None
    assert check_engine_version_compatibility(None, "0.3.0") is None

    # Несовместимая (engine слишком старый)
    err = check_engine_version_compatibility("0.4.0", "0.3.0")
    assert err is not None
    assert "ниже требуемой каталогом" in err
    assert "0.4.0" in err
    assert "0.3.0" in err


def test_unknown_gate_rejected(tmp_path: Path):
    """Неизвестный gate отклоняется с ошибкой GateValidationError при advance."""
    from mcp_skills.flow_engine import FlowEngine, GateValidationError

    engine = FlowEngine()
    storage = FlowStorage(tmp_path / "flow.db")
    session = FlowSession(
        id="gate-test-1",
        project_root=str(tmp_path),
        flow_name="dummy-flow",
        current_stage_id="stage-1",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="stage-1",
        title="Stage 1",
        instruction="Do something",
        meta=FlowStageMeta(gate="non_existent_gate_123"),
    )

    with pytest.raises(
        GateValidationError, match="Неизвестный шлюз качества 'non_existent_gate_123'"
    ):
        engine._check_quality_gate(session, stage, None, tmp_path, storage)


def test_live_qa_flow_e2e_with_qa_report(tmp_path: Path):
    """Сквозной прогон live-qa-flow с проверкой отчёта приёмки."""
    catalog_flows = CATALOG_DIR / "flows"
    if not catalog_flows.is_dir():
        pytest.skip("Каталог flows недоступен")

    engine = FlowEngine(skills_dir=CATALOG_DIR)

    # 1. Старт live-qa-flow
    res_start = engine.start_flow("live-qa-flow", project_root=tmp_path)
    assert res_start["status"] == "active"
    session_id = res_start["session_id"]
    assert res_start["stage"]["id"] == "inspect_runbook"

    # 2. Переход со стадии inspect_runbook
    res_s2 = engine.advance_flow(
        session_id, project_root=tmp_path, expected_stage_id="inspect_runbook"
    )
    assert res_s2["status"] == "active"
    assert res_s2["stage"]["id"] == "execute_verification"

    # 3. Переход со стадии execute_verification (требует verdict)
    res_s3 = engine.advance_flow(
        session_id,
        verdict="approved",
        notes="All checks passed by qa-verifier",
        project_root=tmp_path,
        expected_stage_id="execute_verification",
    )
    assert res_s3["status"] == "active"
    assert res_s3["stage"]["id"] == "report_and_verdict"

    # 4. Попытка завершить report_and_verdict БЕЗ deliverable -> блокируется
    blocked_res = engine.advance_flow(
        session_id,
        project_root=tmp_path,
        expected_stage_id="report_and_verdict",
    )
    assert blocked_res["status"] == "gate_blocked"
    assert any(
        "не предоставлен" in b.lower() or "не является существующим" in b.lower()
        for b in blocked_res["blockers"]
    )

    # 5. Попытка завершить с проваленным отчётом (FAIL) -> блокируется
    fail_report_path = tmp_path / "qa-failed-report.md"
    fail_report_path.write_text(
        """# Отчёт живой приёмки

**Вердикт:** QA_FAILED

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Run CLI | exit 0 | exit 1 | logs/cli.log | FAIL |
""",
        encoding="utf-8",
    )
    blocked_fail_res = engine.advance_flow(
        session_id,
        deliverable="qa-failed-report.md",
        project_root=tmp_path,
        expected_stage_id="report_and_verdict",
    )
    assert blocked_fail_res["status"] == "gate_blocked"
    assert any(
        "failed" in b.lower() or "непройден" in b.lower() for b in blocked_fail_res["blockers"]
    )

    # Проваленный gate не оставляет qa_report_bound в сохранённой сессии
    store = engine._get_storage(tmp_path)
    stored_after_fail = store.get_session(session_id)
    assert stored_after_fail.context_data.get("qa_report_bound") is None

    # 6. Успешный отчёт со всеми проверками и доказательствами -> flow завершается (COMPLETED)
    pass_report_path = tmp_path / "qa-passed-report.md"
    pass_report_path.write_text(
        f"""# Отчёт живой приёмки

**Вердикт:** QA_PASSED
**Сессия:** {session_id}

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Run CLI | exit 0 | exit 0 | tests/cli.log | PASS |
""",
        encoding="utf-8",
    )
    completed_res = engine.advance_flow(
        session_id,
        deliverable="qa-passed-report.md",
        project_root=tmp_path,
        expected_stage_id="report_and_verdict",
    )
    assert completed_res["status"] == "completed"

    # Успешный gate фиксирует qa_report_bound в сохранённой сессии
    stored_after_pass = store.get_session(session_id)
    assert stored_after_pass.context_data.get("qa_report_bound") is True


# --- Ревью: регрессионные тесты (CRITICAL retry+target, MAJOR пустой статус) ---


def test_retry_with_target_updates_session_not_insert(tmp_path: Path):
    """retry с явным target не падает на PK и сохраняет target в сессии.

    Регрессия CRITICAL из ревью: save_session (INSERT) на существующей сессии
    вызывал sqlite3.IntegrityError; смена target через retry была сломана.
    """
    catalog_flows = CATALOG_DIR / "flows"
    if not catalog_flows.is_dir():
        pytest.skip("Каталог flows недоступен")

    engine = FlowEngine(skills_dir=CATALOG_DIR)
    res = engine.start_flow("live-qa-flow", project_root=tmp_path)
    session_id = res["session_id"]

    from mcp_skills.flow_engine import step_flow

    res_retry = step_flow(
        action="retry",
        session_id=session_id,
        target="opencode",
        project_root=tmp_path,
    )
    assert res_retry["status"] == "active"
    assert res_retry["next_call"]["arguments"]["target"] == "opencode"

    from mcp_core.paths import get_cache_dir

    session = FlowStorage(get_cache_dir(tmp_path) / "flows.db").get_session(session_id)
    assert session is not None
    assert isinstance(session.context_data, dict)
    assert session.context_data.get("target") == "opencode"

    # повторная смена target через retry — не INSERT, атомарный update
    res_retry2 = step_flow(
        action="retry",
        session_id=session_id,
        target="prompt",
        project_root=tmp_path,
    )
    assert res_retry2["next_call"]["arguments"]["target"] == "prompt"


def test_qa_report_row_with_empty_status_blocks(tmp_path: Path):
    """Строка таблицы с пустой ячейкой Результат блокирует приёмку.

    Регрессия MAJOR из ревью: пустой res_cell не попадал ни в одну ветку
    и проходил молча — план требует распознанный непустой статус каждой строки.
    """
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session = FlowSession(
        id="qa-empty-status",
        project_root=str(tmp_path),
        flow_name="live-qa-flow",
        current_stage_id="report_and_verdict",
        created_at=0.0,
        updated_at=0.0,
    )
    stage = FlowStage(
        id="report_and_verdict",
        title="Отчёт",
        instruction="Save report",
        meta=FlowStageMeta(gate="validate_qa_report", deliverable_must_exist=True),
    )

    report_empty_status = f"""# Отчёт живой приёмки

**Вердикт:** QA_PASSED
**Сессия:** {session.id}

## Сценарии
| ID | Предусловия и действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Run CLI | exit 0 | exit 0 | tests/cli.log | |
"""
    res = gate_fn(session, stage, report_empty_status, tmp_path, None)
    assert res.passed is False
    assert any("статус" in b.lower() for b in res.blockers)


# --- План A (2026-10-18 followup): retry, конфликт версий, target после restart ---


def _get_store(tmp_path: Path) -> FlowStorage:
    """Возвращает FlowStorage поверх flows.db проекта из tmp_path."""
    from mcp_core.paths import get_cache_dir

    return FlowStorage(get_cache_dir(tmp_path) / "flows.db")


def test_retry_preserves_stage_iteration_and_budget(tmp_path: Path):
    """retry не меняет current_stage_id и iteration_count (бюджет не расходуется).

    План A: iteration_count/loop_max расходуется только advance с
    verdict='needs_work'; повторный retry не должен ни двигать этап,
    ни тратить бюджет итераций.
    """
    catalog_flows = CATALOG_DIR / "flows"
    if not catalog_flows.is_dir():
        pytest.skip("Каталог flows недоступен")

    engine = FlowEngine(skills_dir=CATALOG_DIR)
    res = engine.start_flow("live-qa-flow", project_root=tmp_path)
    session_id = res["session_id"]
    assert res["stage"]["id"] == "inspect_runbook"

    # Два needs_work-перехода на этапе execute_verification (требует verdict):
    # первый advance проходит inspect_runbook без вердикт-требований.
    engine.advance_flow(session_id, project_root=tmp_path, expected_stage_id="inspect_runbook")
    engine.advance_flow(
        session_id,
        verdict="needs_work",
        project_root=tmp_path,
        expected_stage_id="execute_verification",
    )
    engine.advance_flow(
        session_id,
        verdict="needs_work",
        project_root=tmp_path,
        expected_stage_id="execute_verification",
    )

    store = _get_store(tmp_path)
    before = store.get_session(session_id)
    assert before is not None
    assert before.iteration_count == 3  # start=1 + два needs_work
    assert before.current_stage_id == "execute_verification"

    from mcp_skills.flow_engine import step_flow

    res_retry = step_flow(
        action="retry",
        session_id=session_id,
        project_root=tmp_path,
        skills_dir=CATALOG_DIR,
    )
    assert res_retry["status"] == "active"
    assert res_retry["stage"]["id"] == "execute_verification"

    after = store.get_session(session_id)
    assert after is not None
    assert after.current_stage_id == before.current_stage_id
    assert after.iteration_count == before.iteration_count
    # Повторный retry — тоже не расходует бюджет и не меняет этап
    step_flow(action="retry", session_id=session_id, project_root=tmp_path, skills_dir=CATALOG_DIR)
    final = store.get_session(session_id)
    assert final is not None
    assert final.iteration_count == before.iteration_count
    assert final.current_stage_id == before.current_stage_id


def test_update_session_version_conflict_does_not_lose_data(tmp_path: Path):
    """Конфликт версий: update_session отклоняет запись, данные в БД не перезаписаны.

    План A: параллельная модификация сессии (version изменён вторым
    обновлением) должна приводить к SessionConflictError, а не к молчаливой
    перезаписи чужих изменений.
    """
    store = FlowStorage(db_path=tmp_path / "flows.db")
    session = FlowSession(
        id="flow-conflict-test",
        project_root=str(tmp_path),
        flow_name="dummy",
        current_stage_id="stage-1",
        iteration_count=1,
        version=1,
        context_data={"parallel_writer_marker": False},
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.save_session(session)

    # «Параллельный писатель»: загружает сессию, меняет контекст и коммитит.
    parallel = store.get_session("flow-conflict-test")
    assert parallel is not None
    parallel.context_data["parallel_writer_marker"] = True
    store.update_session(parallel)

    # «Отложенный писатель» всё ещё держит версию 1: его update обязан
    # упасть SessionConflictError, а изменение параллельного писателя — сохраниться.
    session.context_data["stale_writer_marker"] = True
    with pytest.raises(SessionConflictError):
        store.update_session(session)

    stored = store.get_session("flow-conflict-test")
    assert stored is not None
    assert stored.context_data.get("parallel_writer_marker") is True
    assert stored.context_data.get("stale_writer_marker") is None
    assert stored.version == 2


def test_step_retry_with_target_on_stale_version_raises_session_conflict(tmp_path: Path):
    """retry с target при параллельной модификации кидает SessionConflictError наружу.

    План A: optimistic locking на retry не должен молча перезаписать
    чужие изменения. Исключение доменное и маппится в error-envelope
    как SESSION_CONFLICT (см. отдельный тест правила envelope).
    """
    catalog_flows = CATALOG_DIR / "flows"
    if not catalog_flows.is_dir():
        pytest.skip("Каталог flows недоступен")

    engine = FlowEngine(skills_dir=CATALOG_DIR)
    res = engine.start_flow("live-qa-flow", project_root=tmp_path)
    session_id = res["session_id"]

    from mcp_skills.flow_engine import step_flow

    # Сначала retry с target — успешный путь (обновление через update_session).
    step_flow(
        action="retry",
        session_id=session_id,
        target="opencode",
        project_root=tmp_path,
        skills_dir=CATALOG_DIR,
    )

    # Теперь эмулируем параллельную модификацию: другой экземпляр
    # FlowStorage меняет сессию, пока наш retry-вызов держит устаревшую копию.
    store = _get_store(tmp_path)
    parallel = store.get_session(session_id)
    assert parallel is not None
    parallel.context_data["parallel_writer_marker"] = True
    store.update_session(parallel)

    # Сессия с устаревшей версией в context_data: получаем копию «из прошлого»
    # и пытаемся retry с target — session.version отстал от БД.
    stale = store.get_session(session_id)
    assert stale is not None
    # parallel уже закоммитил версию N+1; откатываем версию локальной копии
    # для эмуляции отложенного писателя.
    stale.version -= 1
    stale.context_data["stale_writer_marker"] = True
    with pytest.raises(SessionConflictError):
        store.update_session(
            stale,
            history_events=[
                {
                    "stage_id": stale.current_stage_id,
                    "iteration": stale.iteration_count,
                    "event": "target_changed",
                    "notes": "target -> opencode",
                }
            ],
        )

    # Данные параллельного писателя не потеряны
    stored = store.get_session(session_id)
    assert stored is not None
    assert stored.context_data.get("parallel_writer_marker") is True
    assert stored.context_data.get("stale_writer_marker") is None


def test_session_conflict_error_mapped_to_domain_envelope():
    """SessionConflictError из retry/advance маппится в SESSION_CONFLICT, не UNEXPECTED_ERROR.

    План A: проверка правила в SKILLS_ERROR_RULES (error_envelope.py) —
    доменная ошибка конфликтов сессии не должна попадать в ветку
    UNEXPECTED_ERROR с logger.exception.
    """
    from mcp_skills.error_envelope import _handle_tool_error
    from mcp_skills.errors import SessionConflictError as SCE

    resp = _handle_tool_error(
        SCE("Конфликт параллельного изменения сессии flow-x (версия 3)"),
        "step_flow",
    )
    assert resp["error"] is True
    assert resp["status"] == "rejected"
    assert resp["error_type"] == "SessionConflictError"
    assert resp["reason"] == "SESSION_CONFLICT"


def test_missing_target_keeps_choice_after_restart(tmp_path: Path):
    """Отсутствие target (None) сохраняет выбор сессии после «рестарта» движка.

    План A: новая FlowStorage/FlowEngine поверх той же БД (имитация
    restart процесса): status и retry без target возвращают сохранённый
    target='opencode', а не молчаливый antigravity.
    """
    catalog_flows = CATALOG_DIR / "flows"
    if not catalog_flows.is_dir():
        pytest.skip("Каталог flows недоступен")

    from mcp_skills.flow_engine import step_flow

    # 1. Старт с явным target=opencode
    res = step_flow(
        action="advance",
        flow="live-qa-flow",
        target="opencode",
        project_root=tmp_path,
        skills_dir=CATALOG_DIR,
    )
    session_id = res["session_id"]
    assert res["next_call"]["arguments"]["target"] == "opencode"

    # 2. «Рестарт»: новый FlowStorage/FlowEngine поверх той же БД проекта.
    store = _get_store(tmp_path)
    assert store.get_session(session_id) is not None

    # 3. status без target — next_call.target = opencode (сохранённый выбор)
    status_res = step_flow(
        action="status",
        session_id=session_id,
        project_root=tmp_path,
        skills_dir=CATALOG_DIR,
    )
    assert status_res["next_call"] is not None
    assert status_res["next_call"]["arguments"]["target"] == "opencode"

    # 4. retry без target — тоже сохранённый выбор
    retry_res = step_flow(
        action="retry",
        session_id=session_id,
        project_root=tmp_path,
        skills_dir=CATALOG_DIR,
    )
    assert retry_res["status"] == "active"
    assert retry_res["next_call"]["arguments"]["target"] == "opencode"
    # Сама сессия не переключена на antigravity
    stored = store.get_session(session_id)
    assert stored is not None
    assert stored.context_data.get("target") == "opencode"
