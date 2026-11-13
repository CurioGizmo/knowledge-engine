"""Универсальный модуль установки и регистрации MCP-серверов в AI-клиенты."""

from __future__ import annotations

import contextlib
import importlib.resources
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp_core.paths import _find_default_project_root

__all__ = [
    "get_antigravity_server_config",
    "get_server_command",
    "install_all_plugins",
    "install_antigravity_plugin",
    "install_antigravity_server",
    "install_file",
    "install_skill",
    "load_template_content",
    "run_all_installers",
    "update_json_config",
]


def load_template_content(relative_path: str) -> str:
    """Загружает шаблон из пакета mcp_core через importlib.resources или прямого пути на диске."""
    # 1. Попытка через importlib.resources (при установке wheel/пакета)
    with contextlib.suppress(Exception):
        ref = importlib.resources.files("mcp_core").joinpath("templates", relative_path)
        if ref.is_file():
            return ref.read_text(encoding="utf-8")

    # 2. Попытка через каталог пакета на диске
    pkg_template = Path(__file__).parent / "templates" / relative_path
    if pkg_template.is_file():
        return pkg_template.read_text(encoding="utf-8")

    # 3. Попытка через корень репозитория templates/
    root_template = _find_default_project_root() / "templates" / relative_path
    if root_template.is_file():
        return root_template.read_text(encoding="utf-8")

    msg = f"Шаблон '{relative_path}' не найден в mcp_core/templates"
    raise FileNotFoundError(msg)


def install_skill(
    skill_name: str,
    project_root: Path | None = None,
    force: bool = False,
) -> Path:
    """Универсально устанавливает скилл из шаблона templates/skills/<skill_name>/SKILL.md."""
    content = load_template_content(f"skills/{skill_name}/SKILL.md")
    root = project_root or _find_default_project_root()
    target_path = root / ".agents" / "skills" / skill_name / "SKILL.md"
    return install_file(target_path, content, force=force, label=f"Скилл {skill_name}")


def get_server_command(binary_name: str, module_fallback: str) -> tuple[str, list[str]]:
    """Определяет наилучшую команду запуска MCP сервера (binary или sys.executable -m module)."""
    binary = shutil.which(binary_name)
    if binary:
        return binary, []
    venv_bin = Path(sys.prefix) / "bin" / binary_name
    if venv_bin.exists():
        return str(venv_bin), []
    return sys.executable, ["-m", module_fallback]


def get_antigravity_server_config(
    binary_name: str,
    module_fallback: str,
    eager: bool = True,
    tools: list[str] | None = None,
    project_root: Path | None = None,
    multi_tenant: bool | None = None,
) -> dict[str, Any]:
    """Формирует JSON-конфигурацию сервера для Antigravity с поддержкой Eager и Lazy режимов.

    По умолчанию, если project_root не передан (None), используется multi_tenant=True
    (глобальная регистрация сервера без хардкода корня: args=['--multi-tenant']).
    При явной передаче project_root (или multi_tenant=False) используется --project-root <path>.
    """
    command, cmd_args = get_server_command(binary_name, module_fallback)
    is_mt = multi_tenant if multi_tenant is not None else (project_root is None)
    if is_mt:
        args = [*cmd_args, "--multi-tenant"]
    else:
        root = (project_root or _find_default_project_root()).resolve()
        args = [*cmd_args, "--project-root", str(root)]
    config: dict[str, Any] = {"command": command, "args": args}

    if eager:
        config["force_all_tools_eager"] = True
        if tools:
            config["tools"] = {tool_name: {"eager": True} for tool_name in tools}
    else:
        # Для Lazy режима не выставляем force_all_tools_eager
        config["force_all_tools_eager"] = False

    return config


