"""Обработчики и регистрация инструментов управления планами (MCP Audit Engine)."""

from __future__ import annotations

import contextlib
import difflib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from mcp_audit.handlers.common import (
    AuditServiceBundle,
    AuditToolReg,
    CoercedInt,
    CoercedOptionalInt,
    CoercedPlanSteps,
    CoercedStrSeq,
    _AuditResolver,
    _make_reg,
    _tool_error_guard,
)
from mcp_audit.models import (
    PlanArtifact,
    PlanStatus,
    PlanStep,
    PlanVersionMetadata,
    PlanVersionsResponse,
    generate_plan_id,
)
from mcp_audit.parser import compute_plan_definition_projection, parse_plan_markdown
from mcp_audit.repository import PlanRepository
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer
from mcp_core.card_codec import split_frontmatter
from mcp_core.paths import ensure_root_anchored
from mcp_core.sanitizer import normalize_category_slug, sanitize_task_id
from mcp_core.service_pool import ProjectServicePool
from mcp_core.tool_contract import PROJECT_PATH_PARAM

__all__ = [
    "_get_plan_versions_diff",
    "_get_plan_versions_list",
    "_get_plan_versions_show",
    "_register_plan_tools",
    "get_plan_versions",
    "record_plan",
    "register_plan_tools",
    "restore_plan_version",
    "step_plan",
    "update_plan",
]


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
    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    clean_cat = normalize_category_slug(category) if category else "general"
    clean_task = sanitize_task_id(task_id)

    plan_steps: list[PlanStep] = []
    for i, s_text in enumerate(steps, 1):
        clean_text = s_text.strip()
        if clean_text.startswith(("- [ ]", "- [x]", "* [ ]", "* [x]")):
            clean_text = clean_text[5:].strip()
        plan_steps.append(PlanStep(index=i, title=clean_text, completed=False))

    today = datetime.now().strftime("%Y-%m-%d")

    clean_title = " ".join(title.split()).strip() if title else clean_task

    safe_details = details
    if safe_details and "## " in safe_details:
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

    path = PlanRepository(sync).create_unique(plan)
    return {
        "status": "created",
        "id": plan.id,
        "task_id": plan.task_id,
        "plan_status": plan.status.value,
        "file_path": str(path),
        "steps_count": len(plan.steps),
        "plan_version": plan.plan_version,
    }


