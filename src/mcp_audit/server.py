"""MCP stdio сервер для MCP Audit Engine (`audit-engine`)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, NamedTuple, Protocol, cast

from mcp.server.mcpserver import MCPServer
from pydantic import BeforeValidator

from mcp_audit.dag import detect_dependency_cycles, get_ready_issues
from mcp_audit.help_docs import get_audit_help_text
from mcp_audit.installer import AUDIT_INSTRUCTIONS
from mcp_audit.models import (
    AuditAutonomy,
    AuditComplexity,
    AuditIssue,
    AuditPriority,
    AuditStatus,
    PlanArtifact,
    PlanStatus,
    PlanStep,
    ReviewArtifact,
    ReviewComment,
    ReviewVerdict,
    generate_audit_id,
    generate_plan_id,
    generate_review_id,
)
from mcp_audit.parser import parse_plan_markdown
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer, _advisory_lock
from mcp_core.error_envelope import (
    ErrorRule,
)
from mcp_core.error_envelope import (
    handle_tool_error as core_handle_tool_error,
)
from mcp_core.error_envelope import (
    pool_error_guard as core_pool_error_guard,
)
from mcp_core.paths import (
    InvalidProjectPathError,
    ResolvedRoot,
    RootNotAnchoredError,
    _find_default_project_root,
    ensure_root_anchored,
    get_cache_dir,
    is_anchored,
    log_root_banner,
    resolve_dynamic_project_root,
    resolve_factory_root,
)
from mcp_core.sanitizer import normalize_category_slug, sanitize_task_id
from mcp_core.service_pool import ProjectServicePool
from mcp_core.tool_contract import PROJECT_PATH_PARAM, adapt_project_path_signature

__all__ = [
    "DuplicateDebtError",
    "check_known_debt",
    "create_server",
    "get_audit_help",
    "get_ready_backlog",
    "get_storage",
    "get_synchronizer",
    "get_task_context",
    "record_debt",
    "record_plan",
    "record_review",
    "reset_audit_singletons",
    "run_server",
    "search_artifacts",
    "search_debt",
    "step_plan",
    "update_debt_status",
    "update_plan",
]

DEFAULT_LIMIT = 10


logger = logging.getLogger("mcp_audit.server")


class AuditServiceBundle(NamedTuple):
    storage: AuditStorage
    synchronizer: AuditSynchronizer
    audit_dir: Path
    cache_db: Path

    def close(self) -> None:
        self.storage.close()


class DuplicateDebtError(Exception):
    """Исключение при обнаружении дубликата задачи аудита."""

    def __init__(self, existing_id: str):
        super().__init__(f"Обнаружен дубликат карточки аудита: {existing_id}")
        self.existing_id = existing_id


# Доменные правила маппинга ошибок audit на единый error-envelope (раньше в
# mcp_audit error-обёртки не было ни у одного тула — MEDIUM-3). Порядок значим:
# subclass-исключения (RootNotAnchoredError) стоят раньше базовой ветки
# ValueError в движке core.
AUDIT_ERROR_RULES: tuple[ErrorRule, ...] = (
    ErrorRule(
        exc=DuplicateDebtError,
        error_type="DuplicateDebtError",
        reason="DUPLICATE_FOUND",
        log_template=None,
    ),
    ErrorRule(
        exc=InvalidProjectPathError,
        error_type="InvalidProjectPathError",
        reason="INVALID_PROJECT_PATH",
        log_template="Невалидный путь проекта (%s): %s",
        log_level="warning",
    ),
    ErrorRule(
        exc=RootNotAnchoredError,
        error_type="RootNotAnchoredError",
        reason="ROOT_NOT_ANCHORED",
        log_template="Отказ записи: корень проекта не закреплён (%s): %s",
        log_level="warning",
    ),
    ErrorRule(
        exc=FileNotFoundError,
        error_type="FileNotFoundError",
        reason="CARD_NOT_FOUND",
        log_template=None,
    ),
    ErrorRule(
        exc=FileExistsError,
        error_type="FileExistsError",
        reason="REVIEW_ID_COLLISION",
        log_template="Отказ записи: коллизия id ревью (%s): %s",
        log_level="warning",
    ),
)


def _tool_error_guard(
    context: str,
    pool: ProjectServicePool[AuditServiceBundle] | None = None,
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Декоратор tool-обёртки audit: маппит исключения на единый error-envelope.

    Домен пакета (``handle`` + ``AUDIT_ERROR_RULES``) поверх единого каркаса
    core :func:`mcp_core.error_envelope.pool_error_guard` (Ф2 плана
    core-dedup v4): при наличии пула core-каркас открывает scope аренды на
    весь вызов и освобождает его в finally. ``functools.wraps`` (внутри
    core-guard) сохраняет сигнатуру, аннотации и docstring, поэтому
    JSON-схема инструмента, строимая SDK, не меняется. Базовая ветка
    ``ValueError`` движка core мапится в ``INVALID_ARGUMENTS`` (например,
    невалидный литерал AuditStatus).
    """

    def handle(e: Exception) -> dict[str, Any]:
        return core_handle_tool_error(e, context, rules=AUDIT_ERROR_RULES, logger=logger)

    return cast(
        "Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]",
        core_pool_error_guard(handle, pool),
    )


