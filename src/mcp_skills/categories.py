"""Справочник категорий скиллов и валидация."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from mcp_core.paths import resolve_skills_repo_dir

if TYPE_CHECKING:
    from mcp_skills.storage import SkillsStorage

__all__ = [
    "CATEGORIES",
    "get_categories_dict",
    "get_categories_prompt",
    "get_category_keywords",
    "is_valid_category",
    "load_categories_data",
]

logger = logging.getLogger(__name__)

DEFAULT_CATEGORIES: dict[str, str] = {
    "planning": "Планы, архитектура, декомпозиция задач, критерии приёмки",
    "audio-ml": "Аудио, ML/DL пайплайны, Whisper, PyTorch, FFmpeg, DSP",
    "desktop": "Qt, Electron, Tauri, десктопные UI компоненты и нативные окна",
    "frontend": "React, Next.js, TypeScript, Tailwind, Web APIs, CSS",
    "backend-db": "FastAPI, PostgreSQL, SQLite, SQLAlchemy, Docker, Redis",
    "quality-review": "Код-ревью, аудит безопасности, линтинг, тесты, инварианты",
    "general": "Общесистемные утилиты, пакетные менеджеры, автоматизация и CI/CD",
}

DEFAULT_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "backend-db": ["бэкенд", "backend", "хранилище", "api", "сервер", "база"],
    "frontend": ["ui", "frontend", "компонент", "стили", "верстка", "интерфейс"],
    "audio-ml": ["аудио", "audio", "звук", "ml", "whisper", "голос", "модель"],
    "desktop": ["desktop", "десктоп", "окно", "tauri", "electron", "qt"],
    "planning": ["план", "декомпозиция", "архитектура", "сценарий", "roadmap"],
    "quality-review": ["ревью", "код-ревью", "review", "тест", "аудит", "безопасность"],
    "general": ["утилиты", "ci", "cd", "bash", "cli"],
}

# Обратная совместимость для внешних потребителей CATEGORIES
CATEGORIES: dict[str, str] = DEFAULT_CATEGORIES

# Изолированный кэш по пути: {yaml_path.resolve(): (signature, categories_data)}.
# signature = (st_mtime_ns, st_size); данные всегда соответствуют сохранённой сигнатуре.
_CATEGORIES_CACHE: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def _file_signature(stat_result: Any) -> tuple[int, int]:
    """Возвращает сигнатуру файла (mtime_ns, size) для проверки актуальности кэша."""
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _default_categories_data() -> dict[str, dict[str, Any]]:
    """Строит fallback-структуру категорий из DEFAULT_CATEGORIES."""
    return {
        cat: {
            "title": cat.title(),
            "description": desc,
            "keywords": DEFAULT_CATEGORY_KEYWORDS.get(cat, []),
        }
        for cat, desc in DEFAULT_CATEGORIES.items()
    }


def load_categories_data(skills_repo_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Загружает категории из docs/categories.yaml с кэшированием по пути.

    Кэш изолирован по ключу ``yaml_path.resolve()``: разные каталоги с разными
    categories.yaml (даже с совпадающим mtime) получают независимые записи.
    Актуальность записи проверяется сигнатурой ``(st_mtime_ns, st_size)``.
    Потокобезопасно: чтение файла и парсинг YAML выполняются вне lock; запись
    в кэш — под lock, и только если сигнатуры до и после чтения совпадают
    (файл не изменился во время чтения).
    """
    try:
        repo_dir = resolve_skills_repo_dir(skills_repo_dir)
        yaml_path = (repo_dir / "docs" / "categories.yaml").resolve()
        if not yaml_path.is_file():
            return _default_categories_data()

        stat_before = yaml_path.stat()
        signature_before = _file_signature(stat_before)

        with _CACHE_LOCK:
            cached = _CATEGORIES_CACHE.get(yaml_path)
        if cached is not None and cached[0] == signature_before:
            return cached[1]

        # Чтение и парсинг вне lock (блокирующие операции).
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        cats = raw.get("categories", {}) if isinstance(raw, dict) else {}
        result: dict[str, dict[str, Any]] = {}
        for k, v in cats.items():
            if isinstance(v, dict):
                result[str(k).strip().lower()] = {
                    "title": str(v.get("title", k)),
                    "description": str(v.get("description", "")),
                    "keywords": list(v.get("keywords", [])),
                }
            elif isinstance(v, str):
                result[str(k).strip().lower()] = {
                    "title": str(k).title(),
                    "description": v,
                    "keywords": DEFAULT_CATEGORY_KEYWORDS.get(k, []),
                }

        stat_after = yaml_path.stat()
        signature_after = _file_signature(stat_after)
        if signature_after == signature_before:
            # Файл не менялся во время чтения — закрепляем данные за сигнатурой.
            with _CACHE_LOCK:
                _CATEGORIES_CACHE[yaml_path] = (signature_after, result)
        # При изменении файла во время чтения возвращаем свежие данные,
        # не кэшируя устаревшую сигнатуру (следующий вызов перечитает файл).
        return result
    except Exception as exc:
        logger.debug("Не удалось загрузить categories.yaml: %s", exc)
        return _default_categories_data()


def get_categories_dict(skills_repo_dir: Path | None = None) -> dict[str, str]:
    """Возвращает словарь {category_slug: description}."""
    data = load_categories_data(skills_repo_dir)
    return {k: v["description"] for k, v in data.items()}


def get_category_keywords(skills_repo_dir: Path | None = None) -> dict[str, list[str]]:
    """Возвращает словарь ключевых слов для категорий."""
    data = load_categories_data(skills_repo_dir)
    return {k: v["keywords"] for k, v in data.items() if v.get("keywords")}


def is_valid_category(
    category: str,
    storage: SkillsStorage | None = None,
    skills_repo_dir: Path | None = None,
) -> bool:
    """Проверяет валидность категории.

    Категория валидна, если она присутствует в docs/categories.yaml,
    в стандартном реестре CATEGORIES или в БД проекта (storage).
    """
    if not category or not isinstance(category, str):
        return False
    if category != category.lower():
        return False
    norm = category.strip()
    # 1. Известные категории из YAML (если есть) или дефолтные
    data = load_categories_data(skills_repo_dir)
    if norm in data or norm in DEFAULT_CATEGORIES:
        return True
    # 2. Категории из БД проекта
    if storage is not None:
        counts = storage.get_category_counts()
        if norm in counts:
            return True
    return False


def get_categories_prompt(storage: SkillsStorage | None = None) -> str:
    """Генерирует компактный блок категорий для системного промпта агента (<100 токенов).

    При передаче storage формирует список на основе реальных категорий в БД проекта.
    """
    cat_desc = get_categories_dict()
    if storage is not None:
        counts = storage.get_category_counts()
        lines = ["Available Skill Categories:"]
        for c, count in counts.items():
            desc = cat_desc.get(c, f"Пользовательская категория ({count} скиллов)")
            lines.append(f"- {c}: {desc}")
        return "\n".join(lines)

    lines = ["Available Skill Categories:"]
    for c, desc in cat_desc.items():
        lines.append(f"- {c}: {desc}")
    return "\n".join(lines)
