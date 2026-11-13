"""Тесты для инсталлятора mcp_skills."""

import json
from pathlib import Path

from mcp_skills.installer import (
    SKILLS_TOOLS_LIST,
    get_skills_tool_schemas,
    install_skills_server,
    run_installer,
)


def test_install_skills_server_antigravity(tmp_path: Path) -> None:
    config_path = tmp_path / "antigravity.json"
    config_path.write_text("{}", encoding="utf-8")

    # Без явного project_root -> по умолчанию multi-tenant
    install_skills_server(target="antigravity", config_path=config_path)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "skills-engine" in data["mcpServers"]
    assert "mcp-skills-engine" in data["mcpServers"]["skills-engine"]["command"]
    assert data["mcpServers"]["skills-engine"]["args"] == ["--multi-tenant"]

    # С явным project_root -> --project-root <path>
    custom_root = tmp_path / "my_project"
    custom_root.mkdir()
    install_skills_server(target="antigravity", config_path=config_path, project_root=custom_root)
    data2 = json.loads(config_path.read_text(encoding="utf-8"))
    assert data2["mcpServers"]["skills-engine"]["args"] == [
        "--project-root",
        str(custom_root.resolve()),
    ]


def test_install_skills_server_migrates_legacy_key(tmp_path: Path) -> None:
    config_path = tmp_path / "antigravity.json"
    initial_data = {
        "mcpServers": {
            "skills": {"command": "legacy-mcp-skills", "args": []},
            "other": {"command": "other-server"},
        }
    }
    config_path.write_text(json.dumps(initial_data), encoding="utf-8")

    install_skills_server(target="antigravity", config_path=config_path)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "skills" not in data["mcpServers"]
    assert "skills-engine" in data["mcpServers"]
    assert "other" in data["mcpServers"]


def test_install_skills_server_home_dir(tmp_path: Path) -> None:
    # Проверка полной установки в home_dir (mcp_config.json + instructions.md + 9 schemas)
    install_skills_server(target="antigravity", home_dir=tmp_path)

    cfg_file = tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
    assert cfg_file.exists()
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "skills-engine" in data["mcpServers"]

    mcp_dir = tmp_path / ".gemini" / "antigravity" / "mcp" / "skills-engine"
    assert (mcp_dir / "instructions.md").exists()

    schemas = get_skills_tool_schemas()
    assert len(schemas) == 5
    assert set(schemas.keys()) == set(SKILLS_TOOLS_LIST)
    for tool_name in SKILLS_TOOLS_LIST:
        schema_file = mcp_dir / f"{tool_name}.json"
        assert schema_file.exists()


def test_install_skills_server_reinstall_cleans_stale_schemas(tmp_path: Path) -> None:
    """Проверяет, что флаг reinstall очищает устаревшие схемы из mcp/skills-engine/."""
    mcp_dir = tmp_path / ".gemini" / "antigravity" / "mcp" / "skills-engine"
    mcp_dir.mkdir(parents=True)
    stale_file = mcp_dir / "old_obsolete_tool.json"
    stale_file.write_text('{"name": "old_obsolete_tool"}', encoding="utf-8")

    assert stale_file.exists()
    install_skills_server(target="antigravity", home_dir=tmp_path, reinstall=True)

    assert not stale_file.exists()
    for tool_name in SKILLS_TOOLS_LIST:
        assert (mcp_dir / f"{tool_name}.json").exists()


def test_install_skills_skill(tmp_path: Path) -> None:
    from mcp_skills.installer import install_skills_skill

    skill_path = install_skills_skill(project_root=tmp_path)
    assert skill_path.exists()
    assert skill_path == tmp_path / ".agents" / "skills" / "skills-flow" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    assert "skills-flow" in content
    assert "step_flow" in content


def test_installed_skills_flow_keeps_audit_preflight_before_code_inspection(tmp_path: Path) -> None:
    from mcp_skills.installer import install_skills_skill

    installed = install_skills_skill(project_root=tmp_path).read_text(encoding="utf-8")
    plugin_source = (
        Path(__file__).resolve().parents[1]
        / "src/plugins/skills-engine-plugin/skills/skills-flow/SKILL.md"
    ).read_text(encoding="utf-8")

    assert installed == plugin_source
    assert 'get_task_context(task_id=..., view="summary")' in installed
    assert "до чтения кода" in installed
    assert "review-flow" in installed
    assert "execute-plan-flow" in installed


def test_run_installer_skill_flag(tmp_path: Path) -> None:
    run_installer(["--no-antigravity", "--skill", "--project-root", str(tmp_path)])
    assert (tmp_path / ".agents" / "skills" / "skills-flow" / "SKILL.md").exists()
