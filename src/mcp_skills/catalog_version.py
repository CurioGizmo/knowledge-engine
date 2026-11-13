"""Проверка совместимости версий каталога скиллов и engine."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def get_catalog_min_engine_version(catalog_root: Path) -> str | None:
    """Читает min_engine_version из метаданных каталога (catalog.yaml, docs/catalog.yaml, docs/categories.yaml).

    Возвращает строку версии (например, '0.3.0') или None, если поле не задано.
    """
    candidates = (
        catalog_root / "catalog.yaml",
        catalog_root / "docs" / "catalog.yaml",
        catalog_root / "docs" / "catalog-metadata.yaml",
        catalog_root / "docs" / "categories.yaml",
    )
    for path in candidates:
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "min_engine_version" in data:
                    val = data["min_engine_version"]
                    if val is not None:
                        return str(val).strip()
            except (yaml.YAMLError, OSError, ValueError) as err:
                logger.debug("Не удалось прочитать версию каталога из %s: %s", path, err)
                continue
    return None


def check_engine_version_compatibility(min_version: str | None, engine_version: str) -> str | None:
    """Сравнивает версию engine с требуемой min_engine_version каталога.

    Возвращает сообщение об ошибке, если engine устарел, или None при совместимости.
    """
    if not min_version:
        return None

    try:
        from packaging.version import parse as parse_version

        if parse_version(engine_version) < parse_version(min_version):
            return (
                f"Версия engine '{engine_version}' ниже требуемой каталогом "
                f"min_engine_version '{min_version}'. Требуется обновление движка."
            )
    except Exception:
        # Fallback: простое сравнение кортежей целых чисел
        v_eng = tuple(int(x) for x in engine_version.split(".") if x.isdigit())
        v_min = tuple(int(x) for x in min_version.split(".") if x.isdigit())
        if v_eng < v_min:
            return (
                f"Версия engine '{engine_version}' ниже требуемой каталогом "
                f"min_engine_version '{min_version}'. Требуется обновление движка."
            )

    return None
