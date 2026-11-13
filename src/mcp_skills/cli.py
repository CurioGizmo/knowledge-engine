"""Консольный интерфейс (CLI) для утилиты mcp-skills."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Literal, cast

from mcp_skills.adapters import SUPPORTED_TARGETS
from mcp_skills.categories import get_categories_prompt
from mcp_skills.errors import SkillsError
from mcp_skills.server import (
    abort_flow,
    advance_flow,
    get_flow_status,
    install_agent_workflow,
    list_flows,
    load_agent,
    load_skill,
    resolve_flow_escalation,
    run_server,
    search_skills,
    start_flow,
)

__all__ = ["main"]


def _handle_search(parsed: argparse.Namespace) -> int:
    search_type = cast(Literal["all", "skill", "flow"], getattr(parsed, "search_type", "all"))
    category = getattr(parsed, "category", None)
    try:
        res = search_skills(
            query=parsed.query,
            type=search_type,
            category=category,
            limit=parsed.limit,
            project_root=parsed.project_root,
            project_stack="auto",
        )
    except SkillsError as e:
        print(f"🔴 Ошибка поиска: {e}")
        return 1

    if res.get("mode") == "overview":
        print("🧭 Обзор каталога скиллов и флоу:")
        print("\nКатегории:")
        categories = res.get("categories", [])
        for d in categories:
            cat_name = d.get("category", "")
            print(f"• {cat_name}: {d['skills_count']} скилл(ов)")
        print("\nСценарии (flows):")
        for f in res.get("flows", []):
            desc = f.get("description", "")
            print(f"• {f['name']}" + (f": {desc}" if desc else ""))
        return 0

    if res.get("mode") == "flows":
        count = res.get("count", len(res.get("flows", [])))
        print(f"Найдено сценариев: {count}")
        for f in res.get("flows", []):
            print(f"• {f['name']}: {f.get('title', '')}")
            if f.get("description"):
                print(f"  {f['description']}")
        return 0

    results = res.get("results", [])
    count = res.get("count", len(results))
    print(f"Найдено скиллов: {count}")
    for s in results:
        slots_str = (
            f" [слоты: {', '.join(s.get('available_slots', []))}]"
            if s.get("available_slots")
            else ""
        )
        cat_name = s.get("category", "general")
        print(f"• {s['name']} ({cat_name}){slots_str}: {s.get('title', '')}")
        if s.get("description"):
            print(f"  {f['description']}" if (f := s).get("description") else "")
    return 0


def _handle_load(parsed: argparse.Namespace) -> int:
    stack_val = getattr(parsed, "stack", None) or getattr(parsed, "touches", None)
    stack = [t.strip() for t in stack_val.split(",")] if stack_val else None
    try:
        res = load_skill(name=parsed.name, stack=stack, project_root=parsed.project_root)
        print(res["content"])
        return 0
    except (SkillsError, ValueError) as e:
        # ValueError — fail-fast валидация стека (недопустимое имя технологии)
        print(f"🔴 Ошибка загрузки скилла: {e}")
        return 1


def _handle_agent(parsed: argparse.Namespace) -> int:
    project = getattr(parsed, "project", None)
    project_root = getattr(parsed, "project_root", None)
    try:
        if parsed.install:
            res = install_agent_workflow(
                name=parsed.name, target=parsed.target, project=project, project_root=project_root
            )
            if res.get("success"):
                print(f"✅ Агент '{parsed.name}' установлен: {res['installed_path']}")
                return 0
            print(f"🔴 Ошибка установки агента: {res}")
            return 1

        res_load = load_agent(
            name=parsed.name,
            target=parsed.target,
            project=project,
            project_root=project_root,
        )
        print(res_load["content"])
        return 0
    except SkillsError as e:
        print(f"🔴 Ошибка: {e}")
        return 1


def _flow_start(parsed: argparse.Namespace) -> int:
    res = start_flow(
        name=parsed.name,
        task_id=parsed.task_id,
        project_root=parsed.project_root,
        target=parsed.target,
    )
    print(f"✅ Флоу '{res['flow_name']}' запущен. Сессия: {res['session_id']}")
    stage = res["stage"]
    print(f"Этап {stage['order']}/{stage['total_stages']}: {stage['title']} [{stage['id']}]")
    print(f"\n{stage['instruction']}")
    if "subagent" in res:
        sub = res["subagent"]
        print(f"\n🤖 Назначен агент: {sub['name']}")
    return 0


def _flow_advance(parsed: argparse.Namespace) -> int:
    res = advance_flow(
        session_id=parsed.session_id,
        deliverable=parsed.deliverable,
        verdict=parsed.verdict,
        notes=parsed.notes,
        project_root=parsed.project_root,
        target=parsed.target,
    )
    if res.get("status") == "gate_blocked":
        print("⛔ Шлюз качества заблокирован:")
        for b in res.get("blockers", []):
            print(f"  • {b}")
        return 1
    if res.get("status") == "blocked_escalate":
        print("⚠️  Circuit Breaker заблокировал сессию!")
        print(f"Директива: {res.get('directive')}")
        return 1
    if res.get("status") == "completed":
        print(f"🎉 {res.get('message')}")
        return 0
    if not res.get("stage"):
        print(f"ℹ️  {res.get('message', '')}")
        return 0
    stage = res.get("stage", {})
    print(f"▶️  Переход на этап: {stage.get('title')} [{stage.get('id')}]")
    print(f"\n{stage.get('instruction', '')}")
    return 0


def _flow_status(parsed: argparse.Namespace) -> int:
    res = get_flow_status(
        session_id=parsed.session_id,
        project_root=parsed.project_root,
        target=parsed.target,
    )
    if res.get("status") == "no_active_session":
        print("Нет активных сессий флоу.")
        return 0
    if res.get("status") == "multiple_active":
        print("Найдено несколько активных сессий:")
        for s in res.get("sessions", []):
            print(f"  • {s['id']} ({s['flow_name']}, этап: {s['current_stage_id']})")
        return 0
    sess = res["session"]
    print(f"Сессия: {sess['id']} | Флоу: {sess['flow_name']} | Статус: {sess['status']}")
    if res.get("current_stage"):
        st = res["current_stage"]
        print(f"Текущий этап: {st['title']} [{st['id']}] (итерация {sess['iteration_count']})")
    return 0


def _flow_list(parsed: argparse.Namespace) -> int:
    res = list_flows(project_root=parsed.project_root)
    print(f"Доступно сценариев: {res['count']}")
    for f in res.get("flows", []):
        print(
            f"• {f['name']} ({f.get('source', '')}): {f.get('title', '')} "
            f"[{f.get('stages_count', 0)} этапов]"
        )
        if f.get("description"):
            print(f"  {f['description']}")
    return 0


def _flow_resolve(parsed: argparse.Namespace) -> int:
    res = resolve_flow_escalation(
        session_id=parsed.session_id,
        resolution=parsed.resolution,
        notes=parsed.notes,
        project_root=parsed.project_root,
        target=parsed.target,
    )
    print(f"Резолюция '{parsed.resolution}' применена. Статус: {res.get('status')}")
    return 0


def _flow_abort(parsed: argparse.Namespace) -> int:
    res = abort_flow(
        session_id=parsed.session_id,
        reason=parsed.reason,
        project_root=parsed.project_root,
    )
    print(f"Сессия {parsed.session_id} прервана: {res.get('message')}")
    return 0


def _flow_retry(parsed: argparse.Namespace) -> int:
    """flow retry: повтор этапа через фасад step_flow (None-target сохраняет сессию)."""
    from mcp_skills.flow_engine import step_flow

    res = step_flow(
        action="retry",
        session_id=parsed.session_id,
        notes=parsed.notes,
        target=parsed.target,
        project_root=parsed.project_root,
    )
    stage = res.get("stage", {})
    print(f"🔁 Повтор этапа: {stage.get('title', '?')} [{stage.get('id', '?')}]")
    print(f"\n{stage.get('instruction', '')}")
    if res.get("subagent"):
        sub = res["subagent"]
        print(f"\n🤖 Назначен агент: {sub['name']}")
    return 0


def _handle_flow(parsed: argparse.Namespace) -> int:
    handlers = {
        "start": _flow_start,
        "advance": _flow_advance,
        "status": _flow_status,
        "list": _flow_list,
        "resolve": _flow_resolve,
        "retry": _flow_retry,
        "abort": _flow_abort,
    }
    handler = handlers.get(parsed.flow_cmd)
    if handler:
        return handler(parsed)
    print("Неизвестная подкоманда flow. Используйте --help.")
    return 1


def _build_flow_parser(
    subparsers: Any, parent_parser: argparse.ArgumentParser | None = None
) -> None:
    parents = [parent_parser] if parent_parser else []
    flow_p = subparsers.add_parser(
        "flow", parents=parents, help="Управление стейт-машиной воркфлоу (Flow State Machine)"
    )
    flow_subs = flow_p.add_subparsers(dest="flow_cmd", help="Команды воркфлоу")

    f_start = flow_subs.add_parser("start", parents=parents, help="Запустить сессию флоу")
    f_start.add_argument("name", help="Имя сценария (например, feature-delivery)")
    f_start.add_argument("--task-id", help="Идентификатор задачи")
    f_start.add_argument(
        "--target",
        default=None,
        choices=list(SUPPORTED_TARGETS),
        help="Целевой рантайм агентов (по умолчанию: antigravity; R5: None не сбрасывает target существующей сессии)",
    )

    f_advance = flow_subs.add_parser("advance", parents=parents, help="Продвинуть этап флоу")
    f_advance.add_argument("session_id", help="ID сессии")
    f_advance.add_argument("--deliverable", help="Результат этапа")
    f_advance.add_argument("--verdict", choices=["approved", "needs_work"], help="Вердикт ревьюера")
    f_advance.add_argument("--notes", default="", help="Комментарии к переходу")
    f_advance.add_argument(
        "--target",
        default=None,
        choices=list(SUPPORTED_TARGETS),
        help="Целевой рантайм агентов (None = сохранить target сессии, R5)",
    )

    f_status = flow_subs.add_parser("status", parents=parents, help="Статус сессии флоу")
    f_status.add_argument("session_id", nargs="?", default=None, help="ID сессии (опционально)")
    f_status.add_argument(
        "--target",
        default=None,
        choices=list(SUPPORTED_TARGETS),
        help="Целевой рантайм агентов (None = сохранить target сессии, R5)",
    )

    flow_subs.add_parser("list", parents=parents, help="Список доступных сценариев")

    f_resolve = flow_subs.add_parser(
        "resolve", parents=parents, help="Снять блокировку Circuit Breaker"
    )
    f_resolve.add_argument("session_id", help="ID сессии")
    f_resolve.add_argument(
        "resolution",
        choices=["override_approved", "retry", "abort"],
        help="Решение по эскалации",
    )
    f_resolve.add_argument("--notes", default="", help="Пояснение к решению")
    f_resolve.add_argument(
        "--target",
        default=None,
        choices=list(SUPPORTED_TARGETS),
        help="Целевой рантайм агентов (None = сохранить target сессии, R5)",
    )

    f_retry = flow_subs.add_parser(
        "retry", parents=parents, help="Повторить текущий этап сессии (без расхода бюджета)"
    )
    f_retry.add_argument("session_id", help="ID сессии")
    f_retry.add_argument("--notes", default="", help="Комментарии к повтору")
    f_retry.add_argument(
        "--target",
        default=None,
        choices=list(SUPPORTED_TARGETS),
        help="Целевой рантайм агентов (None = сохранить target сессии; явное значение переключает, R5)",
    )

    f_abort = flow_subs.add_parser("abort", parents=parents, help="Прервать сессию флоу")
    f_abort.add_argument("session_id", help="ID сессии")
    f_abort.add_argument("--reason", default="", help="Причина аборта")


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI mcp-skills."""
    args_list = sys.argv[1:] if argv is None else argv

    if not args_list or "--stdio" in args_list:
        run_server()
        return 0

    root_parser = argparse.ArgumentParser(add_help=False)
    root_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Путь к корню целевого проекта",
    )

    parser = argparse.ArgumentParser(
        prog="mcp-skills",
        parents=[root_parser],
        description="CLI инструмент для поиска и сборки скиллов и управления флоу",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Команды")

    _build_flow_parser(subparsers, parent_parser=root_parser)

    # categories
    subparsers.add_parser(
        "categories",
        parents=[root_parser],
        help="Показать список категорий",
    )

    # search
    search_p = subparsers.add_parser("search", parents=[root_parser], help="Поиск скиллов")
    search_p.add_argument("query", nargs="?", default="", help="Поисковый запрос")
    search_p.add_argument(
        "--type",
        dest="search_type",
        choices=["all", "skill", "flow"],
        default="all",
        help="Тип поиска (all, skill, flow)",
    )
    search_p.add_argument(
        "--category",
        dest="category",
        help="Фильтр по категории",
    )
    search_p.add_argument("--limit", type=int, default=10, help="Количество результатов")

    # load
    load_p = subparsers.add_parser("load", parents=[root_parser], help="Собрать и загрузить скилл")
    load_p.add_argument("name", help="Имя скилла")
    load_p.add_argument(
        "--stack",
        "--touches",
        dest="stack",
        help="Задействованный стек/слоты через запятую (например, ui,backend; ранее --touches)",
    )

    # agent
    agent_p = subparsers.add_parser(
        "agent", parents=[root_parser], help="Собрать или установить профиль субагента"
    )
    agent_p.add_argument("name", help="Имя субагента")
    agent_p.add_argument(
        "--target",
        default="antigravity",
        choices=list(SUPPORTED_TARGETS),
        help=f"Целевой рантайм ({', '.join(SUPPORTED_TARGETS)})",
    )
    agent_p.add_argument(
        "--project",
        default=None,
        help="Имя проекта (по умолчанию автоопределение)",
    )
    agent_p.add_argument(
        "--install",
        action="store_true",
        help="Установить артефакты агента в локальное рабочее пространство",
    )

    # install
    install_p = subparsers.add_parser(
        "install",
        parents=[root_parser],
        help="Установка MCP сервера в клиенты (Claude, Antigravity, OpenCode, Cursor)",
    )
    install_p.add_argument(
        "--target",
        default="antigravity",
        choices=list(SUPPORTED_TARGETS),
        help="Целевой AI-клиент (по умолчанию: antigravity)",
    )
    install_p.add_argument(
        "--eager",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Включить Eager-режим для skills-engine (по умолчанию: True, используйте --no-eager для Lazy)",
    )
    install_p.add_argument(
        "--skill",
        action="store_true",
        default=False,
        help="Установить скиллы в проект",
    )
    install_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Принудительно перезаписать файлы",
    )
    install_p.add_argument(
        "--reinstall",
        action="store_true",
        default=False,
        help="Переустановить сервер и обновить схемы",
    )
    install_p.add_argument(
        "--antigravity",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Настроить Antigravity",
    )

    parsed = parser.parse_args(args_list)

    if parsed.subcommand == "flow":
        return _handle_flow(parsed)

    if parsed.subcommand == "categories":
        from mcp_skills.server import get_storage

        store = get_storage(parsed.project_root) if parsed.project_root else None
        print(get_categories_prompt(store))
        return 0

    if parsed.subcommand == "search":
        return _handle_search(parsed)

    if parsed.subcommand == "load":
        return _handle_load(parsed)

    if parsed.subcommand == "agent":
        return _handle_agent(parsed)

    if parsed.subcommand == "install":
        from mcp_skills.installer import install_skills_server, install_skills_skill

        root = parsed.project_root
        if parsed.antigravity and parsed.target == "antigravity":
            install_skills_server(
                target="antigravity",
                project_root=root,
                eager=parsed.eager,
                reinstall=parsed.reinstall,
            )
        elif parsed.target != "antigravity":
            install_skills_server(
                target=parsed.target,
                project_root=root,
                reinstall=parsed.reinstall,
            )

        if parsed.skill:
            install_skills_skill(root, force=parsed.force)

        mode_str = (
            "Eager (нативные функции)"
            if parsed.eager
            else "Lazy (по требованию через call_mcp_tool)"
        )
        print(f"\n🎉 Установка завершена! Сервер настроен как {mode_str}.")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
