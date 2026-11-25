"""Централизованные фикстуры и изоляция тестового окружения для pytest.

Обеспечивает:
1. Автоматический сброс модульных кэшей (_CONTEXT_CACHE, _CATEGORIES_CACHE).
2. Автоматический сброс серверных синглтонов (_STORAGE, _SYNCHRONIZER, _COMPILER, _BUNDLER, _GIT_MGR).
3. Изоляцию переменных окружения (MCP_ACTIVE_SERVERS, MCP_PROJECT_ROOT, MCP_SKILLS_CACHE_DIR).
4. Безопасную фикстуру для нахождения каталога mcp_skills (companion_skills_dir).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from mcp_audit import server as audit_server_module
from mcp_core.db import cleanup_temp_dbs
from mcp_skills import categories as categories_module
from mcp_skills import project_context as pc_module
from mcp_skills import server as skills_server_module


@pytest.fixture(autouse=True)
def _reset_global_caches() -> Generator[None, None, None]:
    """Сбрасывает модульные кэши до и после каждого теста."""

    def _clear():
        pc_module._CONTEXT_CACHE.clear()
        categories_module._CATEGORIES_CACHE.clear()
        with contextlib.suppress(Exception):
            skills_server_module._STACK_VALUES_CACHE.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_server_singletons() -> Generator[None, None, None]:
    """Сбрасывает серверные синглтоны до и после каждого теста."""

    def _reset():
        skills_server_module._STORAGE = None
        skills_server_module._SYNCHRONIZER = None
        skills_server_module._COMPILER = None
        skills_server_module._BUNDLER = None
        skills_server_module._GIT_MGR = None
        skills_server_module.cleanup_unanchored_temp_dbs()
        cleanup_temp_dbs()
        audit_server_module.reset_audit_singletons()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Изолирует глобальные переменные окружения MCP.

    Не удаляет MCP_SKILLS_PATH, если текущий модуль/тест явно использует
    module-scoped фикстуру skills_repo (например, в test_g_followup_e2e.py).
    """
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("MCP_SKILLS_CACHE_DIR", raising=False)

    if "skills_repo" not in request.fixturenames:
        monkeypatch.delenv("MCP_SKILLS_PATH", raising=False)


def get_companion_skills_dir() -> Path | None:
    """Возвращает путь к каталогу скиллов mcp_skills, если он существует.

    Приоритет:
    1. Переменная окружения MCP_SKILLS_TEST_CATALOG
    2. Соседний репозиторий REPO_ROOT.parent / 'mcp_skills'
    3. Локальный путь хоста (fallback)
    """
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        os.environ.get("MCP_SKILLS_TEST_CATALOG"),
        str(repo_root.parent / "mcp_skills"),
        "/home/anton/projects/mcp_skills",
    ]
    for cand in candidates:
        if cand and Path(cand).is_dir() and (Path(cand) / "skills").is_dir():
            return Path(cand)
    return None


@pytest.fixture
def companion_skills_dir() -> Path | None:
    return get_companion_skills_dir()
