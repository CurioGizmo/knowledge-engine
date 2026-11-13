"""Автоматическое определение стека технологий проекта по манифестам."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from mcp_core.paths import _find_default_project_root
from mcp_skills.spam_gate import is_safe_name

__all__ = ["detect_canonical_project_name", "detect_mode", "detect_project_stack"]

_PYTHON_PACKAGES: tuple[tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("django", "django"),
    ("flask", "flask"),
    ("torch", "pytorch"),
    ("pytorch", "pytorch"),
    ("pydantic", "pydantic"),
    ("sqlalchemy", "sqlalchemy"),
    ("pytest", "pytest"),
    ("sqlite3", "sqlite"),
    ("sqlite", "sqlite"),
    ("aiosqlite", "sqlite"),
    ("pysqlite3", "sqlite"),
    ("faster-whisper", "audio-ml"),
    ("openai-whisper", "audio-ml"),
    ("whisper", "audio-ml"),
    ("torchaudio", "audio-ml"),
    ("soundfile", "audio-ml"),
    ("librosa", "audio-ml"),
    ("pyannote-audio", "audio-ml"),
)

_NODE_PACKAGES: tuple[tuple[str, str], ...] = (
    ("typescript", "typescript"),
    ("react", "react"),
    ("react-dom", "react"),
    ("next", "nextjs"),
    ("vue", "vue"),
    ("tailwindcss", "tailwind"),
    ("electron", "electron"),
    ("@tauri-apps/api", "tauri"),
    ("@tauri-apps/cli", "tauri"),
    ("tauri", "tauri"),
    ("better-sqlite3", "sqlite"),
    ("sqlite3", "sqlite"),
)


def _extract_toml_dependencies(data: dict[str, Any]) -> list[str]:
    """Собирает имена зависимостей из стандартных секций pyproject.toml.

    Поддерживает PEP 621 ``[project].dependencies`` (строки вида "pkg>=1.0"),
    опциональные группы ``[project.optional-dependencies]`` и легаси-секции
    ``[tool.poetry.dependencies]`` (таблица pkg -> constraint).
    """
    deps: list[str] = []

    project = data.get("project")
    if isinstance(project, dict):
        base = project.get("dependencies")
        if isinstance(base, list):
            deps.extend(str(item) for item in base)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    deps.extend(str(item) for item in group)

    uv = data.get("tool", {}).get("uv")
    if isinstance(uv, dict):
        sources = uv.get("sources")
        if isinstance(sources, dict):
            deps.extend(str(name) for name in sources)

    poetry = data.get("tool", {}).get("poetry")
    if isinstance(poetry, dict):
        poetry_deps = poetry.get("dependencies")
        if isinstance(poetry_deps, dict):
            deps.extend(str(name) for name in poetry_deps)
        poetry_dev = poetry.get("dev-dependencies") if isinstance(poetry, dict) else None
        if isinstance(poetry_dev, dict):
            deps.extend(str(name) for name in poetry_dev)

    return deps


def _normalize_dep_name(dep: str) -> str:
    """Выделяет имя пакета из PEP 508-строки зависимости (``torch>=2.0`` -> ``torch``)."""
    match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", dep.strip())
    name = (match.group(1) if match else dep.strip()).lower()
    return name.replace("_", "-")


def _detect_python(root: Path) -> set[str]:
    stack: set[str] = set()
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return stack

    stack.add("python")
    try:
        content = pyproject.read_text(encoding="utf-8")
    except OSError:
        return stack

    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        # Повреждённый TOML: фолбэк на substring-скан, чтобы не терять стек
        lowered = content.lower()
        for pkg, tag in _PYTHON_PACKAGES:
            if pkg in lowered:
                stack.add(tag)
        return stack

    dep_names = {_normalize_dep_name(d) for d in _extract_toml_dependencies(data)}
    for pkg, tag in _PYTHON_PACKAGES:
        if pkg in dep_names:
            stack.add(tag)
    if any("sqlite" in dep for dep in dep_names):
        stack.add("sqlite")
    return stack


_EXCLUDED_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "target",
        "out",
        "__pycache__",
        ".git",
        ".agents",
        ".cache",
    }
)
_WORKSPACE_CONTAINER_DIRS = frozenset({"apps", "packages", "services", "modules"})


def _find_candidate_package_jsons(root: Path) -> list[Path]:
    """Находит файлы package.json в корне и ограниченных workspace-подкаталогах.

    Сканирует:
    1. root / package.json;
    2. Прямые подкаталоги (frontend/, ui/, web/, client/ и др.);
    3. Подкаталоги 2-го уровня в workspace-контейнерах (apps/*, packages/*, services/*, modules/*).
    Исключает node_modules, скрытые директории, build/dist/target и venv.
    """
    candidates: list[Path] = []
    root_pkg = root / "package.json"
    if root_pkg.is_file():
        candidates.append(root_pkg)

    try:
        for p in root.iterdir():
            if not p.is_dir() or p.name.startswith(".") or p.name in _EXCLUDED_DIR_NAMES:
                continue
            sub_pkg = p / "package.json"
            if sub_pkg.is_file():
                candidates.append(sub_pkg)
            if p.name in _WORKSPACE_CONTAINER_DIRS:
                try:
                    for sub in p.iterdir():
                        if (
                            not sub.is_dir()
                            or sub.name.startswith(".")
                            or sub.name in _EXCLUDED_DIR_NAMES
                        ):
                            continue
                        nested_pkg = sub / "package.json"
                        if nested_pkg.is_file():
                            candidates.append(nested_pkg)
                except OSError:
                    continue
    except OSError:
        pass
    return candidates


def _parse_package_json(pkg_json: Path, stack: set[str]) -> None:
    """Парсит один package.json и наполняет множество стека найденными технологиями."""
    stack.add("node")
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        # `dependencies: null` встречается в реальных package.json (npm-артефакт):
        # dict(**None) роняет TypeError — нормализуем null/не-dict к пустому словарю.
        deps: dict[str, Any] = {
            **(data.get("dependencies") or {}),
            **(data.get("devDependencies") or {}),
        }
        deps_keys = {str(k).lower() for k in deps}
        for pkg, tag in _NODE_PACKAGES:
            if pkg in deps_keys:
                stack.add(tag)
        if any("sqlite" in k for k in deps_keys):
            stack.add("sqlite")
    except (OSError, ValueError, TypeError):
        pass


def _detect_node(root: Path) -> set[str]:
    stack: set[str] = set()
    for pkg_json in _find_candidate_package_jsons(root):
        _parse_package_json(pkg_json, stack)
    return stack


def detect_project_stack(project_root: Path | None = None) -> list[str]:
    """Сканирует проектные манифесты и возвращает нормализованный список технологий."""
    root = project_root or _find_default_project_root()
    stack: set[str] = set()

    stack.update(_detect_python(root))
    stack.update(_detect_node(root))

    # Tauri / Rust в src-tauri или корне
    if (
        (root / "src-tauri" / "Cargo.toml").is_file()
        or (root / "src-tauri" / "tauri.conf.json").is_file()
        or (root / "tauri.conf.json").is_file()
    ):
        stack.add("tauri")
        stack.add("rust")

    for cargo_path in (root / "src-tauri" / "Cargo.toml", root / "Cargo.toml"):
        if cargo_path.is_file():
            stack.add("rust")
            try:
                content = cargo_path.read_text(encoding="utf-8")
                if "tauri" in content:
                    stack.add("tauri")
                if "rusqlite" in content or "sqlite" in content:
                    stack.add("sqlite")
            except OSError:
                pass

    # SQLite: файлы в корне (исключая внутренние skills.db / knowledge.db)
    if "sqlite" not in stack:
        try:
            for p in root.iterdir():
                if (
                    p.is_file()
                    and p.name not in ("skills.db", "knowledge.db")
                    and (
                        p.suffix in (".sqlite", ".sqlite3")
                        or (p.suffix == ".db" and not p.name.endswith("_fts.db"))
                    )
                ):
                    stack.add("sqlite")
                    break
        except OSError:
            pass

    if (root / "go.mod").is_file():
        stack.add("go")

    return sorted(stack)


def detect_mode(project_root: Path | None = None) -> str | None:
    """Определяет режим работы: 'mcp' / 'offline' / None (неизвестно).

    Приоритет:
    1. MCP_ACTIVE_SERVERS: задан и непуст -> 'mcp'; задан и явно пуст -> 'offline'.
    2. Если MCP_ACTIVE_SERVERS не задан -> None (неизвестно).
       Файлы .agents/* являются признаком данных/артефактов, но НЕ доказательством
       доступности или активности работающего MCP-сервера.
    """
    env_value = os.environ.get("MCP_ACTIVE_SERVERS")
    if env_value is not None:
        return "mcp" if env_value.strip() else "offline"

    return None


def _extract_repo_name_from_git_url(url: str) -> str | None:
    """Извлекает чистое имя репозитория из git URL (HTTPS, SSH, SCP-like)."""
    clean = url.strip()
    if not clean:
        return None
    if clean.endswith(".git"):
        clean = clean[:-4]
    clean = clean.rstrip("/")
    clean = clean.replace(":", "/")
    parts = [p for p in clean.split("/") if p]
    if not parts:
        return None
    return parts[-1] or None


def _sanitize_project_name(name: str) -> str:
    """Санитизирует имя проекта: lower-case, замена недопустимых символов на дефис."""
    if "/" in name:
        name = name.split("/")[-1]
    name = name.lower()
    name = re.sub(r"[^a-z0-9_\-]+", "-", name)
    name = name.strip("-_")
    return name[:64]


def _detect_name_from_git(root: Path) -> str | None:
    """Извлекает имя проекта из git remote origin/upstream url."""
    if not (root / ".git").exists():
        return None
    for remote in ("origin", "upstream"):
        with contextlib.suppress(Exception):
            proc = subprocess.run(
                ["git", "config", "--get", f"remote.{remote}.url"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                raw_name = _extract_repo_name_from_git_url(proc.stdout.strip())
                if raw_name:
                    sanitized = _sanitize_project_name(raw_name)
                    if is_safe_name(sanitized):
                        return sanitized
    return None


def _detect_name_from_manifests(root: Path) -> str | None:
    """Извлекает имя проекта из манифестов pyproject.toml, package.json или Cargo.toml."""
    # 1. pyproject.toml
    pyproject_path = root / "pyproject.toml"
    if pyproject_path.is_file():
        with contextlib.suppress(Exception):
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            name = data.get("project", {}).get("name")
            if isinstance(name, str):
                sanitized = _sanitize_project_name(name)
                if is_safe_name(sanitized):
                    return sanitized

    # 2. package.json
    pkg_path = root / "package.json"
    if pkg_path.is_file():
        with contextlib.suppress(Exception):
            pkg_data = json.loads(pkg_path.read_text(encoding="utf-8"))
            name = pkg_data.get("name")
            if isinstance(name, str):
                sanitized = _sanitize_project_name(name)
                if is_safe_name(sanitized):
                    return sanitized

    # 3. Cargo.toml
    cargo_path = root / "Cargo.toml"
    if cargo_path.is_file():
        with contextlib.suppress(Exception):
            cargo_data = tomllib.loads(cargo_path.read_text(encoding="utf-8"))
            name = cargo_data.get("package", {}).get("name")
            if isinstance(name, str):
                sanitized = _sanitize_project_name(name)
                if is_safe_name(sanitized):
                    return sanitized

    return None


def detect_canonical_project_name(project_root: Path | None = None) -> str:
    """Детерминированно вычисляет каноническое имя проекта.

    Приоритет:
    1. Имя репозитория из git remote (origin или upstream).
    2. Имя из манифеста pyproject.toml [project].name или package.json "name".
    3. Имя из Cargo.toml [package].name.
    4. Fallback: имя папки project_root.name.
    """
    root = project_root or _find_default_project_root()

    from_git = _detect_name_from_git(root)
    if from_git:
        return from_git

    from_manifest = _detect_name_from_manifests(root)
    if from_manifest:
        return from_manifest

    sanitized = _sanitize_project_name(root.name)
    return sanitized if is_safe_name(sanitized) else "project"