def _coerce_json_list(v: Any) -> list[str]:
    """Коэрция JSON-строки или последовательности в список строк (обход бага MCP-мостов)."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("(") and s.endswith(")")):
            with contextlib.suppress(Exception):
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
        return [v] if v else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return []


def _coerce_plan_steps(v: Any) -> list[str]:
    """Принимает JSON-массив или отдельные строки текста как шаги плана."""
    if isinstance(v, str):
        s = v.strip()
        if not (s.startswith("[") and s.endswith("]")):
            lines = [line.strip() for line in s.splitlines() if line.strip()]
            if len(lines) > 1:
                return lines
    return _coerce_json_list(v)


def _coerce_review_comments(v: Any) -> list[ReviewComment]:
    """Принимает массив комментариев или одиночный объект без потери данных."""
    if v is None:
        return []
    raw_items: list[Any] = []
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError as exc:
            raise ValueError("comments должен быть JSON-объектом или массивом") from exc
        if isinstance(parsed, dict):
            raw_items = [parsed]
        elif isinstance(parsed, list):
            raw_items = parsed
        else:
            raise ValueError("comments должен быть JSON-объектом или массивом")
    elif isinstance(v, dict):
        raw_items = [v]
    elif isinstance(v, (list, tuple)):
        raw_items = list(v)
    else:
        raise ValueError("comments должен быть объектом или массивом")

    res: list[ReviewComment] = []
    for item in raw_items:
        if isinstance(item, ReviewComment):
            res.append(item)
        elif isinstance(item, dict):
            res.append(ReviewComment.model_validate(item))
        else:
            raise ValueError("Каждый элемент comments должен быть объектом")
    return res


def _coerce_json_int(v: Any) -> int:
    """Коэрция строки в целое число для обхода бага MCP-клиентов/мостов."""
    if v is None:
        return 0
    if isinstance(v, str):
        s = v.strip()
        with contextlib.suppress(Exception):
            return int(s)
    if isinstance(v, int):
        return v
    return 0


CoercedStrSeq = Annotated[Sequence[str], BeforeValidator(_coerce_json_list)]
CoercedPlanSteps = Annotated[Sequence[str], BeforeValidator(_coerce_plan_steps)]
CoercedReviewCommentSeq = Annotated[
    Sequence[ReviewComment], BeforeValidator(_coerce_review_comments)
]
CoercedInt = Annotated[int, BeforeValidator(_coerce_json_int)]


_STORAGE: AuditStorage | None = None
_STORAGE_ROOT: Path | None = None
_SYNCHRONIZER: AuditSynchronizer | None = None
_SYNCHRONIZER_ROOT: Path | None = None


def reset_audit_singletons() -> None:
    """Сбрасывает кэш глобальных экземпляров storage и synchronizer."""
    global _STORAGE, _STORAGE_ROOT, _SYNCHRONIZER, _SYNCHRONIZER_ROOT
    _STORAGE = None
    _STORAGE_ROOT = None
    _SYNCHRONIZER = None
    _SYNCHRONIZER_ROOT = None


def get_storage(project_root: Path | None = None, db_path: Path | None = None) -> AuditStorage:
    """Возвращает экземпляр AuditStorage с ленивой инициализацией."""
    global _STORAGE, _STORAGE_ROOT
    if project_root is None and db_path is None and _STORAGE is not None:
        return _STORAGE

    root = (project_root or _find_default_project_root()).resolve()
    if _STORAGE is None or root != _STORAGE_ROOT or db_path is not None:
        cache_dir = get_cache_dir(root)
        target_db = db_path or (cache_dir / "audit_fts.db")
        _STORAGE = AuditStorage(target_db)
        _STORAGE_ROOT = root
    return _STORAGE


def get_synchronizer(
    project_root: Path | None = None,
    storage: AuditStorage | None = None,
    audit_dir: Path | None = None,
) -> AuditSynchronizer:
    global _SYNCHRONIZER, _SYNCHRONIZER_ROOT
    if storage is not None:
        root = (project_root or _find_default_project_root()).resolve()
        target_audit = audit_dir or (root / ".agents" / "audit")
        return AuditSynchronizer(storage, audit_dir=target_audit)
    if project_root is None and audit_dir is None and _SYNCHRONIZER is not None:
        return _SYNCHRONIZER

    root = (project_root or _find_default_project_root()).resolve()
    if _SYNCHRONIZER is None or root != _SYNCHRONIZER_ROOT or audit_dir is not None:
        store = storage or get_storage(root)
        target_audit = audit_dir or (root / ".agents" / "audit")
        _SYNCHRONIZER = AuditSynchronizer(store, audit_dir=target_audit)
        _SYNCHRONIZER_ROOT = root
    return _SYNCHRONIZER


def _get_synced_storage(
    synchronizer: AuditSynchronizer | None,
    storage: AuditStorage | None,
) -> tuple[AuditSynchronizer, AuditStorage]:
    """Возвращает (sync, store) из переданных аргументов или глобальных геттеров с синхронизацией."""
    sync = synchronizer or get_synchronizer()
    sync.sync()
    store = storage or get_storage()
    return sync, store


def get_audit_help(tool_name: str | None = "overview") -> str:
    """Возвращает справку по инструменту audit-engine или общий обзор."""
    return get_audit_help_text(tool_name)


def check_known_debt(
    symptom: str,
    file_path: str | None = None,
    symbol: str | None = None,
    category: str | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Проверяет симптом на наличие известного техдолга или регрессии для скилла code-review."""
    _sync, store = _get_synced_storage(synchronizer, storage)
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
    _sync, store = _get_synced_storage(synchronizer, storage)
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
    _sync, store = _get_synced_storage(synchronizer, storage)
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
        # Явное отсутствие пути — participates: пусто совпадает только с пустым.
        return ""
    try:
        normalized = _validate_target_file(target_file)
    except ValueError:
        # Невалидный легаси-путь: карточка исключается из сравнения.
        return None
    return normalized or ""


