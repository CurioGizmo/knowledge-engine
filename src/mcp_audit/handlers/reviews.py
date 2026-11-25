"""Обработчики и регистрация инструментов код-ревью и контекста задач (MCP Audit Engine)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from mcp_audit.handlers.common import (
    AuditServiceBundle,
    AuditToolReg,
    CoercedInt,
    CoercedReviewCommentSeq,
    CoercedStrSeq,
    _AuditResolver,
    _make_reg,
    _tool_error_guard,
    logger,
)
from mcp_audit.help_docs import get_audit_help_text
from mcp_audit.models import (
    PlanStatus,
    ReviewArtifact,
    ReviewComment,
    ReviewVerdict,
    generate_review_id,
)
from mcp_audit.parser import parse_plan_markdown
from mcp_audit.repository import PlanRepository
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer
from mcp_core.paths import ensure_root_anchored
from mcp_core.sanitizer import normalize_category_slug, sanitize_task_id
from mcp_core.service_pool import ProjectServicePool
from mcp_core.tool_contract import PROJECT_PATH_PARAM

__all__ = [
    "_determine_review_round",
    "_register_review_and_context_tools",
    "get_task_context",
    "record_review",
    "register_review_and_context_tools",
    "search_artifacts",
]


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
    round: int | None = None,  # noqa: A002
    category: str = "general",
    spawned_debts: list[str] | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Сохраняет артефакт код-ревью в .agents/reviews/<category>/."""
    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    clean_cat = normalize_category_slug(category) if category else "general"
    clean_task = sanitize_task_id(task_id) if task_id else None

    eff_round = _determine_review_round(sync, clean_cat, clean_task, pr_url, round)

    rev_verdict = ReviewVerdict(verdict.lower())
    review_comments: list[ReviewComment] = comments or []

    pr_num = None
    if pr_url:
        m = re.search(r"/pull/(\d+)", pr_url)
        if m:
            pr_num = m.group(1)

    review_id = generate_review_id(
        category=clean_cat,
        task_id=clean_task,
        round=eff_round,
        pr_number=pr_num,
    )

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
            plan_file = PlanRepository(sync).get_unique(clean_task, category=clean_cat)
            if plan_file and plan_file.exists():
                curr_plan = parse_plan_markdown(
                    plan_file.read_text(encoding="utf-8"), file_path=plan_file
                )
                all_done = bool(curr_plan.steps and all(s.completed for s in curr_plan.steps))
                if rev_verdict == ReviewVerdict.APPROVED:
                    if all_done:
                        sync.update_plan_details(
                            task_id=clean_task, status=PlanStatus.DONE, category=clean_cat
                        )
                    elif curr_plan.status == PlanStatus.DISCUSSED:
                        sync.update_plan_details(
                            task_id=clean_task, status=PlanStatus.APPROVED, category=clean_cat
                        )
                elif rev_verdict == ReviewVerdict.CHANGES_REQUESTED and curr_plan.status in (
                    PlanStatus.APPROVED,
                    PlanStatus.DONE,
                ):
                    sync.update_plan_details(
                        task_id=clean_task, status=PlanStatus.IN_PROGRESS, category=clean_cat
                    )
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
    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    sync.sync_all()
    store = storage or srv.get_storage()
    clean_task = sanitize_task_id(task_id) if task_id else None
    if clean_task:
        sync_err = sync.get_sync_error(clean_task)
        if sync_err:
            raise ValueError(
                f"SYNC_ERROR: Ошибка синхронизации плана для задачи '{clean_task}': {sync_err}"
            )
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
    """Полнотекстовый и фасетный поиск по планам, ревью и карточкам техдолга."""
    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    sync.sync_all()
    store = storage or srv.get_storage()
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


def register_review_and_context_tools(
    server: MCPServer,
    resolver: _AuditResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[AuditServiceBundle] | None = None,
) -> None:
    _reg: AuditToolReg = _make_reg(server, multi_tenant)

    @_reg(name="record_review")
    @_tool_error_guard("record_review", pool=pool)
    def record_review_tool(
        task_id: str | None = None,
        pr_url: str | None = None,
        verdict: Literal["approved", "changes_requested", "commented"] = "approved",
        summary: str = "",
        comments: CoercedReviewCommentSeq = (),
        round: CoercedInt = 0,  # noqa: A002
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
        typed_comments = (
            [
                c if isinstance(c, ReviewComment) else ReviewComment.model_validate(c)
                for c in comments
            ]
            if comments
            else None
        )
        r_val = round if round > 0 else None
        clean_debts = [str(x).strip() for x in spawned_debts if x is not None and str(x).strip()]
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
        limit: CoercedInt = 10,
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

    @server.tool(name="get_help")
    @_tool_error_guard("get_help")
    def get_help(tool_name: str | None = "overview") -> Any:
        """Contextual help for audit-engine tools: purpose, when to call, key params and statuses.

        Trigger: Call when unsure about argument format, allowed statuses, or artifact_type compatibility.
        Example: get_help(tool_name="record_debt")
        """
        return get_audit_help_text(tool_name)


_register_review_and_context_tools = register_review_and_context_tools
