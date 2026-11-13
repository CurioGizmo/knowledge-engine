"""Разрешение графа зависимостей (DAG) и отбор готовых задач бэклога."""

from __future__ import annotations

from mcp_audit.models import AuditAutonomy, AuditComplexity, AuditIssue, AuditPriority, AuditStatus

__all__ = ["detect_dependency_cycles", "get_ready_issues"]

PRIORITY_WEIGHTS: dict[AuditPriority, int] = {
    AuditPriority.CRITICAL: 0,
    AuditPriority.IMPORTANT: 1,
    AuditPriority.SUGGESTION: 2,
}

AUTONOMY_WEIGHTS: dict[AuditAutonomy, int] = {
    AuditAutonomy.HIGH: 0,
    AuditAutonomy.MEDIUM: 1,
    AuditAutonomy.LOW: 2,
}

COMPLEXITY_WEIGHTS: dict[AuditComplexity, int] = {
    AuditComplexity.L: 0,
    AuditComplexity.M: 1,
    AuditComplexity.H: 2,
}


def detect_dependency_cycles(issues: list[AuditIssue]) -> list[list[str]]:
    """Находит циклы в графе зависимостей blocked_by (DFS)."""
    adj: dict[str, list[str]] = {issue.id: issue.blocked_by for issue in issues}
    visited: dict[str, int] = {}  # 0 = unvisited, 1 = visiting, 2 = visited
    cycles: list[list[str]] = []

    def dfs(node: str, path: list[str]) -> None:
        visited[node] = 1
        path.append(node)

        for neighbor in adj.get(node, []):
            if neighbor not in adj:
                continue
            if visited.get(neighbor, 0) == 1:
                # Нашли цикл
                idx = path.index(neighbor)
                cycles.append([*path[idx:], neighbor])
            elif visited.get(neighbor, 0) == 0:
                dfs(neighbor, path)

        path.pop()
        visited[node] = 2

    for issue_id in adj:
        if visited.get(issue_id, 0) == 0:
            dfs(issue_id, [])

    return cycles


def get_ready_issues(
    issues: list[AuditIssue],
    category: str | None = None,
) -> list[AuditIssue]:
    """Отбирает задачи со статусом PLANNED, у которых закрыты все блокеры.

    Сортирует по:
    1. Priority (Critical -> Important -> Suggestion)
    2. Autonomy (High -> Medium -> Low)
    3. Complexity (L -> M -> H)
    """
    status_map = {issue.id: issue.status for issue in issues}
    ready: list[AuditIssue] = []

    for issue in issues:
        if issue.status != AuditStatus.PLANNED:
            continue

        if category and issue.category.lower() != category.lower():
            continue

        # Проверяем все блокеры
        all_blockers_resolved = True
        for blocker_id in issue.blocked_by:
            blocker_status = status_map.get(blocker_id)
            if blocker_status not in (AuditStatus.COMPLETED, AuditStatus.OBSOLETE):
                all_blockers_resolved = False
                break

        if all_blockers_resolved:
            ready.append(issue)

    # Сортировка
    def sort_key(item: AuditIssue) -> tuple[int, int, int, str]:
        return (
            PRIORITY_WEIGHTS.get(item.priority, 1),
            AUTONOMY_WEIGHTS.get(item.autonomy, 0),
            COMPLEXITY_WEIGHTS.get(item.complexity, 1),
            item.id,
        )

    ready.sort(key=sort_key)
    return ready
