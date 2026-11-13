"""Тесты для универсального инсталлятора mcp_core и инсталлятора mcp_audit."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_audit.installer import (
    AUDIT_INSTRUCTIONS,
    AUDIT_TOOLS_LIST,
    install_audit_skill,
)
from mcp_core.installer import (
    get_antigravity_server_config,
    get_server_command,
    install_antigravity_server,
    install_file,
    update_json_config,
)


def test_core_get_server_command() -> None:
    """Проверяет определение бинарника или fallback на sys.executable."""
    cmd, args = get_server_command("non_existent_binary_xyz_123", "mcp_audit.server")
    assert "python" in cmd or sys_executable_present(cmd)
    assert args == ["-m", "mcp_audit.server"]


def sys_executable_present(cmd: str) -> bool:
    import sys

    return cmd == sys.executable


def test_core_get_antigravity_server_config() -> None:
    """Проверяет генерацию конфигураций для Eager и Lazy режимов."""
    # Eager mode
    eager_cfg = get_antigravity_server_config(
        binary_name="mcp-knowledge-engine",
        module_fallback="mcp_knowledge.server",
        eager=True,
        tools=["record_knowledge", "search_knowledge"],
    )
    assert eager_cfg["force_all_tools_eager"] is True
    assert "tools" in eager_cfg
    assert eager_cfg["tools"]["record_knowledge"]["eager"] is True

    # Lazy mode (audit-engine)
    lazy_cfg = get_antigravity_server_config(
        binary_name="mcp-audit-engine",
        module_fallback="mcp_audit.server",
        eager=False,
    )
    assert lazy_cfg["force_all_tools_eager"] is False
    assert "tools" not in lazy_cfg


def test_core_update_json_config_and_backup(tmp_path: Path) -> None:
    """Проверяет обновление mcp_config.json и создание .bak копии."""
    cfg_file = tmp_path / "mcp_config.json"
    server_1 = {"command": "mcp-server-1", "force_all_tools_eager": True}
    update_json_config(cfg_file, "server1", server_1)

    assert cfg_file.exists()
    with cfg_file.open(encoding="utf-8") as f:
        data = json.load(f)
    assert data["mcpServers"]["server1"]["command"] == "mcp-server-1"

    # Второе обновление -> создается backup
    server_2 = {"command": "mcp-server-2", "force_all_tools_eager": False}
    update_json_config(cfg_file, "server2", server_2)

    bak_file = tmp_path / "mcp_config.json.bak"
    assert bak_file.exists()

    with cfg_file.open(encoding="utf-8") as f:
        data2 = json.load(f)
    assert "server1" in data2["mcpServers"]
    assert "server2" in data2["mcpServers"]


def test_core_update_json_config_migration(tmp_path: Path) -> None:
    """Проверяет миграцию старых ключей серверов при обновлении конфига."""
    cfg_file = tmp_path / "mcp_config.json"
    initial = {"mcpServers": {"old-key": {"command": "legacy"}, "keep-me": {"command": "kept"}}}
    cfg_file.write_text(json.dumps(initial), encoding="utf-8")

    new_cfg = {"command": "new-server"}
    update_json_config(cfg_file, "new-key", new_cfg, migrate_keys=["old-key"])

    with cfg_file.open(encoding="utf-8") as f:
        data = json.load(f)
    assert "old-key" not in data["mcpServers"]
    assert "new-key" in data["mcpServers"]
    assert "keep-me" in data["mcpServers"]


def test_core_install_antigravity_server_with_schemas(tmp_path: Path) -> None:
    """Проверяет полную установку сервера с записью инструкций и схем."""
    server_cfg = {"command": "mcp-audit-engine", "force_all_tools_eager": False}
    schemas = {
        "check_known_debt": {
            "name": "check_known_debt",
            "description": "Проверка долга",
            "parameters": {"type": "object", "properties": {"symptom": {"type": "string"}}},
        }
    }

    installed = install_antigravity_server(
        server_key="audit-engine",
        server_config=server_cfg,
        instructions=AUDIT_INSTRUCTIONS,
        tool_schemas=schemas,
        home_dir=tmp_path,
    )
    assert len(installed) == 2

    # Проверяем файлы mcp/audit-engine
    mcp_dir = tmp_path / ".gemini" / "antigravity" / "mcp" / "audit-engine"
    assert (mcp_dir / "instructions.md").exists()
    assert (mcp_dir / "check_known_debt.json").exists()

    with (mcp_dir / "check_known_debt.json").open(encoding="utf-8") as f:
        schema_data = json.load(f)
    assert schema_data["name"] == "check_known_debt"


def test_install_audit_skill(tmp_path: Path) -> None:
    """Проверяет генерацию скилла audit-backlog."""
    skill_file = install_audit_skill(project_root=tmp_path)
    assert skill_file.exists()
    assert skill_file == tmp_path / ".agents" / "skills" / "audit-backlog" / "SKILL.md"

    content = skill_file.read_text(encoding="utf-8")
    assert "audit-backlog" in content
    assert "check_known_debt" in content
    assert "get_ready_backlog" in content


def test_load_template_content_and_missing() -> None:
    """Проверяет загрузку шаблона и выброс FileNotFoundError для несуществующего шаблона."""
    import pytest

    from mcp_core.installer import load_template_content

    # Успешная загрузка скилла
    content = load_template_content("skills/knowledge-memory/SKILL.md")
    assert "Knowledge Engine Lifecycle" in content

    # Ошибка при отсутствии
    with pytest.raises(FileNotFoundError, match="не найден"):
        load_template_content("non_existent_path_xyz.md")


def test_install_skill(tmp_path: Path) -> None:
    """Проверяет установку скилла агента через mcp_core."""
    from mcp_core.installer import install_skill

    skill_file = install_skill("knowledge-memory", project_root=tmp_path)
    assert skill_file.exists()
    assert skill_file == tmp_path / ".agents" / "skills" / "knowledge-memory" / "SKILL.md"
    assert "Pre-check" in skill_file.read_text(encoding="utf-8")


def test_templates_parity() -> None:
    """Проверяет, что шаблоны в src/mcp_core/templates/ согласованы с плагинами и help_docs (no drift).

    src/mcp_core/templates/ является Single Source of Truth для шаблонов скиллов.
    """
    from mcp_core.paths import _find_default_project_root
    from mcp_knowledge.help_docs import SKILL_TEMPLATE

    root = _find_default_project_root()
    pkg_templates = root / "src" / "mcp_core" / "templates"

    # Протокольные константы инсталлера knowledge должны совпадать с эталоном mcp_core/templates.
    skill_file = pkg_templates / "skills" / "knowledge-memory" / "SKILL.md"
    assert skill_file.exists(), "Отсутствует эталон knowledge-memory в src/mcp_core/templates/"
    assert SKILL_TEMPLATE == skill_file.read_text(encoding="utf-8"), (
        "SKILL_TEMPLATE разошёлся с src/mcp_core/templates/skills/knowledge-memory/SKILL.md"
    )

    # Паритет шаблона knowledge-memory и плагина knowledge-engine-plugin
    plugin_knowledge_memory = (
        root
        / "src"
        / "plugins"
        / "knowledge-engine-plugin"
        / "skills"
        / "knowledge-memory"
        / "SKILL.md"
    )
    assert plugin_knowledge_memory.exists(), (
        "Отсутствует knowledge-memory в knowledge-engine-plugin"
    )
    assert plugin_knowledge_memory.read_text(encoding="utf-8") == skill_file.read_text(
        encoding="utf-8"
    ), "Рассинхрон между плагином knowledge-engine-plugin и шаблоном knowledge-memory"

    # Паритет шаблона skills-flow и плагина skills-engine-plugin
    plugin_skills_flow = (
        root / "src" / "plugins" / "skills-engine-plugin" / "skills" / "skills-flow" / "SKILL.md"
    )
    pkg_skills_flow = pkg_templates / "skills" / "skills-flow" / "SKILL.md"
    assert plugin_skills_flow.exists(), "Отсутствует skills-flow в skills-engine-plugin"
    assert pkg_skills_flow.exists(), "Отсутствует skills-flow в mcp_core/templates"
    assert plugin_skills_flow.read_text(encoding="utf-8") == pkg_skills_flow.read_text(
        encoding="utf-8"
    ), "Рассинхрон между плагином skills-engine-plugin и шаблоном skills-flow"

    # Паритет шаблона audit-backlog и плагина audit-engine-plugin
    plugin_audit_backlog = (
        root / "src" / "plugins" / "audit-engine-plugin" / "skills" / "audit-backlog" / "SKILL.md"
    )
    pkg_audit_backlog = pkg_templates / "skills" / "audit-backlog" / "SKILL.md"
    assert plugin_audit_backlog.exists(), "Отсутствует audit-backlog в audit-engine-plugin"
    assert pkg_audit_backlog.exists(), "Отсутствует audit-backlog в mcp_core/templates"
    assert plugin_audit_backlog.read_text(encoding="utf-8") == pkg_audit_backlog.read_text(
        encoding="utf-8"
    ), "Рассинхрон между плагином audit-engine-plugin и шаблоном audit-backlog"
