"""Тесты для модуля инсталлятора MCP Knowledge Engine."""

import json
from pathlib import Path

import pytest

from mcp_knowledge.installer import (
    _get_antigravity_server_config,
    _update_json_config,
    install_agent_rule,
    install_antigravity,
)


def test_get_antigravity_server_config():
    cfg = _get_antigravity_server_config()
    assert cfg["force_all_tools_eager"] is True
    assert "tools" in cfg
    assert cfg["tools"]["record_knowledge"]["eager"] is True
    assert cfg["tools"]["search_knowledge"]["eager"] is True


def test_update_json_config(tmp_path: Path):
    target = tmp_path / "mcp_config.json"
    server_cfg = {"command": "test", "force_all_tools_eager": True}

    # 1. Запись в новый файл
    assert _update_json_config(target, "my-server", server_cfg) is True
    with target.open(encoding="utf-8") as f:
        data = json.load(f)
    assert data["mcpServers"]["my-server"]["force_all_tools_eager"] is True

    # 2. Обновление существующего файла с сохранением других серверов
    other_cfg = {"command": "other"}
    _update_json_config(target, "other-server", other_cfg)
    with target.open(encoding="utf-8") as f:
        data2 = json.load(f)
    assert "my-server" in data2["mcpServers"]
    assert "other-server" in data2["mcpServers"]


def test_install_antigravity(tmp_path: Path):
    installed = install_antigravity(home_dir=tmp_path)
    assert len(installed) == 2
    for p in installed:
        assert p.exists()
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        assert "knowledge-engine" in data["mcpServers"]
        assert data["mcpServers"]["knowledge-engine"]["force_all_tools_eager"] is True
        assert data["mcpServers"]["knowledge-engine"]["tools"]["record_knowledge"]["eager"] is True


def test_install_agent_skill(tmp_path: Path):
    from mcp_knowledge.installer import install_agent_skill

    skill_file = install_agent_skill(project_root=tmp_path)
    assert skill_file.exists()
    assert skill_file == tmp_path / ".agents" / "skills" / "knowledge-memory" / "SKILL.md"
    content = skill_file.read_text(encoding="utf-8")
    assert "knowledge-memory" in content
    assert "Pre-check" in content
    assert "Investigation" in content
    assert "propose_hypothesis" in content
    assert "resolve_hypotheses" in content
    assert "record_knowledge" in content
    assert "Completion Checkpoint" in content or "Чекпоинт завершения" in content
    assert "walkthrough.md" in content


def test_install_agent_rule_deprecated(tmp_path: Path):
    import pytest

    with pytest.deprecated_call():
        skill_file = install_agent_rule(project_root=tmp_path)
    assert skill_file.exists()
    assert skill_file == tmp_path / ".agents" / "skills" / "knowledge-memory" / "SKILL.md"


def test_run_installer_rule_flag(tmp_path: Path):
    from mcp_knowledge.installer import run_installer

    run_installer(args=["--no-antigravity", "--rule", "--project-root", str(tmp_path)])
    installed_skill = tmp_path / ".agents" / "skills" / "knowledge-memory" / "SKILL.md"
    assert installed_skill.exists()


def test_run_installer_plugins_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """--plugins installs all three engine plugins into the (temporary) HOME."""
    from mcp_knowledge.installer import run_installer

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    run_installer(args=["--no-antigravity", "--plugins"])
    plugins_dir = tmp_path / ".gemini" / "config" / "plugins"
    for name in (
        "knowledge-engine-plugin",
        "skills-engine-plugin",
        "audit-engine-plugin",
    ):
        assert (plugins_dir / name / "plugin.json").is_file(), name


def test_skills_installer_plugins_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """mcp-skills-install --plugins installs the engine plugins too."""
    from mcp_skills.installer import run_installer as run_skills_installer

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    run_skills_installer(argv=["--no-antigravity", "--plugins"])
    plugins_dir = tmp_path / ".gemini" / "config" / "plugins"
    for name in (
        "knowledge-engine-plugin",
        "skills-engine-plugin",
        "audit-engine-plugin",
    ):
        assert (plugins_dir / name / "plugin.json").is_file(), name


def test_audit_installer_plugins_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """mcp-audit-install --plugins installs the engine plugins too."""
    from mcp_audit.installer import run_installer as run_audit_installer

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    run_audit_installer(args=["--no-antigravity", "--plugins"])
    plugins_dir = tmp_path / ".gemini" / "config" / "plugins"
    for name in (
        "knowledge-engine-plugin",
        "skills-engine-plugin",
        "audit-engine-plugin",
    ):
        assert (plugins_dir / name / "plugin.json").is_file(), name