def _validate_target_file(target_file: str | None) -> str | None:
    """Валидирует относительный проектный путь target_file.

    Ревью шага 3: путь канонизируется к POSIX-форме и нормализуется —
    Windows-разделители (``..\\\\..\\\\etc\\\\passwd``), префикс ``./`` и
    внутрипутевые ``foo/../`` обрабатываются одинаково на всех платформах,
    а не только там, где ``os.sep`` совпал с разделителем строки. Это и
    защита контракта метаданных: ``src/x.py``, ``./src/x.py`` и
    ``foo/../src/x.py`` — одна карточка, а не три.
    """
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

    # Канонизация: Windows-разделители → POSIX, нормализация точек-сегментов.
    # PurePosixPath не зависит от ОС, поэтому поведение одинаково на POSIX и Windows.
    posix_cleaned = cleaned.replace("\\", "/")
    posix_path = PurePosixPath(posix_cleaned)

    # Нормализованный относительный путь без '.'-сегментов; '..' допустим
    # только если он погашается предыдущим сегментом внутри корня проекта.
    # Примеры: './src/x.py' → 'src/x.py', 'foo/../src/x.py' → 'src/x.py',
    # но '../etc/passwd' и '..\..\etc\passwd' выходят за корень — отказ.
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

    # Enum-параметры отвергаются при неверном значении, а не подменяются default
    # (молчаливый default маскировал ошибку вызывающего — п. D плана integrity).
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

    sync = synchronizer or get_synchronizer(storage=storage)
    store = storage or get_storage()

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
                # Ревью r2: target_file кандидата нормализуется тем же
                # правилом, что и вход — иначе легаси-карточка с «./src/x.py»
                # не блокирует новую «src/x.py», и создаётся дубль.
                # Ревью r4 (п. 4): None = невалидный легаси-путь — кандидат
                # пропускается, не «пустой == пустой».
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

        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")

        issue_id = generate_audit_id(clean_category, existing_ids=existing_ids)
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
    id: str,
    status: str,
    resolution_note: str | None = None,
    pr_url: str | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Обновляет статус задачи аудита (при 'completed' перемещает карточку в done/)."""
    sync = synchronizer or get_synchronizer(storage=storage)
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


def record_plan(
    task_id: str,
    title: str,
    steps: list[str],
    target_files: list[str] | None = None,
    category: str = "general",
    pr_url: str | None = None,
    details: str = "",
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Создаёт новый план разработки в .agents/plans/active/<category>/."""
    sync = synchronizer or get_synchronizer(storage=storage)
    clean_cat = normalize_category_slug(category) if category else "general"
    clean_task = sanitize_task_id(task_id)

    plan_steps: list[PlanStep] = []
    for i, s_text in enumerate(steps, 1):
        clean_text = s_text.strip()
        if clean_text.startswith(("- [ ]", "- [x]", "* [ ]", "* [x]")):
            clean_text = clean_text[5:].strip()
        plan_steps.append(PlanStep(index=i, title=clean_text, completed=False))

    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")

    # Санитируем title: запрещаем/нормализуем переводы строк, чтобы
    # заголовок плана не генерировал многострочные секции или block scalar во frontmatter
    clean_title = " ".join(title.split()).strip() if title else clean_task

    # Экранируем возможные H2 заголовки в details (включая отступные 0-3 пробела/таб),
    # чтобы они не перехватывали структуру секций Markdown-плана (## -> ###)
    safe_details = details
    if safe_details and "## " in safe_details:
        import re

        safe_details = re.sub(r"(?m)^[ \t]{0,3}##\s+", "### ", safe_details)

    plan = PlanArtifact(
        id=generate_plan_id(clean_cat, clean_task),
        task_id=clean_task,
        title=clean_title,
        category=clean_cat,
        status=PlanStatus.DISCUSSED,
        pr_url=pr_url,
        target_files=target_files or [],
        steps=plan_steps,
        content=safe_details,
        created_at=today,
        updated_at=today,
    )

    # AUDIT-ARCH-2026-10 (шаг 3): создание планов идёт через PlanRepository —
    # единственный публичный контракт (get_unique/create_unique). Поведение
    # прежнее: PlanExistsError / AmbiguousPlanError — ValueError с «PLAN_EXISTS»
    # в тексте, как ждут клиенты и тесты; task_id-лок и TOCTOU-защита — внутри
    # save_plan (см. A2-TOCTOU).
    from mcp_audit.repository import PlanRepository

    path = PlanRepository(sync).create_unique(plan)
    return {
        "status": "created",
        "id": plan.id,
        "task_id": plan.task_id,
        "plan_status": plan.status.value,
        "file_path": str(path),
        "steps_count": len(plan.steps),
    }


