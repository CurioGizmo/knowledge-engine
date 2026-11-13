"""Тесты для плагинов Antigravity (knowledge-engine-plugin, skills-engine-plugin, audit-engine-plugin)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from mcp_core.installer import install_all_plugins, install_antigravity_plugin
from mcp_core.paths import _find_default_project_root


@pytest.fixture
def plugins_dir() -> Path:
    return _find_default_project_root() / "src" / "plugins"


def test_plugins_structure_and_manifests(plugins_dir: Path) -> None:
    """Проверяет наличие и валидность всех 3 плагинов в src/plugins/."""
    expected_plugins = {
        "knowledge-engine-plugin": {
            "server": "knowledge-engine",
            "skill": "knowledge-memory",
            "eager_tools_count": 8,
        },
        "skills-engine-plugin": {
            "server": "skills-engine",
            "skill": "skills-flow",
            "eager_tools_count": 4,
        },
        "audit-engine-plugin": {
            "server": "audit-engine",
            "skill": "audit-backlog",
            "eager_tools_count": 0,
        },
    }

    for plugin_name, meta in expected_plugins.items():
        p_dir = plugins_dir / plugin_name
        assert p_dir.is_dir(), f"Плагин {plugin_name} не найден"

        # 1. plugin.json
        manifest_file = p_dir / "plugin.json"
        assert manifest_file.is_file()
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        assert manifest["name"] == plugin_name
        assert "version" in manifest
        assert "description" in manifest

        # 2. mcp_config.json
        mcp_file = p_dir / "mcp_config.json"
        assert mcp_file.is_file()
        mcp_cfg = json.loads(mcp_file.read_text(encoding="utf-8"))
        assert meta["server"] in mcp_cfg["mcpServers"]
        server_conf = mcp_cfg["mcpServers"][meta["server"]]
        if meta["eager_tools_count"] > 0:
            assert server_conf.get("force_all_tools_eager") is True
            assert len(server_conf.get("tools", {})) == meta["eager_tools_count"]

        # 3. SKILL.md
        skill_file = p_dir / "skills" / meta["skill"] / "SKILL.md"
        assert skill_file.is_file(), f"Скилл {meta['skill']} не найден в {plugin_name}"
        skill_content = skill_file.read_text(encoding="utf-8")
        assert skill_content.startswith("---")
        assert f"name: {meta['skill']}" in skill_content


def test_install_antigravity_plugins(tmp_path: Path, plugins_dir: Path) -> None:
    """Проверяет корректность установки плагинов в целевой каталог пользователя."""
    installed = install_all_plugins(home_dir=tmp_path, plugins_source_dir=plugins_dir)
    assert len(installed) == 3

    target_base = tmp_path / ".gemini" / "config" / "plugins"
    assert (target_base / "knowledge-engine-plugin" / "plugin.json").is_file()
    assert (target_base / "skills-engine-plugin" / "plugin.json").is_file()
    assert (target_base / "audit-engine-plugin" / "plugin.json").is_file()

    # Проверка перезаписи при повторной установке
    reinstalled = install_antigravity_plugin(
        "knowledge-engine-plugin",
        home_dir=tmp_path,
        plugins_source_dir=plugins_dir,
    )
    assert reinstalled.is_dir()
