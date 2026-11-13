"""MCP stdio сервер для управления долгосрочной памятью AI-агентов (MCP Knowledge Engine).

Предоставляет 8 нативных инструментов:
1. search_knowledge - токен-эффективный поиск знаний, выборка по категориям и обзор тем проекта (Overview).
2. get_knowledge - точечное чтение полных карточек.
3. record_knowledge - регистрация новых знаний с проверкой Anti-Spam Gate.
4. update_knowledge_status - двухэтапная самокоррекция (verify, suspect, dispute, append_note).
5. review_knowledge - итоговое саморевью свежих карточек (кандидаты, флаги, дубли, рубрика).
6. get_help - точечная контекстная справка и правила для инструментов.
7. propose_hypothesis - фиксация рабочих гипотез во время отладки/исследования.
8. resolve_hypotheses - пакетная резолюция гипотез (confirm, refute, drop).
"""

import contextlib
import functools
import json
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, Protocol, TypeVar, cast

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import ValidationError

from mcp_core.error_envelope import (
    ErrorRule,
    error_envelope_to_tool_result,
)
from mcp_core.error_envelope import handle_tool_error as core_handle_tool_error
from mcp_core.error_envelope import make_error_response as core_make_error_response
from mcp_core.paths import (
    InvalidProjectPathError,
    ResolvedRoot,
    RootNotAnchoredError,
    ensure_root_anchored,
    is_anchored,
    log_root_banner,
    resolve_dynamic_project_root,
    resolve_factory_root,
)
from mcp_core.service_pool import ProjectServicePool
from mcp_core.tool_contract import PROJECT_PATH_PARAM, adapt_project_path_signature
from mcp_knowledge.errors import UncertainDuplicateError
from mcp_knowledge.help_docs import SERVER_INSTRUCTIONS, get_help_text
from mcp_knowledge.logger import logger
from mcp_knowledge.models import (
    CardRecordRequest,
    CardStatusUpdateRequest,
    HypothesesResolveRequest,
    HypothesisRecordRequest,
    HypothesisResolution,
    StorageUnavailableError,
)
from mcp_knowledge.service import (
    CardNotFoundError,
    ForbiddenUserConfirmedActionError,
    KnowledgeService,
    ReportLikeContentError,
    UserConfirmedConflictError,
)
from mcp_knowledge.storage import DuplicateCardError, KnowledgeStorage


class KnowledgeServiceBundle(NamedTuple):
    service: KnowledgeService
    storage: KnowledgeStorage
    knowledge_dir: Path
    cache_db: Path

    def close(self) -> None:
        self.storage.close()


def _normalize_str_list(val: Any) -> list[str]:
    """Нормализует список строк, поддерживая также одиночные строки и CSV."""
    if val is None:
        return []
    if isinstance(val, str):
        return [item.strip() for item in val.split(",") if item.strip()]
    if isinstance(val, (list, tuple, set)):
        return [str(item).strip() for item in val if str(item).strip()]
    return []


def _make_error_response(
    message: str,
    error_type: str,
    reason: str = "INTERNAL_ERROR",
    status: str = "rejected",
) -> dict[str, Any]:
    return core_make_error_response(
        message=message,
        error_type=error_type,
        reason=reason,
        status=status,
    )


