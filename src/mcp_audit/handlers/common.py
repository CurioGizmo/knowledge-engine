"""Общие типы, коэрция, синглтоны и хелперы для обработчиков MCP Audit Engine."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, NamedTuple, Protocol, cast

from mcp.server.mcpserver import MCPServer
from pydantic import BeforeValidator

from mcp_audit.help_docs import get_audit_help_text
from mcp_audit.models import ReviewComment
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer
from mcp_core.error_envelope import ErrorRule
from mcp_core.error_envelope import handle_tool_error as core_handle_tool_error
from mcp_core.error_envelope import pool_error_guard as core_pool_error_guard
from mcp_core.paths import (
    InvalidProjectPathError,
    ResolvedRoot,
    RootNotAnchoredError,
)
from mcp_core.service_pool import ProjectServicePool
from mcp_core.tool_contract import adapt_project_path_signature

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
    """Декоратор tool-обёртки audit: маппит исключения на единый error-envelope."""

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
                    return [str(x) for x in parsed if x is not None]
        return [v] if v else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x is not None]
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
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, str):
        s = v.strip()
        with contextlib.suppress(Exception):
            return int(s)
    if isinstance(v, int):
        return v
    return 0


def _coerce_json_optional_int(v: Any) -> int | None:
    """Коэрция строки в целое число с сохранением None для опциональных полей."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        with contextlib.suppress(Exception):
            return int(s)
    if isinstance(v, int):
        return v
    return None


CoercedStrSeq = Annotated[Sequence[str], BeforeValidator(_coerce_json_list)]
CoercedPlanSteps = Annotated[Sequence[str], BeforeValidator(_coerce_plan_steps)]
CoercedReviewCommentSeq = Annotated[
    Sequence[ReviewComment], BeforeValidator(_coerce_review_comments)
]
CoercedInt = Annotated[int, BeforeValidator(_coerce_json_int)]
CoercedOptionalInt = Annotated[int | None, BeforeValidator(_coerce_json_optional_int)]


def reset_audit_singletons() -> None:
    """Сбрасывает кэш глобальных экземпляров storage и synchronizer в mcp_audit.server."""
    import mcp_audit.server as srv

    srv.reset_audit_singletons()


def get_storage(project_root: Path | None = None, db_path: Path | None = None) -> AuditStorage:
    """Возвращает экземпляр AuditStorage из канонического mcp_audit.server."""
    import mcp_audit.server as srv

    return srv.get_storage(project_root=project_root, db_path=db_path)


def get_synchronizer(
    project_root: Path | None = None,
    storage: AuditStorage | None = None,
    audit_dir: Path | None = None,
) -> AuditSynchronizer:
    """Возвращает экземпляр AuditSynchronizer из канонического mcp_audit.server."""
    import mcp_audit.server as srv

    return srv.get_synchronizer(project_root=project_root, storage=storage, audit_dir=audit_dir)


def _get_synced_storage(
    synchronizer: AuditSynchronizer | None,
    storage: AuditStorage | None,
) -> tuple[AuditSynchronizer, AuditStorage]:
    """Возвращает (sync, store) из переданных аргументов или глобальных геттеров с синхронизацией."""
    import mcp_audit.server as srv

    return srv._get_synced_storage(synchronizer, storage)


def __getattr__(name: str) -> Any:
    if name in {"_STORAGE", "_STORAGE_ROOT", "_SYNCHRONIZER", "_SYNCHRONIZER_ROOT"}:
        import mcp_audit.server as srv

        return getattr(srv, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_audit_help(tool_name: str | None = "overview") -> str:
    """Возвращает справку по инструменту audit-engine или общий обзор."""
    return get_audit_help_text(tool_name)


_AuditResolver = Callable[[str | None], tuple[AuditServiceBundle, ResolvedRoot, bool]]


class AuditToolReg(Protocol):
    """Декоратор регистрации тула с точной сигнатурой."""

    def __call__(
        self, name: str | None = ...
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


def _make_reg(server: MCPServer, multi_tenant: bool) -> AuditToolReg:
    """Фабрика декоратора регистрации тула."""

    def _reg(name: str | None = None) -> Callable[[Any], Any]:
        def dec(fn: Any) -> Any:
            adapted = adapt_project_path_signature(fn, multi_tenant)
            return server.tool(name=name)(adapted)

        return dec

    return _reg
