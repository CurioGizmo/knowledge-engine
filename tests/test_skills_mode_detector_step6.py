"""Тесты Шага 6: разделение MCP-режима и наличия инструментов, инвалидация кэша."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from mcp_skills.project_context import get_project_context
from mcp_skills.stack_detector import detect_mode


def test_agents_folders_do_not_falsely_indicate_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Папки .agents/audit и .agents/knowledge — это данные, а не признак активного MCP-сервера."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)

    # Создаём проект с данными .agents
    (tmp_path / ".agents" / "audit").mkdir(parents=True)
    (tmp_path / ".agents" / "knowledge").mkdir(parents=True)
    (tmp_path / ".agents" / ".cache").mkdir(parents=True)
    (tmp_path / ".agents" / ".cache" / "knowledge_fts.db").write_text("dummy", encoding="utf-8")

    mode = detect_mode(tmp_path)
    # Наличие файлов данных не должно автоматически включать mcp-режим
    assert mode is None


def test_mcp_active_servers_explicit_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """MCP_ACTIVE_SERVERS трактуется как явно заданная конфигурация."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine,knowledge-engine")

    mode = detect_mode(tmp_path)
    assert mode == "mcp"

    ctx = get_project_context(tmp_path)
    assert ctx.mode == "mcp"
    assert "skills-engine" in ctx.active_servers
    assert "knowledge-engine" in ctx.active_servers
    assert "audit-engine" not in ctx.active_servers
    assert ctx.has_server("skills-engine") is True
    assert ctx.has_server("audit-engine") is False


def test_mcp_active_servers_empty_means_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Пустой MCP_ACTIVE_SERVERS означает явный оффлайн-режим."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "")

    mode = detect_mode(tmp_path)
    assert mode == "offline"

    ctx = get_project_context(tmp_path)
    assert ctx.mode == "offline"
    assert ctx.active_servers == ()
    assert ctx.has_server("skills-engine") is False


def test_project_context_cache_invalidation_on_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Изменение конфигурации окружения инвалидирует кэшированный ProjectContext."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    ctx1 = get_project_context(tmp_path)
    assert ctx1.active_servers == ("skills-engine",)
    assert ctx1.has_server("knowledge-engine") is False

    # Изменяем конфигурацию
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine,knowledge-engine")
    ctx2 = get_project_context(tmp_path)
    assert "knowledge-engine" in ctx2.active_servers
    assert ctx2.has_server("knowledge-engine") is True

    # Переключаем в оффлайн
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "")
    ctx3 = get_project_context(tmp_path)
    assert ctx3.mode == "offline"
    assert ctx3.active_servers == ()
