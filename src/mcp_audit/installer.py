"""Модуль установки и настройки MCP Audit Engine (по умолчанию Eager в Antigravity)."""

import argparse
from pathlib import Path
from typing import Any

from mcp_core.installer import (
    get_antigravity_server_config,
    install_all_plugins,
    install_antigravity_server,
    install_skill,
)

__all__ = ["install_audit_skill", "run_installer"]

AUDIT_TOOLS_LIST = [
    "check_known_debt",
    "get_ready_backlog",
    "search_debt",
    "record_debt",
    "update_debt_status",
    "record_plan",
    "update_plan",
    "step_plan",
    "record_review",
    "get_task_context",
    "search_artifacts",
    "get_help",
]

AUDIT_INSTRUCTIONS = """# MCP Audit Engine (`audit-engine`)

Operational tracker of development plans, code reviews and technical debt.
The tools provide:
- Receiving an aggregated task context (`get_task_context`) before starting work;
- Maintaining step-by-step implementation plans (`record_plan`, `step_plan`, `update_plan`);
- Recording code review results and rounds (`record_review`);
- Search across plan, review and debt artifacts (`search_artifacts`);
- Known debt checks and backlog management (`check_known_debt`, `get_ready_backlog`, `search_debt`, `record_debt`, `update_debt_status`);
- Contextual help (`get_help`) with exact statuses and artifact_type compatibility.

Plan tracking channel: while this MCP server is available, create and update plans ONLY through
the API tools (`record_plan` / `update_plan` / `step_plan`). A repository file
`docs/plans/<date>-<slug>.md` is an explicit OFFLINE fallback for when the server is unreachable;
after MCP access is restored, re-import that plan via `record_plan` so the index has one source
of truth (no dual tracking).
"""


def get_audit_tool_schemas(
    project_root: Path | None = None,
    multi_tenant: bool = False,
) -> dict[str, dict[str, Any]]:
    """Возвращает JSON-схемы инструментов audit, экспортированные напрямую из MCPServer."""
    from mcp_audit.server import create_server
    from mcp_core.schema_export import export_server_schemas_sync

    server = create_server(project_root=project_root, multi_tenant=multi_tenant)
    return export_server_schemas_sync(server)


def install_audit_skill(project_root: Path | None = None, force: bool = False) -> Path:
    """Создаёт эталонный файл скилла audit-backlog в проекте."""
    return install_skill("audit-backlog", project_root=project_root, force=force)


def run_installer(args: list[str] | None = None) -> None:
    """Точка входа CLI инсталлера MCP Audit Engine."""
    parser = argparse.ArgumentParser(
        description="Установщик MCP Audit Engine (настройка Lazy-режима в AI-клиентах)"
    )
    parser.add_argument(
        "--antigravity",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Настроить Antigravity (по умолчанию: включено, используйте --no-antigravity для отключения)",
    )
    parser.add_argument(
        "--eager",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Включить Eager-режим для audit-engine (по умолчанию: True, используйте --no-eager для Lazy)",
    )
    parser.add_argument(
        "--skill",
        action="store_true",
        default=False,
        help="Создать скилл audit-backlog в проекте (.agents/skills/audit-backlog/SKILL.md)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Принудительно перезаписать существующие файлы скиллов",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Корень проекта для размещения скиллов и конфигурации",
    )
    parser.add_argument(
        "--plugins",
        action="store_true",
        default=False,
        help="Install Antigravity plugins (~/.gemini/config/plugins) for all three engines",
    )

    parsed = parser.parse_args(args)

    print("🚀 Установка MCP Audit Engine...")
    if parsed.antigravity:
        server_cfg = get_antigravity_server_config(
            binary_name="mcp-audit-engine",
            module_fallback="mcp_audit",
            eager=parsed.eager,
            tools=AUDIT_TOOLS_LIST if parsed.eager else None,
            project_root=parsed.project_root,
        )

        is_mt = parsed.project_root is None
        schemas = get_audit_tool_schemas(project_root=parsed.project_root, multi_tenant=is_mt)
        install_antigravity_server(
            server_key="audit-engine",
            server_config=server_cfg,
            instructions=AUDIT_INSTRUCTIONS,
            tool_schemas=schemas,
        )

    if parsed.skill:
        install_audit_skill(parsed.project_root, force=parsed.force)

    if parsed.plugins:
        install_all_plugins()

    mode_str = (
        "Eager (нативные функции)" if parsed.eager else "Lazy (по требованию через call_mcp_tool)"
    )
    print(f"\n🎉 Установка завершена! Сервер настроен как {mode_str}.")


if __name__ == "__main__":
    run_installer()