def update_plan(
    task_id: str,
    title: str | None = None,
    add_steps: list[str] | None = None,
    target_files: list[str] | None = None,
    status: str | None = None,
    pr_url: str | None = None,
    category: str | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Обновляет метаданные плана (согласование approved, отмена cancelled, шаги).

    A13: при нескольких планах одного task_id укажите category — иначе
    AMBIGUOUS_PLAN.
    """
    sync = synchronizer or get_synchronizer(storage=storage)
    plan_status = PlanStatus(status.lower()) if status else None
    plan, path = sync.update_plan_details(
        task_id=task_id,
        title=title,
        status=plan_status,
        pr_url=pr_url,
        target_files=target_files,
        add_steps=add_steps,
        category=category,
    )
    return {
        "status": "updated",
        "id": plan.id,
        "task_id": plan.task_id,
        "plan_status": plan.status.value,
        "file_path": str(path),
    }


def step_plan(
    task_id: str,
    step: int,
    status: str = "done",
    note: str | None = None,
    allow_out_of_order: bool = False,
    category: str | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Точечно закрывает или переключает чекбокс шага в плане с фиксацией заметки.

    A13: при нескольких планах одного task_id укажите category — иначе
    AMBIGUOUS_PLAN.
    """
    sync = synchronizer or get_synchronizer(storage=storage)
    resp = sync.advance_plan_step(
        task_id=task_id,
        step=step,
        status=status,
        note=note,
        allow_out_of_order=allow_out_of_order,
        category=category,
    )
    return resp.model_dump()


def _determine_review_round(
    sync: AuditSynchronizer,
    clean_cat: str,
    clean_task: str | None,
    pr_url: str | None,
    requested_round: int | None,
) -> int:
    """Вычисляет номер раунда ревью с проверкой контекста и сканированием диска."""
    if requested_round is not None and requested_round > 0:
        return requested_round

    max_round = 0
    if clean_task or pr_url:
        sync.sync_artifacts()
        ctx = sync.storage.get_task_context(task_id=clean_task, pr_url=pr_url, view="summary")
        max_round = len(ctx.reviews) if (ctx.found and ctx.reviews) else 0

    rev_dir = sync._review_target_dir(clean_cat)
    if rev_dir.exists():
        import re

        pattern = (
            rf"^rev-{re.escape(clean_cat)}-{re.escape(clean_task)}(?:-pr\d+)?-r(\d+)\.md$"
            if clean_task
            else rf"^rev-{re.escape(clean_cat)}(?:-pr\d+)?-r(\d+)\.md$"
        )
        for p in rev_dir.glob("rev-*.md"):
            rm = re.match(pattern, p.name)
            if rm:
                max_round = max(max_round, int(rm.group(1)))

    return max_round + 1


def record_review(
    task_id: str | None = None,
    pr_url: str | None = None,
    verdict: Literal["approved", "changes_requested", "commented"] = "approved",
    summary: str = "",
    comments: list[ReviewComment] | None = None,
    round: int | None = None,
    category: str = "general",
    spawned_debts: list[str] | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Сохраняет артефакт код-ревью в .agents/reviews/<category>/."""
    sync = synchronizer or get_synchronizer(storage=storage)
    clean_cat = normalize_category_slug(category) if category else "general"
    clean_task = sanitize_task_id(task_id) if task_id else None

    eff_round = _determine_review_round(sync, clean_cat, clean_task, pr_url, round)

    rev_verdict = ReviewVerdict(verdict.lower())
    review_comments: list[ReviewComment] = comments or []

    pr_num = None
    if pr_url:
        import re

        m = re.search(r"/pull/(\d+)", pr_url)
        if m:
            pr_num = m.group(1)

    review_id = generate_review_id(
        category=clean_cat,
        task_id=clean_task,
        round=eff_round,
        pr_number=pr_num,
    )

    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")

    review = ReviewArtifact(
        id=review_id,
        task_id=clean_task,
        category=clean_cat,
        round=eff_round,
        verdict=rev_verdict,
        summary=summary,
        comments=review_comments,
        pr_url=pr_url,
        spawned_debts=spawned_debts or [],
        created_at=today,
    )

    path = sync.save_review(review)

    # State machine side-effect: если есть связанный план, обновляем его статус
    if clean_task:
        try:
            # aud-backend-3712: lookup через PlanRepository — единый контракт
            from mcp_audit.repository import PlanRepository

            plan_file = PlanRepository(sync).get_unique(clean_task, category=clean_cat)
            if plan_file and plan_file.exists():
                curr_plan = parse_plan_markdown(
                    plan_file.read_text(encoding="utf-8"), file_path=plan_file
                )
                all_done = bool(curr_plan.steps and all(s.completed for s in curr_plan.steps))
                if rev_verdict == ReviewVerdict.APPROVED:
                    if all_done:
                        sync.update_plan_details(task_id=clean_task, status=PlanStatus.DONE)
                    elif curr_plan.status == PlanStatus.DISCUSSED:
                        sync.update_plan_details(task_id=clean_task, status=PlanStatus.APPROVED)
                elif rev_verdict == ReviewVerdict.CHANGES_REQUESTED and curr_plan.status in (
                    PlanStatus.APPROVED,
                    PlanStatus.DONE,
                ):
                    sync.update_plan_details(task_id=clean_task, status=PlanStatus.IN_PROGRESS)
        except Exception as err:
            logger.warning("Ошибка обновления статуса плана для задачи '%s': %s", clean_task, err)

    return {
        "status": "created",
        "id": review.id,
        "task_id": review.task_id,
        "verdict": review.verdict.value,
        "round": review.round,
        "file_path": str(path),
    }


def get_task_context(
    task_id: str | None = None,
    pr_url: str | None = None,
    view: str = "summary",
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Возвращает агрегированный контекст задачи (план, ревью, долги)."""
    sync = synchronizer or get_synchronizer(storage=storage)
    sync.sync_all()
    store = storage or get_storage()
    # A6: читающий путь тоже санитизирует task_id (' CTX-1 ' → 'CTX-1'),
    # иначе тихий found:false при пробелах/регистре
    clean_task = sanitize_task_id(task_id) if task_id else None
    resp = store.get_task_context(task_id=clean_task, pr_url=pr_url, view=view)
    return resp.model_dump()


def search_artifacts(
    query: str = "",
    artifact_type: str = "all",
    category: str | None = None,
    status: str = "all",
    limit: int = 10,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Полнотекстовый и фасетный поиск по планам, ревью и карточкам техдолга.

    Статусы по типам артефактов (применяются только к совместимым типам):
    - plan: discussed, approved, in_progress, done, cancelled;
    - debt: planned, in_progress, completed, obsolete;
    - review (verdict): approved, changes_requested, commented.
    artifact_type='audit' — входной alias 'debt' (канонический ответ — 'debt').
    status='all' отключает фильтр. Полностью неизвестный статус — ошибка.
    """
    sync = synchronizer or get_synchronizer(storage=storage)
    sync.sync_all()
    store = storage or get_storage()
    # Входной alias: 'audit' -> канонический 'debt' (совместимость со старыми клиентами)
    eff_type = "debt" if artifact_type == "audit" else artifact_type
    results = store.search_artifacts(
        query=query,
        artifact_type=eff_type,
        category=category,
        status=status,
        limit=limit,
    )
    return {
        "count": len(results),
        "query": query,
        "artifact_type": eff_type,
        "category": category,
        "status": status,
        "results": results,
    }


_AuditResolver = Callable[[str | None], tuple[AuditServiceBundle, ResolvedRoot, bool]]


class AuditToolReg(Protocol):
    """Декоратор регистрации тула с точной сигнатурой (ревью Ф2-3).

    Сохраняет имя параметра ``name`` и значение по умолчанию —
    ``Callable``-аннотация теряла их (mypy: call-arg на 12 вызовах).
    """

    def __call__(
        self, name: str | None = ...
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


def _make_reg(server: MCPServer, multi_tenant: bool) -> AuditToolReg:
    """Фабрика декоратора регистрации тула (Ф2 плана core-dedup v4).

    Поглощает три побайтово одинаковые локальные копии ``_reg`` из
    ``_register_*_tools``: адаптация project_path-сигнатуры под
    multi-tenant-режим + регистрация через ``server.tool(name=name)``.
    """

    def _reg(name: str | None = None) -> Callable[[Any], Any]:
        def dec(fn: Any) -> Any:
            adapted = adapt_project_path_signature(fn, multi_tenant)
            return server.tool(name=name)(adapted)

        return dec

    return _reg


def _register_debt_tools(
    server: MCPServer,
    resolver: _AuditResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[AuditServiceBundle] | None = None,
) -> None:
    _reg = _make_reg(server, multi_tenant)

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
        limit: int = 10,
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
        clean_blocked = [str(x) for x in blocked_by if str(x).strip()]
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
        id: str,
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


def _register_plan_tools(
    server: MCPServer,
    resolver: _AuditResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[AuditServiceBundle] | None = None,
) -> None:
    _reg = _make_reg(server, multi_tenant)

    @_reg(name="record_plan")
    @_tool_error_guard("record_plan", pool=pool)
    def record_plan_tool(
        task_id: str,
        title: str,
        steps: CoercedPlanSteps,
        target_files: CoercedStrSeq = (),
        category: str = "general",
        pr_url: str | None = None,
        details: str = "",
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Create a new feature or refactoring implementation plan in .agents/plans/active/<category>/.
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored.
        A2: task_id is globally unique — if a plan already exists (in any category),
        rejects with PLAN_EXISTS; edits should be performed via update_plan/step_plan.

        Trigger: Call when starting work on a feature/refactoring to establish goals and stages (creates plan in discussed status).
        Example: record_plan(task_id="PROJ-142", title="OAuth2", steps=["1. TokenProvider", "2. Middleware"], target_files=["src/auth.py"])
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        clean_target = [str(x) for x in target_files if str(x).strip()]
        result = record_plan(
            task_id=task_id,
            title=title,
            steps=list(steps),
            target_files=clean_target if clean_target else None,
            category=category,
            pr_url=pr_url,
            details=details,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        return result

    @_reg(name="update_plan")
    @_tool_error_guard("update_plan", pool=pool)
    def update_plan_tool(
        task_id: str,
        title: str | None = None,
        add_steps: CoercedPlanSteps = (),
        target_files: CoercedStrSeq = (),
        status: Literal["discussed", "approved", "in_progress", "done", "cancelled"] | None = None,
        pr_url: str | None = None,
        category: str | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Update plan status (approved, in_progress, done, cancelled), title, or PR URL.
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored.
        A13: when the same task_id exists in multiple categories without category specified — rejects with AMBIGUOUS_PLAN;
        with category specified — selects the exact plan.

        Trigger: Call to approve a plan (status="approved"), attach a PR URL, or cancel.
        Example: update_plan(task_id="PROJ-142", status="approved", pr_url="https://github.com/org/repo/pull/85")
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        clean_steps = [str(x) for x in add_steps if str(x).strip()]
        clean_target = [str(x) for x in target_files if str(x).strip()]
        result = update_plan(
            task_id=task_id,
            title=title,
            add_steps=clean_steps if clean_steps else None,
            target_files=clean_target if clean_target else None,
            status=status,
            pr_url=pr_url,
            category=category,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        return result

    @_reg(name="step_plan")
    @_tool_error_guard("step_plan", pool=pool)
    def step_plan_tool(
        task_id: str,
        step: int,
        status: Literal["done", "open"] = "done",
        note: str | None = None,
        allow_out_of_order: bool = False,
        category: str | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Mark a plan step completed (checks [x] and records log with date/note) without manual Markdown editing.
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored.
        A13: when the same task_id exists in multiple categories without category specified — rejects with AMBIGUOUS_PLAN.

        Trigger: MANDATORY when completing a plan step INSTEAD OF manually editing Markdown files (checks [x] and logs verification note).
        Example: step_plan(task_id="PROJ-142", step=1, note="Unit tests passing")
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        result = step_plan(
            task_id=task_id,
            step=step,
            status=status,
            note=note,
            allow_out_of_order=allow_out_of_order,
            category=category,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        return result


def _register_review_and_context_tools(
    server: MCPServer,
    resolver: _AuditResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[AuditServiceBundle] | None = None,
) -> None:
    _reg = _make_reg(server, multi_tenant)

    @_reg(name="record_review")
    @_tool_error_guard("record_review", pool=pool)
    def record_review_tool(
        task_id: str | None = None,
        pr_url: str | None = None,
        verdict: Literal["approved", "changes_requested", "commented"] = "approved",
        summary: str = "",
        comments: CoercedReviewCommentSeq = (),
        round: CoercedInt = 0,
        category: str = "general",
        spawned_debts: CoercedStrSeq = (),
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Save a code review round to .agents/reviews/<category>/ with a verdict and comments list.
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored.

        Strict schema for comments: list of ReviewComment objects with fields
        file_path (required), symptom (required), line, status
        (open/resolved/deferred); unexpected fields are rejected by validation.

        Trigger: MANDATORY upon concluding code review for a PR or hotfix (works with an existing plan or standalone via pr_url).
        Example: record_review(pr_url="https://github.com/org/repo/pull/85", verdict="approved", summary="All clean")
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        # Конвертация dict -> ReviewComment выполняется Pydantic на границе схемы;
        # доменная функция получает уже строго типизированные ReviewComment.
        typed_comments = (
            [
                c if isinstance(c, ReviewComment) else ReviewComment.model_validate(c)
                for c in comments
            ]
            if comments
            else None
        )
        r_val = round if round > 0 else None
        clean_debts = [str(x) for x in spawned_debts if str(x).strip()]
        debts = clean_debts if clean_debts else None
        result = record_review(
            task_id=task_id,
            pr_url=pr_url,
            verdict=verdict,
            summary=summary,
            comments=typed_comments,
            round=r_val,
            category=category,
            spawned_debts=debts,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        return result

    @_reg(name="get_task_context")
    @_tool_error_guard("get_task_context", pool=pool)
    def get_task_context_tool(
        task_id: str | None = None,
        pr_url: str | None = None,
        view: Literal["summary", "full"] = "summary",
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Return aggregated task context: active plan, code review history, and associated technical debt.

        Trigger: MANDATORY before starting work on a task or continuing across sessions (call BEFORE reading code).
        Example: get_task_context(task_id="PROJ-142", view="summary")
        """
        bundle, resolved, _ = resolver(project_path)
        result = get_task_context(
            task_id=task_id,
            pr_url=pr_url,
            view=view,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        result["root_source"] = resolved.source
        return result

    @_reg(name="search_artifacts")
    @_tool_error_guard("search_artifacts", pool=pool)
    def search_artifacts_tool(
        query: str = "",
        artifact_type: Literal["all", "plan", "review", "debt", "audit"] = "all",
        category: str | None = None,
        status: str = "all",
        limit: int = 10,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Full-text and faceted search across plans, reviews, and technical debt cards.

        artifact_type: 'plan', 'review', 'debt', or 'all'; 'audit' is an alias for 'debt' (canonical 'debt' returned in response).
        Statuses by type (applied only to compatible types):
        plan: discussed, approved, in_progress, done, cancelled;
        debt: planned, in_progress, completed, obsolete;
        review (verdict): approved, changes_requested, commented.
        status='all' disables status filtering; unrecognized statuses trigger an error.
        When query is provided and artifact_type='all', ranks from both sources are fused via RRF; empty query defaults to chronological sorting.

        limit<=0 -> 10 (DEFAULT_LIMIT); allowed range is 1..100.
        Trigger: Call to search plans and reviews by keyword or review active backlog tasks (status="in_progress").
        Example: search_artifacts(query="OAuth2", artifact_type="plan")
        """
        bundle, resolved, _ = resolver(project_path)
        result = search_artifacts(
            query=query,
            artifact_type=artifact_type,
            category=category,
            status=status,
            limit=limit,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        result["root_source"] = resolved.source
        return result

    # Справка по образцу knowledge: без project_path и без привязки к проекту
    # (is_project=False) — конвертация dict-аргументов здесь не нужна.
    @server.tool(name="get_help")
    @_tool_error_guard("get_help")
    def get_help(tool_name: str | None = "overview") -> Any:
        """Contextual help for audit-engine tools: purpose, when to call, key params and statuses.

        Trigger: Call when uncertain about argument formats, allowed statuses, or artifact_type compatibility.
        Example: get_help(tool_name="record_debt")
        """
        return get_audit_help_text(tool_name)


def create_server(
    project_root: Path | None = None,
    audit_dir: Path | None = None,
    cache_db: Path | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
    multi_tenant: bool = False,
) -> MCPServer:
    """Фабрика для создания и настройки MCP сервера audit-engine.

    Root-guard (P1): ``write_ok`` (флаг разрешения записи write_ok) — передан
    явный ``audit_dir`` ИЛИ корень закреплён (env / walk-up / закреплённый
    ``--project-root``) ИЛИ инжектированы ``storage``/``synchronizer`` (моки
    тестов). ``cache_db`` разрешением записи НЕ считается: он пиннит только эфемерный
    FTS-кэш, карточки пишутся в audit_dir.
    """
    default_resolved = resolve_factory_root(project_root) if not multi_tenant else None

    if storage is not None or synchronizer is not None:
        def_root = default_resolved.root if default_resolved is not None else Path.cwd()
        target_audit = audit_dir or (def_root / ".agents" / "audit")
        target_db = cache_db or (get_cache_dir(def_root) / "audit_fts.db")
        store = storage or AuditStorage(db_path=target_db)
        sync = synchronizer or AuditSynchronizer(storage=store, audit_dir=target_audit)
        default_bundle = AuditServiceBundle(
            storage=store,
            synchronizer=sync,
            audit_dir=target_audit,
            cache_db=target_db,
        )
        pool: ProjectServicePool[AuditServiceBundle] | None = None
        write_ok_default = True
    else:

        def _factory(r: Path) -> AuditServiceBundle:
            if default_resolved is not None and r == default_resolved.root:
                a_dir = audit_dir or (r / ".agents" / "audit")
                c_db = cache_db or (get_cache_dir(r) / "audit_fts.db")
            else:
                a_dir = r / ".agents" / "audit"
                c_db = get_cache_dir(r) / "audit_fts.db"

            is_project = is_anchored(r) or (r / ".agents").exists()
            if is_project:
                a_dir.mkdir(parents=True, exist_ok=True)
                c_db.parent.mkdir(parents=True, exist_ok=True)
                store = AuditStorage(db_path=c_db)
                sync = AuditSynchronizer(storage=store, audit_dir=a_dir, create_dirs=True)
            else:
                import hashlib

                h = hashlib.sha256(str(r).encode()).hexdigest()[:12]
                temp_db = Path(tempfile.gettempdir()) / f"mcp_audit_unanchored_{h}.db"
                store = AuditStorage(db_path=temp_db)
                sync = AuditSynchronizer(storage=store, audit_dir=a_dir, create_dirs=False)

            return AuditServiceBundle(
                storage=store,
                synchronizer=sync,
                audit_dir=a_dir,
                cache_db=c_db,
            )

        pool = ProjectServicePool(_factory)
        if not multi_tenant and default_resolved is not None:
            default_bundle = pool.get(default_resolved.root)
            default_bundle.synchronizer.sync()
            write_ok_default = bool(audit_dir) or default_resolved.anchored
        else:
            default_bundle = None
            write_ok_default = False

    if default_resolved is not None:
        log_root_banner("audit-engine", default_resolved, write_ok_default)

    def resolver(project_path: str | None) -> tuple[AuditServiceBundle, ResolvedRoot, bool]:
        res = resolve_dynamic_project_root(
            project_path=project_path,
            multi_tenant=multi_tenant,
            default_root=default_resolved,
        )
        if pool is not None:
            # Scoped lease: released in _tool_error_guard finally via release_scope.
            bundle = pool.scoped_get(res.root)
            effective_write_ok = res.anchored if project_path is not None else write_ok_default
        else:
            if default_bundle is None:
                raise RuntimeError("Default audit bundle is not initialized")
            bundle = default_bundle
            effective_write_ok = True
        return bundle, res, effective_write_ok

    server = MCPServer("audit-engine", instructions=AUDIT_INSTRUCTIONS)
    _register_debt_tools(server, resolver, multi_tenant=multi_tenant, pool=pool)
    _register_plan_tools(server, resolver, multi_tenant=multi_tenant, pool=pool)
    _register_review_and_context_tools(server, resolver, multi_tenant=multi_tenant, pool=pool)
    return server


def run_server() -> None:
    """Точка входа запуска MCP stdio сервера аудита."""
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        from mcp_audit.installer import run_installer

        run_installer(sys.argv[2:])
        return

    import argparse

    parser = argparse.ArgumentParser(description="MCP Audit Engine stdio server")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Путь к корню целевого проекта",
    )
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=None,
        help="Путь к каталогу Markdown-карточек аудита",
    )
    parser.add_argument(
        "--cache-db",
        type=Path,
        default=None,
        help="Путь к файлу кэша SQLite FTS5",
    )
    parser.add_argument(
        "--multi-tenant",
        action="store_true",
        default=False,
        help="Режим multi-tenant: отключить дефолтный корень, требовать project_path",
    )
    args = parser.parse_args()

    server = create_server(
        project_root=args.project_root,
        audit_dir=args.audit_dir,
        cache_db=args.cache_db,
        multi_tenant=args.multi_tenant,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    run_server()