# Доменные правила маппинга: единственная логируемая доменная ветвь —
# StorageUnavailableError (warning); остальные молчат (log_template=None),
# как в исходном маппере. RootNotAnchoredError (ValueError-подкласс) стоит
# ПЕРЕД базовой ветвью ValueError и логируется на warning — это ожидаемое
# состояние окружения, а не сбой.
KNOWLEDGE_ERROR_RULES: tuple[ErrorRule, ...] = (
    ErrorRule(
        exc=RootNotAnchoredError,
        error_type="RootNotAnchoredError",
        reason="ROOT_NOT_ANCHORED",
        log_template="Отказ записи: корень проекта не закреплён (%s): %s",
        log_level="warning",
    ),
    ErrorRule(
        exc=InvalidProjectPathError,
        error_type="InvalidProjectPathError",
        reason="INVALID_PROJECT_PATH",
        log_template="Невалидный путь проекта (%s): %s",
        log_level="warning",
    ),
    ErrorRule(
        exc=StorageUnavailableError,
        error_type="StorageUnavailableError",
        reason="STORAGE_UNAVAILABLE",
        log_template="Хранилище недоступно (%s): %s",
        log_level="warning",
    ),
    ErrorRule(
        exc=ValidationError,
        error_type="ValidationError",
        reason=None,
        log_template=None,
    ),
    ErrorRule(
        exc=DuplicateCardError,
        error_type="DuplicateCardError",
        reason="DUPLICATE_FOUND",
        log_template=None,
    ),
    ErrorRule(
        exc=UncertainDuplicateError,
        error_type="UncertainDuplicateError",
        reason="UNCERTAIN_DUPLICATE",
        log_template=None,
    ),
    ErrorRule(
        exc=UserConfirmedConflictError,
        error_type="UserConfirmedConflictError",
        reason="CONFLICT_USER_CONFIRMED",
        log_template=None,
    ),
    ErrorRule(
        exc=ForbiddenUserConfirmedActionError,
        error_type="ForbiddenUserConfirmedActionError",
        reason="FORBIDDEN_USER_CONFIRMED",
        log_template=None,
    ),
    ErrorRule(
        exc=CardNotFoundError,
        error_type="CardNotFoundError",
        reason="CARD_NOT_FOUND",
        log_template=None,
    ),
    ErrorRule(
        exc=ReportLikeContentError,
        error_type="ReportLikeContentError",
        reason="REPORT_LIKE_CONTENT",
        log_template=None,
    ),
)


def _handle_tool_error(
    e: Exception,
    context: str = "",
    validation_reason: str = "ANTI_SPAM_REJECTED",
) -> dict[str, Any]:
    # ValueError-ветвь: VALIDATION_FAILED-логика остаётся в обёртке (нюанс
    # knowledge-маппера, движок core о нём не знает).
    value_error_reason = (
        validation_reason if validation_reason == "VALIDATION_FAILED" else "INVALID_ARGUMENTS"
    )
    return core_handle_tool_error(
        e,
        context,
        rules=KNOWLEDGE_ERROR_RULES,
        logger=logger,
        validation_reason=validation_reason,
        value_error_reason=value_error_reason,
    )


_F = TypeVar("_F", bound=Callable[..., Any])
_ToolArgs = dict[str, Any]


