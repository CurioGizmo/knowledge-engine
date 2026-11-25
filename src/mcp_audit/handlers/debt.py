"""Обработчики и регистрация инструментов технического долга (MCP Audit Engine)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from mcp_audit.dag import detect_dependency_cycles, get_ready_issues
from mcp_audit.handlers.common import (
    DEFAULT_LIMIT,
    AuditServiceBundle,
    AuditToolReg,
    CoercedInt,
    CoercedStrSeq,
    DuplicateDebtError,
    _AuditResolver,
    _make_reg,
    _tool_error_guard,
)
from mcp_audit.models import (
    AuditAutonomy,
    AuditComplexity,
    AuditIssue,
    AuditPriority,
    AuditStatus,
)
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer, _advisory_lock
from mcp_core.paths import ensure_root_anchored
from mcp_core.sanitizer import normalize_category_slug, sanitize_task_id
from mcp_core.service_pool import ProjectServicePool
from mcp_core.tool_contract import PROJECT_PATH_PARAM

__all__ = [
    "_normalize_target_file_for_cmp",
    "_register_debt_tools",
    "_validate_symbol",
    "_validate_target_file",
    "check_known_debt",
    "get_ready_backlog",
    "record_debt",
    "register_debt_tools",
    "search_debt",
    "update_debt_status",
]


def check_known_debt(
    symptom: str,
    file_path: str | None = None,
    symbol: str | None = None,
    category: str | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Проверяет симптом на наличие известного техдолга или регрессии для скилла code-review."""
    import mcp_audit.server as srv

    _sync, store = srv._get_synced_storage(synchronizer, storage)
    resp = store.check_known_debt(
        symptom=symptom,
        file_path=file_path,
        symbol=symbol,
        category=category,
    )
    return resp.model_dump()


