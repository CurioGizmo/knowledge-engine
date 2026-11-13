"""Тесты графа зависимостей (DAG) и отбора готовых задач."""

from mcp_audit.dag import detect_dependency_cycles, get_ready_issues
from mcp_audit.models import (
    AuditAutonomy,
    AuditComplexity,
    AuditIssue,
    AuditPriority,
    AuditStatus,
)


def test_get_ready_issues_filters_unresolved_blockers():
    issue1 = AuditIssue(
        id="aud-1",
        title="Базовая миграция",
        category="db",
        status=AuditStatus.COMPLETED,
        symptom="DB needs migration",
    )
    issue2 = AuditIssue(
        id="aud-2",
        title="Зависимая задача",
        category="db",
        status=AuditStatus.PLANNED,
        blocked_by=["aud-1"],
        symptom="Needs aud-1",
    )
    issue3 = AuditIssue(
        id="aud-3",
        title="Заблокированная незакрытой задачей",
        category="db",
        status=AuditStatus.PLANNED,
        blocked_by=["aud-2"],
        symptom="Needs aud-2",
    )

    ready = get_ready_issues([issue1, issue2, issue3])
    # aud-1 completed (не входит в ready), aud-2 готов (aud-1 completed), aud-3 ждет aud-2
    assert len(ready) == 1
    assert ready[0].id == "aud-2"


def test_get_ready_issues_sorting():
    crit = AuditIssue(
        id="aud-crit",
        title="Критическая",
        category="backend",
        priority=AuditPriority.CRITICAL,
        complexity=AuditComplexity.H,
        status=AuditStatus.PLANNED,
        symptom="Crash",
    )
    sugg_easy = AuditIssue(
        id="aud-sugg",
        title="Предложение легкое",
        category="backend",
        priority=AuditPriority.SUGGESTION,
        complexity=AuditComplexity.L,
        status=AuditStatus.PLANNED,
        symptom="Refactor",
    )
    imp_easy = AuditIssue(
        id="aud-imp",
        title="Важная легкая",
        category="backend",
        priority=AuditPriority.IMPORTANT,
        complexity=AuditComplexity.L,
        status=AuditStatus.PLANNED,
        symptom="Perf",
    )

    ready = get_ready_issues([sugg_easy, crit, imp_easy])
    # Сортировка по приоритету: CRITICAL -> IMPORTANT -> SUGGESTION
    assert [r.id for r in ready] == ["aud-crit", "aud-imp", "aud-sugg"]


def test_detect_dependency_cycles():
    issue_a = AuditIssue(
        id="aud-a",
        title="A",
        category="core",
        status=AuditStatus.PLANNED,
        blocked_by=["aud-b"],
        symptom="s",
    )
    issue_b = AuditIssue(
        id="aud-b",
        title="B",
        category="core",
        status=AuditStatus.PLANNED,
        blocked_by=["aud-a"],
        symptom="s",
    )
    issue_c = AuditIssue(
        id="aud-c",
        title="C",
        category="core",
        status=AuditStatus.PLANNED,
        blocked_by=[],
        symptom="s",
    )

    cycles = detect_dependency_cycles([issue_a, issue_b, issue_c])
    assert len(cycles) > 0
    cycle_nodes = set(cycles[0])
    assert "aud-a" in cycle_nodes and "aud-b" in cycle_nodes
