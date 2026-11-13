"""Шаг 5 плана AUDIT-VERIFY-2026-10: K6 — multi-tenant + --knowledge-dir/--cache-db.

До фикса аргументы молча игнорировались в multi-tenant-режиме (default_resolved=None,
factory-ветка с r == default_resolved.root недостижима). Теперь — fail-fast
MULTI_TENANT_PATH_CONFLICT в create_server до создания пула.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_knowledge.server import create_server


def test_multi_tenant_with_knowledge_dir_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="MULTI_TENANT_PATH_CONFLICT"):
        create_server(multi_tenant=True, knowledge_dir=tmp_path / "k")


def test_multi_tenant_with_cache_db_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="MULTI_TENANT_PATH_CONFLICT"):
        create_server(multi_tenant=True, cache_db=tmp_path / "c.db")


def test_multi_tenant_without_paths_still_starts(tmp_path: Path) -> None:
    """multi-tenant без путей — прежний контракт (сервер стартует)."""
    server = create_server(multi_tenant=True)
    assert server is not None


def test_single_tenant_with_paths_still_starts(tmp_path: Path) -> None:
    """single-tenant с явными путями — прежний контракт (не reject)."""
    server = create_server(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "k",
        cache_db=tmp_path / "c.db",
    )
    assert server is not None