def get_ready_backlog(
    category: str | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Возвращает задачи в статусе planned, все блокеры которых закрыты (completed или obsolete).

    A4: задачи с dangling-блокерами (blocker_id не существует среди карточек)
    не попадают в ready молча — фиксируются в поле warnings для диагностики.
    """
    import mcp_audit.server as srv

    _sync, store = srv._get_synced_storage(synchronizer, storage)
    all_issues = store.list_all_issues()
    ready = get_ready_issues(all_issues, category=category)

    # A4: диагностика dangling-блокеров
    status_map = {issue.id: issue.status for issue in all_issues}
    warnings: list[str] = []
    seen_blockers: set[str] = set()
    for issue in all_issues:
        if issue.status != AuditStatus.PLANNED:
            continue
        if category and issue.category.lower() != category.lower():
            continue
        for blocker_id in issue.blocked_by:
            if blocker_id not in status_map and blocker_id not in seen_blockers:
                seen_blockers.add(blocker_id)
                warnings.append(
                    f"Задача '{issue.id}' заблокирована несуществующей карточкой "
                    f"'{blocker_id}' (dangling blocked_by) — проверьте id блокера"
                )

    result: dict[str, Any] = {
        "count": len(ready),
        "category": category,
        "ready_issues": [issue.model_dump() for issue in ready],
    }
    if warnings:
        result["warnings"] = warnings
    return result


def search_debt(
    query: str = "",
    category: str | None = None,
    status: str = "planned",
    limit: int = 10,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Полнотекстовый поиск по карточкам аудита и техдолга.

    Статусы долга: planned, in_progress, completed, obsolete; status="all"
    отключает фильтр по статусу. Полностью неизвестный статус отвергается
    ошибкой валидации, а не пустым результатом.
    """
    import mcp_audit.server as srv

    _sync, store = srv._get_synced_storage(synchronizer, storage)
    valid_debt_statuses = {s.value for s in AuditStatus}
    if status != "all" and status not in valid_debt_statuses:
        msg = (
            f"Unknown status '{status}' for debt search. "
            f"Valid statuses: {', '.join(sorted(valid_debt_statuses))} (or 'all')."
        )
        raise ValueError(msg)
    status_filter = None if status == "all" else status
    effective_limit = max(1, min(limit, 100)) if limit > 0 else DEFAULT_LIMIT
    results = store.search_issues(
        query=query,
        category=category,
        status=status_filter,
        limit=effective_limit,
    )
    return {
        "count": len(results),
        "query": query,
        "category": category,
        "results": results,
    }


def _normalize_target_file_for_cmp(target_file: str | None) -> str | None:
    """Нормализует target_file существующей карточки для сравнения в дедупе.

    Ревью r2: карточки, записанные до канонизации («./src/x.py», «src\\x.py»,
    «foo/../src/x.py»), должны распознаваться как тот же путь, что и
    нормализованный «src/x.py».

    Ревью r4 (п. 4): невалидное значение НЕ превращается в пустую строку —
    иначе легаси-карточка с «../../outside» ложно блокировала бы новые
    карточки без target_file (у них norm_target тоже пуст). Возвращается
    None-сигнал «кандидат не участвует в сравнении по пути»: при None
    текущая карточка пропускается в дедупе, а «пустой == пустой» не
    возникает.
    """
    if not target_file:
        return ""
    try:
        normalized = _validate_target_file(target_file)
    except ValueError:
        return None
    return normalized or ""


def _validate_target_file(target_file: str | None) -> str | None:
    """Валидирует относительный проектный путь target_file."""
    if target_file is None:
        return None
    cleaned = target_file.strip()
    if not cleaned:
        return None

    if any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
        raise ValueError(f"Недопустимый target_file: содержит управляющие символы: {cleaned!r}")

    if cleaned.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", cleaned):
        raise ValueError(
            f"Недопустимый target_file: путь должен быть относительным, получен абсолютный: {cleaned!r}"
        )

    posix_cleaned = cleaned.replace("\\", "/")
    posix_path = PurePosixPath(posix_cleaned)

    norm_parts: list[str] = []
    for part in posix_path.parts:
        if part == ".":
            continue
        if part == "..":
            if not norm_parts:
                raise ValueError(
                    f"Недопустимый target_file: путь выходит за пределы корня проекта: {cleaned!r}"
                )
            norm_parts.pop()
            continue
        norm_parts.append(part)
    normalized = "/".join(norm_parts)

    if not normalized or normalized == ".":
        raise ValueError(f"Недопустимый target_file: пустой путь после нормализации: {cleaned!r}")

    return normalized


def _validate_symbol(symbol: str | None) -> str | None:
    """Валидирует метку символа."""
    if symbol is None:
        return None
    cleaned = symbol.strip()
    if not cleaned:
        raise ValueError("Поле 'symbol' не может быть пустой строкой")

    if any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
        raise ValueError(f"Недопустимый symbol: содержит управляющие символы: {cleaned!r}")

    return cleaned


def record_debt(
    title: str,
    category: str | None = None,
    priority: str = "important",
    symptom: str = "",
    root_cause: str | None = None,
    solution: str | None = None,
    target_file: str | None = None,
    symbol: str | None = None,
    autonomy: str = "high",
    complexity: str = "M",
    blocked_by: list[str] | None = None,
    task_id: str | None = None,
    force: bool = False,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Создаёт новую атомарную карточку технического долга в .agents/audit/todo/<category>/."""
    if not title or not title.strip():
        raise ValueError("Заголовок карточки долга (title) не может быть пустым")
    clean_title = title.strip()
    clean_target_file = _validate_target_file(target_file)
    clean_symbol = _validate_symbol(symbol)

    clean_category = normalize_category_slug(category or "general")
    clean_task = sanitize_task_id(task_id) if task_id else None

    clean_priority = priority.lower() if priority else "important"
    if clean_priority not in ("critical", "important", "suggestion"):
        msg = f"Invalid priority '{priority}': expected one of critical, important, suggestion"
        raise ValueError(msg)
    clean_autonomy = autonomy.lower() if autonomy else "high"
    if clean_autonomy not in ("high", "medium", "low"):
        msg = f"Invalid autonomy '{autonomy}': expected one of high, medium, low"
        raise ValueError(msg)
    clean_complexity = complexity.upper() if complexity else "M"
    if clean_complexity not in ("L", "M", "H"):
        msg = f"Invalid complexity '{complexity}': expected one of L, M, H"
        raise ValueError(msg)

    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    store = storage or srv.get_storage()

    clean_symptom = symptom.strip() if symptom and symptom.strip() else clean_title

    # Блокировка дедупликации по сигнатуре карточки
    norm_title = clean_title.lower()
    norm_symptom = clean_symptom.lower()
    norm_target = clean_target_file or ""
    norm_sym = clean_symbol or ""
    dedup_sig = f"{clean_category}|{norm_title}|{norm_symptom}|{norm_target}|{norm_sym}"
    dedup_hash = hashlib.sha256(dedup_sig.encode()).hexdigest()[:16]

    lock_dir = sync.audit_dir / ".locks"
    lock_path = lock_dir / f"debt-dedup-{dedup_hash}.lock"

    with _advisory_lock(lock_path):
        sync.sync()
        existing_ids = store.get_all_ids()

        clean_blocked_by = blocked_by or []
        if clean_blocked_by:
            unknown_blockers = [bid for bid in clean_blocked_by if bid not in existing_ids]
            if unknown_blockers:
                msg = f"Указаны несуществующие ID в blocked_by: {', '.join(unknown_blockers)}"
                raise ValueError(msg)

        if not force:
            open_issues = [
                iss
                for iss in store.list_all_issues(category=clean_category)
                if iss.status in (AuditStatus.PLANNED, AuditStatus.IN_PROGRESS)
            ]
            for candidate in open_issues:
                cand_title = candidate.title.strip().lower()
                cand_symptom = (candidate.symptom or candidate.title).strip().lower()
                cand_target = _normalize_target_file_for_cmp(candidate.target_file)
                if cand_target is None:
                    continue
                cand_sym = (candidate.symbol or "").strip()
                if (
                    norm_title == cand_title
                    and norm_symptom == cand_symptom
                    and norm_target == cand_target
                    and norm_sym == cand_sym
                ):
                    raise DuplicateDebtError(candidate.id)

        today = datetime.now().strftime("%Y-%m-%d")

        issue_id = srv.generate_audit_id(clean_category, existing_ids=existing_ids)
        issue = AuditIssue(
            id=issue_id,
            title=clean_title,
            category=clean_category,
            priority=AuditPriority(clean_priority),
            autonomy=AuditAutonomy(clean_autonomy),
            complexity=AuditComplexity(clean_complexity),
            status=AuditStatus.PLANNED,
            blocked_by=clean_blocked_by,
            target_file=clean_target_file,
            symbol=clean_symbol,
            symptom=clean_symptom,
            root_cause=root_cause,
            solution=solution,
            task_id=clean_task,
            created_at=today,
            updated_at=today,
        )

        if clean_blocked_by:
            all_existing = store.list_all_issues()
            cycles = detect_dependency_cycles([*all_existing, issue])
            new_cycles = [c for c in cycles if issue.id in c]
            if new_cycles:
                cycle_str = " -> ".join(new_cycles[0])
                msg = f"Обнаружен цикл зависимостей в blocked_by: {cycle_str}"
                raise ValueError(msg)

        file_path = sync.save_and_index_card(issue)
        return {
            "status": "created",
            "id": issue.id,
            "file_path": str(file_path),
            "issue": issue.model_dump(),
        }


def update_debt_status(
    id: str,  # noqa: A002
    status: str,
    resolution_note: str | None = None,
    pr_url: str | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Обновляет статус задачи аудита (при 'completed' перемещает карточку в done/)."""
    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    try:
        new_status = AuditStatus(status.lower())
    except ValueError as exc:
        valid = ", ".join(s.value for s in AuditStatus)
        msg = f"Invalid status '{status}': expected one of {valid}"
        raise ValueError(msg) from exc
    updated_issue, new_path = sync.update_issue_status(
        issue_id=id,
        new_status=new_status,
        pr_url=pr_url,
        resolution_note=resolution_note,
    )
    return {
        "status": "updated",
        "id": updated_issue.id,
        "new_status": updated_issue.status.value,
        "file_path": str(new_path),
        "issue": updated_issue.model_dump(),
    }


def register_debt_tools(
    server: MCPServer,
    resolver: _AuditResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[AuditServiceBundle] | None = None,
) -> None:
    _reg: AuditToolReg = _make_reg(server, multi_tenant)

    @_reg(name="check_known_debt")
    @_tool_error_guard("check_known_debt", pool=pool)
    def check_known_debt_tool(
        symptom: str,
        file_path: str | None = None,
        symbol: str | None = None,
        category: str | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Check a symptom against known technical debt or regression entries for code review.

        Trigger: MANDATORY during code review — call BEFORE rendering a defect verdict (known debt does not block merge, regressions do).
        Example: check_known_debt(symptom="method > 50 lines", file_path="src/api.py", symbol="handler")
        """
        bundle, resolved, _ = resolver(project_path)
        resp = check_known_debt(
            symptom,
            file_path=file_path,
            symbol=symbol,
            category=category,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        resp["project_root"] = str(resolved.root)
        return resp

    @_reg(name="get_ready_backlog")
    @_tool_error_guard("get_ready_backlog", pool=pool)
    def get_ready_backlog_tool(
        category: str | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Return planned tasks whose blockers are all closed (completed or obsolete).

        Trigger: Call when selecting the next backlog task to work on (DAG blocked_by dependencies are resolved automatically).
        Example: get_ready_backlog(category="backend")
        """
        bundle, resolved, _ = resolver(project_path)
        resp = get_ready_backlog(
            category=category, synchronizer=bundle.synchronizer, storage=bundle.storage
        )
        resp["project_root"] = str(resolved.root)
        return resp

    @_reg(name="search_debt")
    @_tool_error_guard("search_debt", pool=pool)
    def search_debt_tool(
        query: str = "",
        category: str | None = None,
        status: Literal["all", "planned", "in_progress", "completed", "obsolete"] = "planned",
        limit: CoercedInt = 10,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Full-text search across audit and technical debt cards.

        Statuses: planned, in_progress, completed, obsolete; status="all" disables status filtering.
        Unknown statuses are rejected with a validation error rather than returning empty results.
        limit<=0 -> 10 (DEFAULT_LIMIT); allowed range is 1..100.
        Trigger: Call before recording new debt (to check for duplicates) and to review topic backlogs.
        Example: search_debt(query="race condition", status="planned")
        Response includes root visibility fields: project_root/root_source (warning emitted if project root is not anchored).
        """
        bundle, resolved, write_ok = resolver(project_path)
        result = search_debt(
            query=query,
            category=category,
            status=status,
            limit=limit,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        result["root_source"] = resolved.source
        if not write_ok:
            result["root_warning"] = (
                f"Корень проекта не закреплён (source={resolved.source}): результаты "
                "могут быть пустыми/неполными, запись отклоняется ROOT_NOT_ANCHORED. "
                "Проверьте MCP_PROJECT_ROOT / --project-root в mcp_config.json."
            )
        return result

    @_reg(name="record_debt")
    @_tool_error_guard("record_debt", pool=pool)
    def record_debt_tool(
        title: str,
        category: str | None = None,
        priority: Literal["critical", "important", "suggestion"] = "important",
        symptom: str = "",
        root_cause: str | None = None,
        solution: str | None = None,
        target_file: str | None = None,
        symbol: str | None = None,
        autonomy: Literal["high", "medium", "low"] = "high",
        complexity: Literal["L", "M", "H"] = "M",
        blocked_by: CoercedStrSeq = (),
        task_id: str | None = None,
        force: bool = False,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Create a new atomic technical debt card in .agents/audit/todo/<category>/.
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored (no MCP_PROJECT_ROOT/--project-root/root markers).
        Invalid priority/autonomy/complexity values are rejected with validation errors rather than falling back to defaults.

        Trigger: Call when discovering legacy defects outside the current task scope (or deferred by agreement), after check_known_debt.
        Example: record_debt(title="Remove DEVNULL", category="backend", symptom="subprocess hang", target_file="src/run.py")
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        clean_blocked = [str(x).strip() for x in blocked_by if x is not None and str(x).strip()]
        result = record_debt(
            title=title,
            category=category,
            priority=priority,
            symptom=symptom,
            root_cause=root_cause,
            solution=solution,
            target_file=target_file,
            symbol=symbol,
            autonomy=autonomy,
            complexity=complexity,
            blocked_by=clean_blocked if clean_blocked else None,
            task_id=task_id,
            force=force,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        return result

    @_reg(name="update_debt_status")
    @_tool_error_guard("update_debt_status", pool=pool)
    def update_debt_status_tool(
        id: str,  # noqa: A002
        status: Literal["planned", "in_progress", "completed", "obsolete"],
        resolution_note: str | None = None,
        pr_url: str | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Update audit task status (moves card to done/ upon 'completed').
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored (no MCP_PROJECT_ROOT/--project-root/root markers).
        Allowed statuses: planned, in_progress, completed, obsolete; invalid values trigger a validation error.

        Trigger: Call after completing a technical debt task with passing tests (status="completed" + pr_url).
        Example: update_debt_status(id="aud-backend-abc123", status="completed", pr_url="https://...")
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        result = update_debt_status(
            id=id,
            status=status,
            resolution_note=resolution_note,
            pr_url=pr_url,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        return result


_register_debt_tools = register_debt_tools