class ToolReg(Protocol):
    """Декоратор регистрации тула с точной сигнатурой (ревью Ф2-3).

    Сохраняет имя параметра ``is_project`` и значение по умолчанию —
    ``Callable``-аннотация теряла их (mypy: 19 call-arg).
    """

    def __call__(
        self, is_project: bool = ...
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


_BundleResolver = Callable[[str | None], tuple[KnowledgeServiceBundle, ResolvedRoot, bool]]


def _result_error_message(res: Any) -> str | None:
    """Извлекает сообщение ошибки из CallToolResult для телеметрии.

    Возвращает None для результата без is_error (успех не считается ошибкой),
    иначе message/reason из envelope либо текст content[0].text.
    """
    if not isinstance(res, CallToolResult):
        return None
    if not res.is_error:
        return None
    if not res.content:
        return "Error"
    first_text = getattr(res.content[0], "text", None)
    if isinstance(first_text, str):
        try:
            parsed = json.loads(first_text)
            return parsed.get("message") or parsed.get("reason") or "Error"
        except Exception:
            return first_text
    return "Error"


def _telemetry(
    resolver: _BundleResolver,
    tool_name: str,
    payload: Callable[[_ToolArgs], dict[str, Any]],
    context: Callable[[_ToolArgs], str] | None = None,
    validation_reason: str = "ANTI_SPAM_REJECTED",
    on_error: Callable[[Exception, _ToolArgs], Any] | None = None,
    pool: ProjectServicePool[KnowledgeServiceBundle] | None = None,
) -> Callable[[_F], _F]:
    """Декоратор MCP-инструмента: единая телеметрия вызовов и error-envelope.

    Оборачивает тело инструмента в try/except/finally: замер latency, запись
    adoption-телеметрии в finally (сбои телеметрии подавляются) и маппинг
    исключений на единый error-envelope через ``_handle_tool_error``.

    При наличии пула открывает scope аренды на весь вызов (``pool.start_scope``)
    и освобождает его в finally ПОСЛЕ телеметрической записи — вторая аренда
    резолвера в телеметрии попадает в тот же scope и освобождается тем же
    ``release_scope``.
    """

    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            err_msg: str | None = None
            if pool is not None:
                pool.start_scope()
            try:
                res = fn(*args, **kwargs)
                if isinstance(res, CallToolResult):
                    if res.is_error:
                        err_msg = _result_error_message(res) or "Error"
                    return res
                if isinstance(res, dict) and res.get("error") is True:
                    err_msg = res.get("message") or res.get("reason") or "Error"
                    return error_envelope_to_tool_result(res)
                return res
            except Exception as e:
                err_msg = str(e)
                if on_error is not None:
                    # on_error может вернуть CallToolResult(is_error=True) с доменным
                    # envelope — извлекаем message для телеметрии (счёт сбоёв строго
                    # один раз, как и у стандартного error-guard ниже).
                    res = on_error(e, kwargs)
                    msg = _result_error_message(res)
                    if msg is not None:
                        err_msg = msg
                    return res
                envelope = _handle_tool_error(
                    e,
                    context=context(kwargs) if context is not None else tool_name,
                    validation_reason=validation_reason,
                )
                return error_envelope_to_tool_result(envelope)
            finally:
                dt_ms = (time.perf_counter() - t0) * 1000.0
                with contextlib.suppress(Exception):
                    bundle, _, _ = resolver(kwargs.get("project_path"))
                    bundle.storage.record_tool_call(tool_name, payload(kwargs), dt_ms, err_msg)
                if pool is not None:
                    pool.release_scope()

        return cast("_F", wrapper)

    return decorator


def _get_help_error(e: Exception, kwargs: _ToolArgs) -> CallToolResult:
    """Error-path для get_help: доменный error-envelope с is_error=True.

    Исторически возвращал текстовый fallback «⚠️ Ошибка...» со статусом успеха,
    что маскировало сбой на уровне протокола MCP. По плану harness-stability-fixes
    (шаг 1) клиент должен получать тот же структурированный envelope, что и у
    остальных инструментов: CallToolResult(is_error=True, structured_content=...).
    """
    logger.exception("Ошибка при получении справки по '%s': %s", kwargs.get("tool_name"), e)
    context = f"справка по '{kwargs.get('tool_name')}'"
    envelope = _handle_tool_error(e, context)
    return error_envelope_to_tool_result(envelope)


def _make_reg(server: MCPServer, multi_tenant: bool) -> ToolReg:
    """Фабрика декоратора регистрации тула (Ф2 плана core-dedup v4).

    Поглощает три побайтово одинаковые локальные копии ``_reg`` из
    ``_register_search_and_get_tools`` / ``_register_mutation_tools`` /
    ``_register_hypothesis_tools``: адаптация project_path-сигнатуры под
    multi-tenant-режим + регистрация через ``server.tool()``.
    """

    def _reg(is_project: bool = True) -> Callable[[Any], Any]:
        def dec(fn: Any) -> Any:
            adapted = adapt_project_path_signature(fn, multi_tenant) if is_project else fn
            return server.tool()(adapted)

        return dec

    return _reg


def _register_search_and_get_tools(
    server: MCPServer,
    resolver: _BundleResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[KnowledgeServiceBundle] | None = None,
) -> None:
    _reg = _make_reg(server, multi_tenant)

    @_reg()
    @_telemetry(
        resolver,
        "search_knowledge",
        payload=lambda kw: {
            "query": kw.get("query"),
            "categories": kw.get("categories"),
            "task_id": kw.get("task_id"),
        },
        context=lambda _kw: "поиск знаний",
        pool=pool,
    )
    def search_knowledge(
        query: str | None = None,
        categories: list[str] | str | None = None,
        task_id: str | None = None,
        include_hypotheses: bool = False,
        include_antipatterns: bool = True,
        include_overview: bool = False,
        include_archived: bool = False,
        include_drafts: bool = False,
        limit: int = 5,
        limit_per_section: int = 3,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Search engineering solutions, category facets, and the Project Overview summary.

        Trigger: Call before feature planning (to review project structure and topics), starting debugging, or writing code.
        Example: search_knowledge(query="FFmpeg freeze", categories=["audio"])
        Parameter details and modes: get_help('search_knowledge'). Response field 'results' is DEPRECATED (transitional alias for query_results, removed in v0.3).
        Response includes root visibility fields: project_root/root_source (warning emitted if project root is not anchored).
        """
        bundle, resolved, write_ok = resolver(project_path)
        norm_categories = _normalize_str_list(categories)
        clean_query = query.strip() if query and query.strip() else None

        # Auto include_overview: True for zero-query invocation or when explicitly requested
        effective_overview = include_overview or (
            clean_query is None and not norm_categories and not task_id
        )

        clamped_limit = max(1, min(limit, 20))
        clamped_section_limit = max(1, min(limit_per_section, 5))

        response = bundle.service.search_faceted(
            query=clean_query,
            categories=norm_categories if norm_categories else None,
            task_id=task_id,
            include_hypotheses=include_hypotheses,
            include_antipatterns=include_antipatterns,
            limit=clamped_limit,
            limit_per_facet=clamped_section_limit,
            include_overview=effective_overview,
            include_archived=include_archived,
            include_drafts=include_drafts,
        )
        result = response.model_dump(mode="json")

        result["project_root"] = str(resolved.root)
        result["root_source"] = resolved.source
        if not write_ok:
            result["root_warning"] = (
                f"Корень проекта не закреплён (source={resolved.source}): результаты "
                "могут быть пустыми/неполными, запись отклоняется ROOT_NOT_ANCHORED. "
                "Проверьте MCP_PROJECT_ROOT / --project-root в mcp_config.json."
            )
        return result

    @_reg()
    @_telemetry(
        resolver,
        "get_knowledge",
        payload=lambda kw: {"ids": kw.get("ids")},
        context=lambda kw: f"получение знаний ids={kw.get('ids')}",
        pool=pool,
    )
    def get_knowledge(
        ids: list[str] | str,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Retrieve full knowledge cards by their IDs (up to 10 IDs).

        Trigger: Call after search_knowledge to inspect discovered solutions/ADRs in detail.
        Example: get_knowledge(ids=["audio-ffmpeg-sigttin", "platform-sqlite-wal"])
        Details and response format: get_help('get_knowledge').
        """
        bundle, _, _ = resolver(project_path)
        clean_ids = _normalize_str_list(ids)
        resp = bundle.service.get(ids=clean_ids)
        return resp.model_dump(mode="json")

    @_reg()
    @_telemetry(
        resolver,
        "review_knowledge",
        payload=lambda kw: {
            "ids": kw.get("ids"),
            "task_id": kw.get("task_id"),
            "since": kw.get("since"),
        },
        context=lambda kw: f"саморевью карточек task_id={kw.get('task_id')}",
        validation_reason="VALIDATION_FAILED",
        pool=pool,
    )
    def review_knowledge(
        ids: list[str] | str | None = None,
        task_id: str | None = None,
        since: str | None = None,
        include_user_confirmed: bool = False,
        limit: int = 10,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Self-review summary for recent cards: candidates + flags + duplicate suggestions + rubric.

        Trigger: Call ONLY if new cards were created in the current session via record_knowledge — for self-verification before commit.
        Example: review_knowledge(task_id="task-1") or review_knowledge() (default: auto_inferred cards from the last 24h).
        Candidate selection: explicit ids (targeted) > task_id and/or since (combined with AND; ISO date, day precision) > last 24h window. Mark invalid cards via update_knowledge_status(action="dispute").
        Details and review rubric: get_help('review_knowledge').
        """
        bundle, _, _ = resolver(project_path)
        clean_ids = _normalize_str_list(ids)
        return bundle.service.review(
            ids=clean_ids or None,
            task_id=task_id,
            since=since,
            include_user_confirmed=include_user_confirmed,
            limit=limit,
        )

    @_reg(is_project=False)
    @_telemetry(
        resolver,
        "get_help",
        payload=lambda kw: {"tool_name": kw.get("tool_name")},
        on_error=_get_help_error,
        pool=pool,
    )
    def get_help(tool_name: str | None = "overview") -> str:
        """Detailed help, protocol guidelines, and canonical examples for Knowledge Engine tools.

        Trigger: Call when uncertain about argument formats or Anti-Spam Gate criteria.
        Example: get_help(tool_name="record_knowledge")
        """
        return get_help_text(tool_name)


def _register_mutation_tools(
    server: MCPServer,
    resolver: _BundleResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[KnowledgeServiceBundle] | None = None,
) -> None:
    _reg = _make_reg(server, multi_tenant)

    @_reg()
    @_telemetry(
        resolver,
        "record_knowledge",
        payload=lambda kw: {
            "title": kw.get("title"),
            "category": kw.get("category"),
            "type": kw.get("type"),
        },
        context=lambda kw: f"запись карточки '{kw.get('title')}'",
        validation_reason="VALIDATION_FAILED",
        pool=pool,
    )
    def record_knowledge(
        title: str,
        category: str,
        type: str = "lesson",
        solution: str = "",
        rationale: str = "",
        context: str | None = None,
        symptom: str | None = None,
        root_cause: str | None = None,
        source: str = "auto_inferred",
        agent: str | None = None,
        slug: str | None = None,
        conditions: str | None = None,
        nuances: str | None = None,
        aliases: list[str] | str | None = None,
        tags: list[str] | str | None = None,
        supersedes: str | None = None,
        related_to: list[str] | str | None = None,
        task_id: str | None = None,
        stale_after: str | None = None,
        status: str | None = None,
        force: bool = False,
        confirmed_force: bool = False,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Record valuable engineering experience: architectural decisions (decision/ADR), gotchas (lesson), research (research), or technical hints (hint).

        Trigger: Call ONLY when encountering hidden library/runtime traps (lesson), establishing new project-wide architectural conventions (decision), or completing non-trivial research. NOT required for 95% of routine tasks (0 cards).
        Example: record_knowledge(title="...", category="backend", type="lesson", symptom="...", root_cause="...", solution="...", rationale="...", task_id="task-1")
        For architectural decisions (ADR/decision), a unified context can be passed instead of symptom and root_cause:
        Example: record_knowledge(title="...", category="backend", type="decision", context="...", solution="...", rationale="...", task_id="task-1")
        Anti-Spam Gate criteria and reference examples: get_help('record_knowledge').
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored (no MCP_PROJECT_ROOT/--project-root/root markers).
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        if not category:
            raise ValueError("Параметр 'category' обязателен для карточки знаний")
        req = CardRecordRequest(
            title=title,
            category=category,
            type=type,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            agent=agent,
            context=context,
            symptom=symptom or "",
            root_cause=root_cause or "",
            solution=solution,
            rationale=rationale,
            slug=slug,
            conditions=conditions,
            nuances=nuances,
            aliases=_normalize_str_list(aliases),
            tags=_normalize_str_list(tags),
            supersedes=supersedes,
            related_to=_normalize_str_list(related_to),
            task_id=task_id,
            stale_after=stale_after,
            force=force,
            confirmed_force=confirmed_force,
        )
        result = bundle.service.record(req)
        result["project_root"] = str(resolved.root)
        return result

    @_reg()
    @_telemetry(
        resolver,
        "update_knowledge_status",
        payload=lambda kw: {"id": kw.get("id"), "action": kw.get("action")},
        context=lambda kw: f"обновление статуса карточки '{kw.get('id')}'",
        validation_reason="VALIDATION_FAILED",
        pool=pool,
    )
    def update_knowledge_status(
        id: str,
        action: str,
        note: str | None = None,
        by: str | None = None,
        source: str = "auto_inferred",
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Two-step self-correction of a card's status: verify, suspect, dispute, append_note, promote.

        Trigger: On solution failure (suspect -> check the old version via include_archived=True), bench verification (verify), a database error (dispute), or explicit user approval of a draft card (promote; the caller must actually obtain user consent first — never promote on the agent's own initiative). Card origin (generated_by/confidence) is preserved on promote; the approval is recorded in the verified entry and the PROMOTED log line.
        Consent boundary: 'user_confirmed'/'human' are caller-supplied strings; the server does NOT reject spoofed values — consent is a process agreement (ADR docs/analysis/adr-promote-consent-boundary.md), not a technical boundary.
        Example: update_knowledge_status(id="audio-ffmpeg-sigttin", action="suspect", note="Failure in Node 20: ...")
        Details: get_help('update_knowledge_status').
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when the project root is not anchored (no MCP_PROJECT_ROOT/--project-root/root markers).
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        clean_action = action.strip().lower() if action else ""
        clean_source = source.strip().lower() if source else "auto_inferred"
        actual_by = by or ("human" if clean_source == "user_confirmed" else "agent")
        req = CardStatusUpdateRequest(
            id=id,
            action=clean_action,  # type: ignore[arg-type]
            note=note,
            by=actual_by,
            source=clean_source,  # type: ignore[arg-type]
        )
        result = bundle.service.update_status(req)
        result["project_root"] = str(resolved.root)
        return result


def _register_hypothesis_tools(
    server: MCPServer,
    resolver: _BundleResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[KnowledgeServiceBundle] | None = None,
) -> None:
    _reg = _make_reg(server, multi_tenant)

    @_reg()
    @_telemetry(
        resolver,
        "propose_hypothesis",
        payload=lambda kw: {
            "title": kw.get("title"),
            "category": kw.get("category"),
            "task_id": kw.get("task_id"),
        },
        context=lambda kw: f"выдвижение гипотезы '{kw.get('title')}'",
        validation_reason="VALIDATION_FAILED",
        pool=pool,
    )
    def propose_hypothesis(
        title: str,
        category: str,
        symptom: str = "",
        hypothesis: str = "",
        test_plan: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        confidence: float = 0.20,
        tags: list[str] | str | None = None,
        aliases: list[str] | str | None = None,
        slug: str | None = None,
        force: bool = False,
        confirmed_force: bool = False,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Record a working hypothesis or implementation alternative during investigation or debugging.

        Trigger: MANDATORY when debugging non-obvious issues (>1 possible root cause) or architectural forks. Never keep hypotheses solely in hidden CoT!
        Example: propose_hypothesis(title="...", category="audio", symptom="...", hypothesis="...", task_id="task-1")
        Details: get_help('propose_hypothesis').
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored (no MCP_PROJECT_ROOT/--project-root/root markers).
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        if not category:
            raise ValueError("Параметр 'category' обязателен для гипотезы")
        req = HypothesisRecordRequest(
            title=title,
            category=category,
            symptom=symptom,
            hypothesis=hypothesis,
            test_plan=test_plan,
            task_id=task_id,
            agent=agent,
            confidence=confidence,
            tags=_normalize_str_list(tags),
            aliases=_normalize_str_list(aliases),
            slug=slug,
            force=force,
            confirmed_force=confirmed_force,
        )
        result = bundle.service.propose_hypothesis(req)
        result["project_root"] = str(resolved.root)
        return result

    @_reg()
    @_telemetry(
        resolver,
        "resolve_hypotheses",
        payload=lambda kw: {
            "task_id": kw.get("task_id"),
            "count": len(kw.get("resolutions") or []),
        },
        context=lambda kw: f"резолюция гипотез task_id={kw.get('task_id')}",
        validation_reason="VALIDATION_FAILED",
        pool=pool,
    )
    def resolve_hypotheses(
        resolutions: list[dict[str, Any]] | list[Any],
        task_id: str | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Batch-resolve hypotheses: confirm (promotes to knowledge card with solution), refute (records antipattern ⛔ or merges into existing card via merge_into='card-id'), drop (deletes draft), duplicate (marks superseded with duplicate_of='card-id').

        Trigger: MANDATORY before final response, walkthrough, or commit IF hypotheses were proposed during the task. Never leave open hypotheses.
        Example: resolve_hypotheses(task_id="task-1", resolutions=[{"id": "...", "action": "confirm", "solution": "..."}])
        Details: get_help('resolve_hypotheses').
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored (no MCP_PROJECT_ROOT/--project-root/root markers).
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        parsed_resolutions: list[HypothesisResolution] = []
        for item in resolutions:
            if isinstance(item, HypothesisResolution):
                parsed_resolutions.append(item)
            elif isinstance(item, dict):
                parsed_resolutions.append(HypothesisResolution(**item))
            else:
                raise ValueError(f"Некорректный формат элемента резолюции: {item}")

        req = HypothesesResolveRequest(task_id=task_id, resolutions=parsed_resolutions)
        result = bundle.service.resolve_hypotheses(req)
        result["project_root"] = str(resolved.root)
        return result


def create_server(
    service: KnowledgeService | None = None,
    project_root: Path | None = None,
    knowledge_dir: Path | None = None,
    cache_db: Path | None = None,
    multi_tenant: bool = False,
) -> MCPServer:
    """Фабрика для создания и настройки MCP сервера.

    Root-guard (P1): корень резолвится ВСЕГДА (дешёвая операция, без
    mkdir при инъекции ``service``) — для banner-лога и поля ``project_root``
    в ответах инструментов. ``write_ok`` (флаг разрешения записи write_ok):
    ``service`` инжектирован (моки тестов) ИЛИ передан явный
    ``knowledge_dir`` ИЛИ корень закреплён (env / walk-up / закреплённый
    ``--project-root``).

    K6 fail-fast: в multi-tenant-режиме ``knowledge_dir``/``cache_db``
    не имеют эффекта (пути резолвятся из per-project ``project_path``) —
    молчаливое игнорирование аргументов запрещено, reject на старте,
    до создания пула.
    """
    if multi_tenant and (knowledge_dir is not None or cache_db is not None):
        raise ValueError(
            "MULTI_TENANT_PATH_CONFLICT: --knowledge-dir/--cache-db игнорируются "
            "в multi-tenant-режиме (пути резолвятся из per-project project_path). "
            "Уберите эти аргументы или отключите multi-tenant."
        )

    default_resolved = resolve_factory_root(project_root) if not multi_tenant else None

    if service is not None:
        def_root = default_resolved.root if default_resolved is not None else Path.cwd()
        default_bundle = KnowledgeServiceBundle(
            service=service,
            storage=service.storage,
            knowledge_dir=knowledge_dir or (def_root / ".agents" / "knowledge"),
            cache_db=cache_db or (def_root / ".agents" / ".cache" / "knowledge_fts.db"),
        )
        pool: ProjectServicePool[KnowledgeServiceBundle] | None = None
        write_ok_default = True
    else:

        def _factory(r: Path) -> KnowledgeServiceBundle:
            if default_resolved is not None and r == default_resolved.root:
                k_dir = knowledge_dir or (r / ".agents" / "knowledge")
                c_db = cache_db or (r / ".agents" / ".cache" / "knowledge_fts.db")
            else:
                k_dir = r / ".agents" / "knowledge"
                c_db = r / ".agents" / ".cache" / "knowledge_fts.db"

            is_project = is_anchored(r) or (r / ".agents").exists()
            if is_project:
                k_dir.mkdir(parents=True, exist_ok=True)
                c_db.parent.mkdir(parents=True, exist_ok=True)
                store = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir, create_dirs=True)
            else:
                import hashlib

                h = hashlib.sha256(str(r).encode()).hexdigest()[:12]
                temp_db = Path(tempfile.gettempdir()) / f"mcp_knowledge_unanchored_{h}.db"
                store = KnowledgeStorage(db_path=temp_db, knowledge_dir=k_dir, create_dirs=False)

            svc = KnowledgeService(storage=store, knowledge_dir=k_dir)
            return KnowledgeServiceBundle(
                service=svc,
                storage=store,
                knowledge_dir=k_dir,
                cache_db=c_db,
            )

        pool = ProjectServicePool(_factory)
        if not multi_tenant and default_resolved is not None:
            default_bundle = pool.get(default_resolved.root)
            write_ok_default = bool(knowledge_dir) or default_resolved.anchored
        else:
            default_bundle = None
            write_ok_default = False

    if default_resolved is not None:
        log_root_banner("knowledge-engine", default_resolved, write_ok_default)

    def resolver(project_path: str | None) -> tuple[KnowledgeServiceBundle, ResolvedRoot, bool]:
        res = resolve_dynamic_project_root(
            project_path=project_path,
            multi_tenant=multi_tenant,
            default_root=default_resolved,
        )
        if pool is not None:
            # Scoped lease: bundle is protected from eviction for the whole tool
            # call (released in _telemetry finally via pool.release_scope).
            bundle = pool.scoped_get(res.root)
            effective_write_ok = res.anchored if project_path is not None else write_ok_default
        else:
            if default_bundle is None:
                raise RuntimeError("Default knowledge bundle is not initialized")
            bundle = default_bundle
            effective_write_ok = True
        return bundle, res, effective_write_ok

    server = MCPServer("mcp-knowledge-engine", instructions=SERVER_INSTRUCTIONS)
    _register_search_and_get_tools(server, resolver, multi_tenant=multi_tenant, pool=pool)
    _register_mutation_tools(server, resolver, multi_tenant=multi_tenant, pool=pool)
    _register_hypothesis_tools(server, resolver, multi_tenant=multi_tenant, pool=pool)
    return server


def run_server() -> None:
    """Точка запуска MCP stdio сервера с поддержкой CLI параметров и команды install."""

    if len(sys.argv) > 1 and sys.argv[1] == "install":
        from mcp_knowledge.installer import run_installer

        run_installer(sys.argv[2:])
        return

    import argparse

    parser = argparse.ArgumentParser(description="MCP Knowledge Engine stdio server")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Путь к корню целевого проекта",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=None,
        help="Путь к каталогу Markdown-карточек",
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
        knowledge_dir=args.knowledge_dir,
        cache_db=args.cache_db,
        multi_tenant=args.multi_tenant,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    run_server()