def update_json_config(
    path: Path,
    server_key: str,
    server_config: dict[str, Any],
    migrate_keys: list[str] | None = None,
) -> bool:
    """Безопасно обновляет mcp_config.json с сохранением существующей структуры и созданием .bak."""
    data: dict[str, Any] = {"mcpServers": {}}
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
            backup = path.with_suffix(".json.bak")
            if not backup.exists():
                shutil.copy2(path, backup)
        except Exception as e:
            print(f"⚠️  Ошибка чтения существующего {path}: {e}. Создаём резервную копию.")
            backup = path.with_suffix(".json.bak")
            if not backup.exists():
                with contextlib.suppress(Exception):
                    shutil.copy2(path, backup)
            data = {"mcpServers": {}}

    if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
        data["mcpServers"] = {}

    if migrate_keys:
        for old_key in migrate_keys:
            if old_key in data["mcpServers"] and old_key != server_key:
                del data["mcpServers"][old_key]
                print(
                    f"⚠️  Миграция: устаревший ключ сервера '{old_key}' удалён из {path} в пользу '{server_key}'."
                )

    data["mcpServers"][server_key] = server_config
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(f".tmp.{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    temp_path.replace(path)
    return True


def install_antigravity_server(
    server_key: str,
    server_config: dict[str, Any],
    instructions: str | None = None,
    tool_schemas: dict[str, dict[str, Any]] | None = None,
    home_dir: Path | None = None,
    migrate_keys: list[str] | None = None,
) -> list[Path]:
    """Устанавливает конфигурацию MCP-сервера в профили Antigravity (global + legacy) и генерирует схемы."""
    home = home_dir or Path.home()
    target_paths = [
        home / ".gemini" / "config" / "mcp_config.json",
        home / ".gemini" / "antigravity" / "mcp_config.json",
    ]

    installed = []
    for path in target_paths:
        try:
            update_json_config(path, server_key, server_config, migrate_keys=migrate_keys)
            installed.append(path)
            eager_str = "Eager" if server_config.get("force_all_tools_eager") else "Lazy"
            print(f"✅ Настроен {eager_str} MCP сервер [{server_key}] в Antigravity: {path}")
        except Exception as e:
            print(f"❌ Не удалось обновить {path}: {e}")

    # Удаление устаревших каталогов схем при миграции
    if migrate_keys:
        for old_key in migrate_keys:
            old_mcp_dir = home / ".gemini" / "antigravity" / "mcp" / old_key
            if old_mcp_dir.exists() and old_key != server_key:
                try:
                    shutil.rmtree(old_mcp_dir)
                    print(f"ℹ️  Удалён устаревший каталог схем {old_mcp_dir}")
                except Exception as e:
                    print(f"⚠️  Не удалось удалить устаревший каталог {old_mcp_dir}: {e}")

    # Записываем инструкции и схемы тулов в каталог MCP
    server_mcp_dir = home / ".gemini" / "antigravity" / "mcp" / server_key
    try:
        server_mcp_dir.mkdir(parents=True, exist_ok=True)
        if instructions:
            inst_path = server_mcp_dir / "instructions.md"
            if inst_path.exists():
                backup_inst = inst_path.with_suffix(".md.bak")
                if not backup_inst.exists():
                    shutil.copy2(inst_path, backup_inst)
            with inst_path.open("w", encoding="utf-8") as f:
                f.write(instructions)
            print(f"✅ Обновлены инструкции MCP: {inst_path}")

        if tool_schemas:
            for old_schema in server_mcp_dir.glob("*.json"):
                if old_schema.stem not in tool_schemas:
                    old_schema.unlink(missing_ok=True)
                    print(f"🧹 Удалена устаревшая схема: {old_schema.name}")
            for tool_name, schema_data in tool_schemas.items():
                schema_path = server_mcp_dir / f"{tool_name}.json"
                with schema_path.open("w", encoding="utf-8") as f:
                    json.dump(schema_data, f, indent=2, ensure_ascii=False)
                print(f"✅ Обновлена схема инструмента: {schema_path}")
    except Exception as e:
        print(f"⚠️ Не удалось записать instructions/schema для {server_key}: {e}")

    return installed


def install_file(
    target_path: Path,
    content: str,
    force: bool = False,
    label: str = "Файл",
) -> Path:
    """Универсально создает/обновляет файл конфигурации с защитой от перезаписи и бэкапом."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        existing_content = target_path.read_text(encoding="utf-8")
        if existing_content.strip() == content.strip():
            print(f"ℹ️  {label} уже актуален: {target_path}")
            return target_path
        if not force:
            print(
                f"⚠️  Файл {target_path} уже существует и содержит пользовательские изменения. "
                "Перезапись пропущена (используйте флаг --force)."
            )
            return target_path

        backup_path = target_path.with_suffix(f"{target_path.suffix}.bak")
        if not backup_path.exists():
            shutil.copy2(target_path, backup_path)
            print(f"ℹ️  Создана резервная копия существующего файла: {backup_path}")

    with target_path.open("w", encoding="utf-8") as f:
        f.write(content)

    print(f"✅ Создан {label.lower()}: {target_path}")
    return target_path


def _plugin_source_dir(plugin_name: str, plugins_source_dir: Path | None) -> Path | None:
    """Находит каталог плагина по явному пути, через importlib.resources или в checkout.

    Порядок:
    1. ``plugins_source_dir`` — явный каталог (тесты и legacy-вызовы).
    2. ``importlib.resources`` пакета ``mcp_plugins`` — работает в wheel вне checkout,
       независимо от cwd.
    3. ``<repo-root>/src/plugins`` — checkout (source-only окружения).
    """
    if plugins_source_dir is not None:
        return plugins_source_dir / plugin_name

    with contextlib.suppress(Exception):
        ref = importlib.resources.files("mcp_plugins").joinpath(plugin_name)
        if ref.is_dir():
            # Путь реальной файловой системы для copytree/symlink
            return Path(str(ref))

    source_dir = _find_default_project_root() / "src" / "plugins" / plugin_name
    return source_dir


def install_antigravity_plugin(
    plugin_name: str,
    home_dir: Path | None = None,
    plugins_source_dir: Path | None = None,
    symlink: bool = False,
) -> Path:
    """Устанавливает или линкует плагин в ~/.gemini/config/plugins/<plugin_name>.

    Повторная установка не перезаписывает пользовательские правки: файлы,
    отличающиеся от эталона, пропускаются (как в :func:`install_file` без
    ``force``); отсутствующие и неизменённые файлы обновляются.
    """
    home = home_dir or Path.home()
    target_dir = home / ".gemini" / "config" / "plugins" / plugin_name
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    source_dir = _plugin_source_dir(plugin_name, plugins_source_dir)
    if source_dir is None or not source_dir.is_dir():
        msg = f"Каталог плагина '{source_dir}' не найден"
        raise FileNotFoundError(msg)

    if symlink:
        if target_dir.exists() or target_dir.is_symlink():
            _remove_existing_target(target_dir)
        target_dir.symlink_to(source_dir)
        print(f"✅ Создан symlink плагина: {target_dir} -> {source_dir}")
        return target_dir

    # Copy file-by-file: keep user-modified files intact on reinstall.
    for src_file in sorted(source_dir.rglob("*")):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(source_dir)
        dst = target_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            # MINOR-4: файл с текстовым суффиксом, но нечитаемый как utf-8 (или
            # бинарным содержимым), не должен ронять установку — сравнение
            # пропускается, копирование идёт как есть (shutil.copy2).
            try:
                existing = dst.read_text(encoding="utf-8") if _is_text_file(rel) else None
                if existing is not None and existing != src_file.read_text(encoding="utf-8"):
                    print(
                        f"⚠️  Файл {dst} содержит пользовательские изменения. Перезапись пропущена."
                    )
                    continue
            except (UnicodeDecodeError, OSError):
                print(f"⚠️  Файл {dst} нечитаем как текст — копируется без сравнения.")
        shutil.copy2(src_file, dst)

    # Note: files added by the user inside the plugin directory are never
    # deleted on reinstall (data safety over cleanup).
    print(f"✅ Установлен плагин: {target_dir}")
    return target_dir


def _is_text_file(rel: Path) -> bool:
    return rel.suffix in {".json", ".md", ".txt", ".yaml", ".yml", ".toml"}


def _remove_existing_target(target_dir: Path) -> None:
    if target_dir.is_symlink():
        target_dir.unlink()
    else:
        shutil.rmtree(target_dir)


def install_all_plugins(
    home_dir: Path | None = None,
    plugins_source_dir: Path | None = None,
    symlink: bool = False,
) -> list[Path]:
    """Устанавливает все 3 плагина платформы в ~/.gemini/config/plugins/."""
    plugin_names = [
        "knowledge-engine-plugin",
        "skills-engine-plugin",
        "audit-engine-plugin",
    ]
    installed: list[Path] = []
    for name in plugin_names:
        p = install_antigravity_plugin(
            name,
            home_dir=home_dir,
            plugins_source_dir=plugins_source_dir,
            symlink=symlink,
        )
        installed.append(p)
    return installed


def run_all_installers(args: list[str] | None = None) -> None:
    """Универсальная установка всех трех серверов платформы (knowledge, audit, skills)."""
    from mcp_audit.installer import run_installer as run_audit_installer
    from mcp_knowledge.installer import run_installer as run_knowledge_installer
    from mcp_skills.installer import run_installer as run_skills_installer

    cmd_args = sys.argv[1:] if args is None else args
    print("=" * 60)
    print("🚀 Установка MCP Knowledge Engine Platform (all 3 engines)...")
    print("=" * 60)
    print("\n[1/3] Настройка MCP Knowledge Engine...")
    run_knowledge_installer(cmd_args)
    print("\n[2/3] Настройка MCP Audit Engine...")
    run_audit_installer(cmd_args)
    print("\n[3/3] Настройка MCP Skills Engine...")
    run_skills_installer(cmd_args)
    print("\n" + "=" * 60)
    print("🎉 Все три MCP-сервера успешно настроены и готовы к работе!")
    print("=" * 60)
