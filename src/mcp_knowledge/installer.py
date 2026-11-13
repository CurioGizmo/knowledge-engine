"""Модуль автоматической установки и настройки MCP Knowledge Engine в AI-клиенты (Antigravity, Claude, Cursor)."""

import argparse
from pathlib import Path
from typing import Any

from mcp_core.installer import (
    get_antigravity_server_config,
    install_all_plugins,
    install_antigravity_server,
    install_skill,
    update_json_config,
)
from mcp_knowledge.help_docs import (
    AGENT_RULE_TEMPLATE,
    SERVER_INSTRUCTIONS,
    SKILL_TEMPLATE,
)

__all__ = [
    "AGENT_RULE_TEMPLATE",
    "KNOWLEDGE_TOOLS_LIST",
    "SERVER_INSTRUCTIONS",
    "SKILL_TEMPLATE",
    "_get_antigravity_server_config",
    "_update_json_config",
    "get_knowledge_tool_schemas",
    "install_agent_rule",
    "install_agent_skill",
    "install_antigravity",
    "run_installer",
]

KNOWLEDGE_TOOLS_LIST = [
    "record_knowledge",
    "search_knowledge",
    "get_knowledge",
    "update_knowledge_status",
    "review_knowledge",
    "get_help",
    "propose_hypothesis",
    "resolve_hypotheses",
]


def _get_antigravity_server_config(
    project_root: Path | None = None,
    eager: bool = True,
) -> dict[str, Any]:
    """Возвращает конфигурацию MCP сервера для Antigravity с гарантированным Eager-режимом."""
    return get_antigravity_server_config(
        binary_name="mcp-knowledge-engine",
        module_fallback="mcp_knowledge",
        eager=eager,
        tools=KNOWLEDGE_TOOLS_LIST if eager else None,
        project_root=project_root,
    )


def _update_json_config(path: Path, server_key: str, server_config: dict[str, Any]) -> bool:
    """Безопасно обновляет mcp_config.json с сохранением существующей структуры и созданием .bak."""
    return update_json_config(path, server_key, server_config)


def get_knowledge_tool_schemas(project_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Возвращает JSON-схемы инструментов knowledge, экспортированные напрямую из MCPServer."""
    from mcp_core.schema_export import export_server_schemas_sync
    from mcp_knowledge.server import create_server

    server = create_server(project_root=project_root)
    return export_server_schemas_sync(server)


def install_antigravity(
    home_dir: Path | None = None,
    project_root: Path | None = None,
    eager: bool = True,
) -> list[Path]:
    """Устанавливает конфигурацию в профили Antigravity (global + legacy)."""
    return install_antigravity_server(
        server_key="knowledge-engine",
        server_config=_get_antigravity_server_config(project_root=project_root, eager=eager),
        instructions=SERVER_INSTRUCTIONS,
        tool_schemas=get_knowledge_tool_schemas(project_root=project_root),
        home_dir=home_dir,
    )


def install_agent_rule(project_root: Path | None = None, force: bool = False) -> Path:
    """Устарело: правила .agents/rules/ упразднены в пользу скилла .agents/skills/knowledge-memory/."""
    import warnings

    warnings.warn(
        "Файлы правил (.agents/rules/knowledge.md) упразднены для экономии токенов системного промпта. "
        "Используйте install_agent_skill() для установки скилла knowledge-memory.",
        DeprecationWarning,
        stacklevel=2,
    )
    return install_agent_skill(project_root=project_root, force=force)


def install_agent_skill(project_root: Path | None = None, force: bool = False) -> Path:
    """Создаёт файл скилла для AI-агентов в проекте (.agents/skills/knowledge-memory/SKILL.md) с бэкапом."""
    return install_skill("knowledge-memory", project_root=project_root, force=force)


def run_installer(args: list[str] | None = None) -> None:
    """Точка входа CLI инсталлера."""
    parser = argparse.ArgumentParser(
        description="Установщик MCP Knowledge Engine (авто-настройка Eager-режима в AI-клиентах)"
    )
    parser.add_argument(
        "--antigravity",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Настроить Antigravity (включает Eager-режим по умолчанию, используйте --no-antigravity для отключения)",
    )
    parser.add_argument(
        "--eager",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Включить Eager-режим (по умолчанию: True, используйте --no-eager для Lazy)",
    )
    parser.add_argument(
        "--rule",
        action="store_true",
        default=False,
        help="(Устарело) Вместо правил устанавливает скилл knowledge-memory (.agents/skills/knowledge-memory/SKILL.md)",
    )
    parser.add_argument(
        "--skill",
        action="store_true",
        default=False,
        help="Создать скилл knowledge-memory в проекте (.agents/skills/knowledge-memory/SKILL.md)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Принудительно перезаписать существующие правила и скиллы, даже если они содержат пользовательские изменения",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Корень проекта для размещения правил и скиллов",
    )
    parser.add_argument(
        "--plugins",
        action="store_true",
        default=False,
        help="Install Antigravity plugins (~/.gemini/config/plugins) for all three engines",
    )

    parsed = parser.parse_args(args)

    print("🚀 Установка MCP Knowledge Engine...")
    if parsed.antigravity:
        install_antigravity(project_root=parsed.project_root, eager=parsed.eager)

    if parsed.rule:
        print(
            "⚠️  Правила в .agents/rules/ упразднены в пользу скилла knowledge-memory. Устанавливаем скилл..."
        )
        install_agent_skill(parsed.project_root, force=parsed.force)

    if parsed.skill:
        install_agent_skill(parsed.project_root, force=parsed.force)

    if parsed.plugins:
        install_all_plugins()

    mode_str = "Eager (нативные функции)" if parsed.eager else "Lazy (по требованию)"
    print(f"\n🎉 Установка завершена! Инструменты настроены как {mode_str}.")


if __name__ == "__main__":
    run_installer()
