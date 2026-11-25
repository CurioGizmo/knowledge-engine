"""MCP-сервер управления техническим долгом и планами (MCP Audit Engine).

Поддерживает единый error-envelope, FastMCP-инструменты с Pydantic-схемами,
multi-tenant режим через project_path, scoped lease аренду сервисов и fail-fast
защиту корня проекта.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from mcp_audit.handlers import (
    AUDIT_ERROR_RULES,
    DEFAULT_LIMIT,
    AuditServiceBundle,
    AuditToolReg,
    CoercedInt,
    CoercedOptionalInt,
    CoercedPlanSteps,
    CoercedReviewCommentSeq,
    CoercedStrSeq,
    DuplicateDebtError,
    _AuditResolver,
    _coerce_json_int,
    _coerce_json_list,
    _coerce_json_optional_int,
    _coerce_plan_steps,
    _coerce_review_comments,
    _determine_review_round,
    _get_plan_versions_diff,
    _get_plan_versions_list,
    _get_plan_versions_show,
    _make_reg,
    _normalize_target_file_for_cmp,
    _register_debt_tools,
    _register_plan_tools,
    _register_review_and_context_tools,
    _tool_error_guard,
    _validate_symbol,
    _validate_target_file,
    check_known_debt,
    get_audit_help,
    get_plan_versions,
    get_ready_backlog,
    get_task_context,
    record_debt,
    record_plan,
    record_review,
    register_debt_tools,
    register_plan_tools,
    register_review_and_context_tools,
    restore_plan_version,
    search_artifacts,
    search_debt,
    step_plan,
    update_debt_status,
    update_plan,
)
from mcp_audit.installer import AUDIT_INSTRUCTIONS
from mcp_audit.models import generate_audit_id, generate_plan_id, generate_review_id
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer
from mcp_core.db import register_temp_db
from mcp_core.paths import (
    ResolvedRoot,
    _find_default_project_root,
    get_cache_dir,
    is_anchored,
    log_root_banner,
    resolve_dynamic_project_root,
    resolve_factory_root,
)
from mcp_core.service_pool import ProjectServicePool

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


__all__ = [
    "AUDIT_ERROR_RULES",
    "DEFAULT_LIMIT",
    "AuditServiceBundle",
    "AuditToolReg",
    "CoercedInt",
    "CoercedOptionalInt",
    "CoercedPlanSteps",
    "CoercedReviewCommentSeq",
    "CoercedStrSeq",
    "DuplicateDebtError",
    "_AuditResolver",
    "_coerce_json_int",
    "_coerce_json_list",
    "_coerce_json_optional_int",
    "_coerce_plan_steps",
    "_coerce_review_comments",
    "_determine_review_round",
    "_get_plan_versions_diff",
    "_get_plan_versions_list",
    "_get_plan_versions_show",
    "_get_synced_storage",
    "_make_reg",
    "_normalize_target_file_for_cmp",
    "_register_debt_tools",
    "_register_plan_tools",
    "_register_review_and_context_tools",
    "_tool_error_guard",
    "_validate_symbol",
    "_validate_target_file",
    "check_known_debt",
    "create_server",
    "generate_audit_id",
    "generate_plan_id",
    "generate_review_id",
    "get_audit_help",
    "get_plan_versions",
    "get_ready_backlog",
    "get_storage",
    "get_synchronizer",
    "get_task_context",
    "record_debt",
    "record_plan",
    "record_review",
    "register_debt_tools",
    "register_plan_tools",
    "register_review_and_context_tools",
    "reset_audit_singletons",
    "restore_plan_version",
    "run_server",
    "search_artifacts",
    "search_debt",
    "step_plan",
    "update_debt_status",
    "update_plan",
]


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
                h = hashlib.sha256(str(r).encode()).hexdigest()[:12]
                temp_db = Path(tempfile.gettempdir()) / f"mcp_audit_unanchored_{h}.db"
                register_temp_db(temp_db)
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
