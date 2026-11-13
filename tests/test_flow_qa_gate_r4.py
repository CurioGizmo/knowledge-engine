"""R4: QA-gate принимает BANANA/PENDING как успешные статусы (дефект приёмки).

Воспроизведения из отчёта ревью (docs/analysis/2026-10-18-post-implementation-review.md,
раздел R4): QA-gate обязан:
1. В таблице: строка с непустым, но невалидным статусом (BANANA, PENDING) → блокер unknown_status.
2. В freeform: та же строгость — непризнанный статус сценария → блокер (закрытие обхода
   через альтернативный формат отчёта).
3. Английская колонка Evidence распознаётся как evidence (не классифицируется как id).
4. Общий QA_PASSED не оправдывает PENDING-строку (смешанный случай → fail).
5. Валидные статусы PASS/PASSED/OK/УСПЕШНО/ПРОЙДЕН/QA_PASSED и N/A с обоснованием → pass.
"""

from pathlib import Path

from mcp_skills.flow_engine import GATE_REGISTRY
from mcp_skills.models import FlowSession, FlowStage, FlowStageMeta


def _make_session_stage(tmp_path: Path):
    session = FlowSession(
        id="qa-session-r4",
        project_root=str(tmp_path),
        flow_name="r4-flow",
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
    return session, stage


def test_r4_table_banana_status_is_blocker(tmp_path: Path):
    """Воспроизведение R4-1: статус BANANA в таблице → блокер (не молча пройден)."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session, stage = _make_session_stage(tmp_path)

    report = """# Отчёт приёмки

**Вердикт:** QA_PASSED
**Сессия:** qa-session-r4

| ID | Действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Запуск CLI tests | Exit 0 | Exit 0, 15 tests passed | tests/output.log | BANANA |
"""
    res = gate_fn(session, stage, report, tmp_path, None)
    assert res.passed is False
    assert any("unknown_status" in b or "неизвестный статус" in b.lower() for b in res.blockers), (
        res.blockers
    )


def test_r4_table_pending_status_is_blocker(tmp_path: Path):
    """Воспроизведение R4-1: статус PENDING в таблице → блокер."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session, stage = _make_session_stage(tmp_path)

    report = """# Отчёт приёмки

**Вердикт:** QA_PASSED
**Сессия:** qa-session-r4

| ID | Действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Запуск CLI tests | Exit 0 | Exit 0, 15 tests passed | tests/output.log | PENDING |
"""
    res = gate_fn(session, stage, report, tmp_path, None)
    assert res.passed is False
    assert any("unknown_status" in b or "неизвестный статус" in b.lower() for b in res.blockers), (
        res.blockers
    )


def test_r4_english_evidence_column_recognized(tmp_path: Path):
    """R4-3: английская колонка Evidence классифицируется как evidence, не как id."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session, stage = _make_session_stage(tmp_path)

    report = """# QA Report

**Verdict:** QA_PASSED
**Session:** qa-session-r4

| ID | Action | Expected | Actual | Evidence | Result |
|---|---|---|---|---|---|
| TC-1 | Run CLI tests | Exit 0 | Exit 0, 15 tests passed | tests/output.log | PASS |
"""
    res = gate_fn(session, stage, report, tmp_path, None)
    # Отчёт корректен: не должно быть блокера про отсутствие evidence
    assert not any("evidence" in b.lower() or "доказательств" in b.lower() for b in res.blockers), (
        res.blockers
    )
    assert res.passed is True, res.blockers


def test_r4_mixed_qa_passed_with_pending_row_fails(tmp_path: Path):
    """R4-4: общий QA_PASSED + одна PENDING-строка → fail (смешанный случай)."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session, stage = _make_session_stage(tmp_path)

    report = """# Отчёт приёмки

**Вердикт:** QA_PASSED
**Сессия:** qa-session-r4

| ID | Действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Запуск CLI tests | Exit 0 | Exit 0, 15 tests passed | tests/output.log | PASS |
| TC-2 | Проверка микрофона | Запись звука | Ещё не проверено | tests/output.log | PENDING |
"""
    res = gate_fn(session, stage, report, tmp_path, None)
    assert res.passed is False
    assert any("unknown_status" in b or "неизвестный статус" in b.lower() for b in res.blockers), (
        res.blockers
    )


def test_r4_freeform_banana_scenario_is_blocker(tmp_path: Path):
    """R4-2: обход через freeform — сценарий с невалидным статусом → блокер.

    Freeform-сценарий обязан содержать распознанный валидный статус; общий
    QA_PASSED не оправдывает строку без статуса/с невалидным статусом.
    """
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session, stage = _make_session_stage(tmp_path)

    report = """# Отчёт приёмки

**Вердикт:** QA_PASSED
**Сессия:** qa-session-r4

### Сценарий TC-1: Запуск CLI tests
- Ожидание: Exit 0, все тесты зелёные.
- Факт: Exit 0, 15 tests passed.
- Доказательство: tests/output.log
- Результат: BANANA
"""
    res = gate_fn(session, stage, report, tmp_path, None)
    assert res.passed is False
    assert any("unknown_status" in b or "неизвестный статус" in b.lower() for b in res.blockers), (
        res.blockers
    )


def test_r4_freeform_valid_statuses_pass(tmp_path: Path):
    """R4-5: валидные статусы в freeform проходят (УСПЕШНО, PASS, evidence)."""
    gate_fn = GATE_REGISTRY["validate_qa_report"]
    session, stage = _make_session_stage(tmp_path)
    report = """# Отчёт приёмки

**Вердикт:** QA_PASSED
**Сессия:** qa-session-r4

### Сценарий TC-1: Запуск CLI tests
- Ожидание: Exit 0, все тесты зелёные.
- Факт: Exit 0, 15 tests passed.
- Результат: УСПЕШНО
- Доказательство: tests/output.log
"""
    res = gate_fn(session, stage, report, tmp_path, None)
    assert res.passed is True, res.blockers
    assert res.blockers == []