def update_plan(
    task_id: str,
    title: str | None = None,
    add_steps: list[str] | None = None,
    target_files: list[str] | None = None,
    status: str | None = None,
    pr_url: str | None = None,
    category: str | None = None,
    reason: str | None = None,
    details: str | None = None,
    replace_details: str | None = None,
    replace_steps: list[str] | None = None,
    expected_version: int | None = None,
    reopen: bool = False,
    reset_progress: bool = False,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Обновляет метаданные плана (согласование approved, отмена cancelled, шаги).

    A13: при нескольких планах одного task_id укажите category — иначе
    AMBIGUOUS_PLAN.
    """
    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    plan_status = PlanStatus(status.lower()) if status else None
    plan, path = sync.update_plan_details(
        task_id=task_id,
        title=title,
        status=plan_status,
        pr_url=pr_url,
        target_files=target_files,
        add_steps=add_steps,
        category=category,
        reason=reason,
        details=details,
        replace_details=replace_details,
        replace_steps=replace_steps,
        expected_version=expected_version,
        reopen=reopen,
        reset_progress=reset_progress,
    )
    return {
        "status": "updated",
        "id": plan.id,
        "task_id": plan.task_id,
        "plan_status": plan.status.value,
        "file_path": str(path),
        "plan_version": plan.plan_version,
    }


def restore_plan_version(
    task_id: str,
    version: int,
    expected_version: int,
    expected_state_etag: str,
    reason: str,
    reset_progress: bool = False,
    reopen: bool = False,
    category: str | None = None,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Безопасный откат редакции плана: восстановление как новая версия N+1.

    Объектом снимка является карточка плана (.agents/plans/...).
    """
    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    return sync.restore_plan_version(
        task_id=task_id,
        version=version,
        expected_version=expected_version,
        expected_state_etag=expected_state_etag,
        reason=reason,
        reset_progress=reset_progress,
        reopen=reopen,
        category=category,
    )


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
    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    resp = sync.advance_plan_step(
        task_id=task_id,
        step=step,
        status=status,
        note=note,
        allow_out_of_order=allow_out_of_order,
        category=category,
    )
    return resp.model_dump()


def _get_plan_versions_list(
    version_dir: Path,
    clean_task: str,
    plan_id: str,
    current_version: int,
    limit: int,
) -> dict[str, Any]:
    eff_limit = 10 if limit <= 0 else min(limit, 100)
    versions_list: list[PlanVersionMetadata] = []
    if version_dir.exists():
        for p in version_dir.glob("v*.md"):
            m = re.match(r"^v(\d+)\.md$", p.name)
            if not m:
                continue
            with contextlib.suppress(Exception):
                raw = p.read_text(encoding="utf-8")
                meta_dict, _ = split_frontmatter(raw)
                if meta_dict:
                    versions_list.append(PlanVersionMetadata.model_validate(meta_dict))

    versions_sorted = sorted(versions_list, key=lambda v: v.version, reverse=True)
    resp = PlanVersionsResponse(
        task_id=clean_task,
        plan_id=plan_id,
        current_version=current_version,
        total_count=len(versions_sorted),
        versions=versions_sorted[:eff_limit],
        object_type="plan_card",
    )
    return resp.model_dump()


def _get_plan_versions_show(
    version_dir: Path,
    clean_task: str,
    plan_id: str,
    current_version: int,
    version: int | None,
) -> dict[str, Any]:
    target_ver = version if version is not None else current_version
    if target_ver <= 0:
        raise ValueError(
            f"PLAN_VERSION_NOT_FOUND: невалидный номер версии {target_ver} для плана '{plan_id}'"
        )

    snap_path = version_dir / f"v{target_ver:04d}.md"
    if not snap_path.exists():
        raise ValueError(
            f"PLAN_VERSION_NOT_FOUND: версия {target_ver} для плана '{plan_id}' "
            f"не найдена в истории карточки плана (.agents/plan_versions/). "
            f"Внешний документ в ветке в снимки не входит (A8), история не восстанавливается автоматически из git."
        )

    raw = snap_path.read_text(encoding="utf-8")
    meta_dict, snap_body = split_frontmatter(raw)
    meta = PlanVersionMetadata.model_validate(meta_dict) if meta_dict else None

    resp = PlanVersionsResponse(
        task_id=clean_task,
        plan_id=plan_id,
        version=target_ver,
        metadata=meta,
        raw_markdown=snap_body,
        object_type="plan_card",
    )
    return resp.model_dump()


def _get_plan_versions_diff(
    version_dir: Path,
    clean_task: str,
    plan_id: str,
    current_version: int,
    version: int | None,
    compare_to: int | None,
) -> dict[str, Any]:
    target_ver = version if version is not None else current_version
    compare_ver = compare_to if compare_to is not None else target_ver - 1

    if compare_ver <= 0:
        if target_ver == 1 and compare_to is None:
            resp = PlanVersionsResponse(
                task_id=clean_task,
                plan_id=plan_id,
                version=target_ver,
                compare_to=compare_ver,
                diff="",
                object_type="plan_card",
            )
            return resp.model_dump()
        raise ValueError(
            f"PLAN_VERSION_NOT_FOUND: невалидный номер версии compare_to={compare_ver} для diff"
        )

    v_from_path = version_dir / f"v{compare_ver:04d}.md"
    v_to_path = version_dir / f"v{target_ver:04d}.md"

    if not v_from_path.exists():
        raise ValueError(
            f"PLAN_VERSION_NOT_FOUND: версия {compare_ver} для плана '{plan_id}' "
            f"не найдена в истории карточки плана (.agents/plan_versions/). "
            f"Внешний документ в ветке в снимки не входит (A8), история не восстанавливается автоматически из git."
        )
    if not v_to_path.exists():
        raise ValueError(
            f"PLAN_VERSION_NOT_FOUND: версия {target_ver} для плана '{plan_id}' "
            f"не найдена в истории карточки плана (.agents/plan_versions/). "
            f"Внешний документ в ветке в снимки не входит (A8), история не восстанавливается автоматически из git."
        )

    raw_from = v_from_path.read_text(encoding="utf-8")
    raw_to = v_to_path.read_text(encoding="utf-8")
    _, body_from = split_frontmatter(raw_from)
    _, body_to = split_frontmatter(raw_to)

    proj_from = compute_plan_definition_projection(body_from)
    proj_to = compute_plan_definition_projection(body_to)

    diff_lines = list(
        difflib.unified_diff(
            proj_from.splitlines(keepends=True),
            proj_to.splitlines(keepends=True),
            fromfile=f"v{compare_ver:04d}",
            tofile=f"v{target_ver:04d}",
        )
    )
    diff_text = "".join(diff_lines)

    resp = PlanVersionsResponse(
        task_id=clean_task,
        plan_id=plan_id,
        version=target_ver,
        compare_to=compare_ver,
        diff=diff_text,
        object_type="plan_card",
    )
    return resp.model_dump()


def get_plan_versions(
    task_id: str,
    category: str | None = None,
    view: Literal["list", "show", "diff"] = "list",
    version: int | None = None,
    compare_to: int | None = None,
    limit: int = 10,
    synchronizer: AuditSynchronizer | None = None,
    storage: AuditStorage | None = None,
) -> dict[str, Any]:
    """Возвращает историю содержательных редакций плана: список версий, точный снимок или unified diff определений.

    Объектом снимка является карточка плана (.agents/plans/...), внешний документ в ветке в снимки не входит (A8).
    Если каталога истории нет или он пуст, запрос старой версии возвращает ошибку PLAN_VERSION_NOT_FOUND (история не восстанавливается из git).
    """
    if not task_id or not str(task_id).strip():
        raise ValueError("task_id обязателен для получения версий плана")

    if view not in ("list", "show", "diff"):
        raise ValueError(
            f"Неизвестный режим view='{view}'. Допустимые режимы: 'list', 'show', 'diff'."
        )

    clean_task = sanitize_task_id(task_id)
    clean_cat = normalize_category_slug(category) if category else None

    import mcp_audit.server as srv

    sync = synchronizer or srv.get_synchronizer(storage=storage)
    sync.sync_all()

    repo = PlanRepository(sync)
    plan_path = repo.get_unique(clean_task, clean_cat)
    if plan_path is None:
        raise ValueError(f"PLAN_NOT_FOUND: план для task_id '{clean_task}' не найден")

    content = plan_path.read_text(encoding="utf-8")
    plan = parse_plan_markdown(content, file_path=plan_path)

    sync.recover_orphan_snapshots(plan.id, plan.plan_version)
    version_dir = sync._plan_version_dir(plan.id)

    if view == "list":
        return _get_plan_versions_list(version_dir, clean_task, plan.id, plan.plan_version, limit)
    if view == "show":
        return _get_plan_versions_show(version_dir, clean_task, plan.id, plan.plan_version, version)
    if view == "diff":
        return _get_plan_versions_diff(
            version_dir, clean_task, plan.id, plan.plan_version, version, compare_to
        )

    raise ValueError(f"Неизвестный режим view='{view}'")


def register_plan_tools(
    server: MCPServer,
    resolver: _AuditResolver,
    multi_tenant: bool = False,
    pool: ProjectServicePool[AuditServiceBundle] | None = None,
) -> None:
    _reg: AuditToolReg = _make_reg(server, multi_tenant)

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
        clean_target = [str(x).strip() for x in target_files if x is not None and str(x).strip()]
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
        reason: str | None = None,
        details: str | None = None,
        replace_details: str | None = None,
        replace_steps: CoercedPlanSteps = (),
        expected_version: CoercedOptionalInt = None,
        reopen: bool = False,
        reset_progress: bool = False,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Update plan status (approved, in_progress, done, cancelled), title, or PR URL.
        Fail-fast: rejects with reason=ROOT_NOT_ANCHORED when project root is not anchored.
        A13: when the same task_id exists in multiple categories without category specified — rejects with AMBIGUOUS_PLAN;
        with category specified — selects the exact plan.

        Trigger: Call to approve a plan (status="approved"), attach a PR URL, or modify definition.
        Example: update_plan(task_id="PROJ-142", status="approved", pr_url="https://github.com/org/repo/pull/85")
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        clean_steps = [str(x).strip() for x in add_steps if x is not None and str(x).strip()]
        clean_replace_steps = [
            str(x).strip() for x in replace_steps if x is not None and str(x).strip()
        ]
        clean_target = [str(x).strip() for x in target_files if x is not None and str(x).strip()]
        result = update_plan(
            task_id=task_id,
            title=title,
            add_steps=clean_steps if clean_steps else None,
            target_files=clean_target if clean_target else None,
            status=status,
            pr_url=pr_url,
            category=category,
            reason=reason,
            details=details,
            replace_details=replace_details,
            replace_steps=clean_replace_steps if clean_replace_steps else None,
            expected_version=expected_version,
            reopen=reopen,
            reset_progress=reset_progress,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        return result

    @_reg(name="step_plan")
    @_tool_error_guard("step_plan", pool=pool)
    def step_plan_tool(
        task_id: str,
        step: CoercedInt,
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

    @_reg(name="get_plan_versions")
    @_tool_error_guard("get_plan_versions", pool=pool)
    def get_plan_versions_tool(
        task_id: str,
        category: str | None = None,
        view: Literal["list", "show", "diff"] = "list",
        version: CoercedOptionalInt = None,
        compare_to: CoercedOptionalInt = None,
        limit: CoercedInt = 10,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Return historical plan revisions: version list, exact snapshot, or unified diff of plan definitions.

        The snapshot target is the plan card (.agents/plans/...); external documents in branches are excluded (A8).
        If the history directory does not exist or is empty, requesting an older version returns PLAN_VERSION_NOT_FOUND (history is not restored from git).

        Trigger: Call to view plan version history, retrieve an exact snapshot of an earlier revision, or compute a diff between revisions.
        Example: get_plan_versions(task_id="PROJ-142", view="list")
        """
        bundle, resolved, _ = resolver(project_path)
        result = get_plan_versions(
            task_id=task_id,
            category=category,
            view=view,
            version=version,
            compare_to=compare_to,
            limit=limit,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        result["root_source"] = resolved.source
        return result

    @_reg(name="restore_plan_version")
    @_tool_error_guard("restore_plan_version", pool=pool)
    def restore_plan_version_tool(
        task_id: str,
        version: CoercedInt,
        expected_version: CoercedInt,
        expected_state_etag: str,
        reason: str,
        reset_progress: bool = False,
        reopen: bool = False,
        category: str | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Safely roll back a plan revision: restore as a new version N+1.

        The snapshot target is the plan card (.agents/plans/...); external documents in branches are excluded (A8).
        Verifies the ETag of the current file and checks for absence of progress conflicts.

        Trigger: Call when rolling back a plan to an earlier revision while preserving history.
        Example: restore_plan_version(task_id="PROJ-142", version=1, expected_version=2, expected_state_etag="...", reason="Rollback")
        """
        bundle, resolved, write_ok = resolver(project_path)
        ensure_root_anchored(write_ok, resolved)
        result = restore_plan_version(
            task_id=task_id,
            version=version,
            expected_version=expected_version,
            expected_state_etag=expected_state_etag,
            reason=reason,
            reset_progress=reset_progress,
            reopen=reopen,
            category=category,
            synchronizer=bundle.synchronizer,
            storage=bundle.storage,
        )
        result["project_root"] = str(resolved.root)
        return result


_register_plan_tools = register_plan_tools
