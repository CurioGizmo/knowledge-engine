"""Модуль установки и настройки MCP Skills Engine."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from mcp_core.installer import (
    get_antigravity_server_config,
    install_all_plugins,
    install_antigravity_server,
    update_json_config,
)
from mcp_skills.adapters import SUPPORTED_TARGETS

SKILLS_TOOLS_LIST = [
    "search_skills",
    "load_skill",
    "create_skill",
    "get_help",
    "step_flow",
]

SKILLS_INSTRUCTIONS = """# MCP Skills Engine (`skills-engine`)

Catalog and assembly of modular skills, agent cards and development workflows.

## Model: engine is a LIBRARY, not a runtime
The engine DOES NOT execute anything itself. It returns instructions and content;
execution belongs to the host platform (you) or its native subagent mechanism.

## Roles
- **Skills** (`skills/`): modular instruction sets. Skeleton + `<!-- SLOT: ... -->` markers
  + block variants (`blocks/<slot>/<value>.md`). Assembled per project stack via `load_skill`.
- **Flows** (`flows/`): multi-stage scenario playbooks with a state machine (`step_flow`).
  Each stage's `<!-- meta -->` may name an `agent`, `gate`, `loop_max`.
- **Agent cards** (`agents/AGENT.yaml`): ready-made subagent prompt definitions
  (role, model, skills to bundle, permissions). The engine BUNDLES them
  (instructions + relevant skills, token-budgeted) — the HOST spawns the actual subagent
  with this bundle as its prompt. Cards reference skills by name; `permissions.write: false`
  means read-only auditor.

## Typical patterns
- Pick a skill: search_skills() → search_skills(category) → load_skill(name, stack).
- Run a review: step_flow(flow='review-flow', flow_options={'target_kind': 'code'}).
- Execute an approved plan: step_flow(flow='execute-plan-flow') defaults to auto/adaptive;
  use flow_options={'autonomy': 'semi'} to pause before independent review.
  Both modes require explicit human confirmation before push or PR publication.
  Each response contains
  the CURRENT stage instruction; if the stage names an agent, its bundle arrives in the
  `subagent` field: spawn your native subagent with that prompt, collect its verdict,
  then step_flow(action='advance', verdict=...).
- Author a new skill: get_help('create_skill').

## Tools
- `search_skills(query, type="all"|"skill"|"flow", category, limit)`:
  Catalog + FTS5. Zero-query → overview (categories, flows, stack_values).
  Auto-filters skills bound to incompatible stack (requires_stack) or mode (activation).
- `load_skill(name, stack)`:
  Assemble a skill: blocks land on SLOT markers; response has `slot_variants`, `stack_applied`.
  `stack` is an engine-wide vocabulary (language / mode mcp-offline / framework).
- `create_skill(name, title, description, content, blocks, stack, stack_descriptions, overwrite)`:
  Author a modular skill (skeleton + blocks + stack conventions).
- `get_help(tool_name)`:
  Detailed docs and patterns per tool.
- `step_flow(action, flow, flow_options, transition, deliverable, verdict, session_id,
  expected_stage_id, resolution, notes, human_confirmed)`:
  Flow state machine: advance / status / retry / abort / escalation_resolve.
"""

AGENTS_HINT = """Необязательная подсказка для корневого AGENTS.md, если клиент не загрузил skills-flow:
Для процесса: search_skills(type="flow") → step_flow.
Для отдельного скилла: search_skills(type="skill") → load_skill.
Если step_flow уже доставил инструкцию, не вызывай load_skill повторно.
Установщик не изменяет AGENTS.md.
"""

__all__ = [
    "SKILLS_INSTRUCTIONS",
    "SKILLS_TOOLS_LIST",
    "get_skills_tool_schemas",
    "install_skills_server",
    "install_skills_skill",
    "run_installer",
]


def install_skills_skill(project_root: Path | None = None, force: bool = False) -> Path:
    """Создаёт эталонный файл скилла skills-flow в проекте (.agents/skills/skills-flow/SKILL.md)."""
    from mcp_core.installer import install_skill

    return install_skill("skills-flow", project_root=project_root, force=force)


def get_skills_tool_schemas(project_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Возвращает JSON-схемы инструментов skills, экспортированные напрямую из MCPServer."""
    from mcp_core.schema_export import export_server_schemas_sync
    from mcp_skills.server import create_server

    server = create_server(project_root=project_root)
    return export_server_schemas_sync(server)


def install_skills_server(
    target: str = "antigravity",
    config_path: Path | None = None,
    home_dir: Path | None = None,
    project_root: Path | None = None,
    eager: bool = True,
    reinstall: bool = False,
) -> None:
    """Регистрирует сервер mcp-skills в указанном AI-клиенте."""
    if target == "antigravity":
        cfg = get_antigravity_server_config(
            binary_name="mcp-skills-engine",
            module_fallback="mcp_skills.server",
            eager=eager,
            tools=SKILLS_TOOLS_LIST if eager else None,
            project_root=project_root,
        )
        if config_path:
            update_json_config(config_path, "skills-engine", cfg, migrate_keys=["skills"])
            print(f"✅ Сервер 'skills-engine' зарегистрирован в {config_path}")
        else:
            schemas = get_skills_tool_schemas(project_root=project_root)
            install_antigravity_server(
                server_key="skills-engine",
                server_config=cfg,
                instructions=SKILLS_INSTRUCTIONS,
                tool_schemas=schemas,
                home_dir=home_dir,
                migrate_keys=["skills"],
            )
    else:
        print(f"Цель установки '{target}' пока не поддерживается автоматически.")


def run_installer(argv: list[str] | None = None) -> None:
    """CLI точка входа для mcp-skills-install."""
    parser = argparse.ArgumentParser(
        description="Установщик MCP Skills Engine (настройка в AI-клиентах)"
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
        help="Режим регистрации инструментов в Antigravity (по умолчанию: --eager; используйте --no-eager для Lazy)",
    )
    parser.add_argument(
        "--reinstall",
        action="store_true",
        default=False,
        help="Переустановить сервер и удалить устаревшие схемы инструментов",
    )
    parser.add_argument(
        "--target",
        default="antigravity",
        choices=list(SUPPORTED_TARGETS),
        help="Целевой AI-клиент (по умолчанию: antigravity)",
    )
    parser.add_argument(
        "--skill",
        action="store_true",
        default=False,
        help="Установить скиллы в проект",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Принудительно перезаписать существующие файлы",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Корень проекта для размещения конфигурации",
    )
    parser.add_argument(
        "--plugins",
        action="store_true",
        default=False,
        help="Install Antigravity plugins (~/.gemini/config/plugins) for all three engines",
    )
    args = parser.parse_args(argv)

    print("🚀 Установка MCP Skills Engine...")
    if args.antigravity and args.target == "antigravity":
        install_skills_server(
            target="antigravity",
            project_root=args.project_root,
            eager=args.eager,
            reinstall=args.reinstall,
        )
    elif args.target != "antigravity":
        install_skills_server(
            target=args.target,
            project_root=args.project_root,
            reinstall=args.reinstall,
        )

    if args.skill:
        install_skills_skill(args.project_root, force=args.force)

    if args.plugins:
        install_all_plugins()

    server_configured = args.antigravity and args.target == "antigravity"
    if server_configured:
        mode_str = (
            "Eager (нативные функции)" if args.eager else "Lazy (по требованию через call_mcp_tool)"
        )
        print(f"\n🎉 Установка завершена! Сервер настроен как {mode_str}.")
    else:
        print("\n🎉 Установка завершена!")
    if server_configured or args.skill:
        print(f"\n{AGENTS_HINT}")
