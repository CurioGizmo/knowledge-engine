"""Шаг 1 плана skills-correctness: изолированный кэш категорий по пути."""

from __future__ import annotations

import os
from pathlib import Path

from mcp_skills import categories as categories_module
from mcp_skills.categories import DEFAULT_CATEGORIES, load_categories_data


def _make_repo(base: Path, name: str, cats_yaml: str) -> Path:
    repo_dir = base / name
    docs_dir = repo_dir / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "categories.yaml").write_text(cats_yaml, encoding="utf-8")
    return repo_dir


YAML_A = """categories:
  alpha:
    title: Alpha
    description: Категория A
  beta:
    title: Beta
    description: Категория B
"""

YAML_A_CHANGED = """categories:
  alpha:
    title: Alpha
    description: Категория A (обновлено)
  beta:
    title: Beta
    description: Категория B
"""

YAML_B = """categories:
  gamma:
    title: Gamma
    description: Категория C
  delta:
    title: Delta
    description: Категория D
"""


def test_same_mtime_different_dirs(tmp_path: Path) -> None:
    """Два каталога с одинаковым mtime: каждый возвращает свои категории."""
    categories_module._CATEGORIES_CACHE.clear()
    repo_a = _make_repo(tmp_path, "repo_a", YAML_A)
    repo_b = _make_repo(tmp_path, "repo_b", YAML_B)

    stat_a = (repo_a / "docs" / "categories.yaml").stat()
    stat_b = (repo_b / "docs" / "categories.yaml").stat()
    same_time = max(stat_a.st_mtime_ns, stat_b.st_mtime_ns)
    os.utime(repo_a / "docs" / "categories.yaml", ns=(same_time, same_time))
    os.utime(repo_b / "docs" / "categories.yaml", ns=(same_time, same_time))

    data_a = load_categories_data(repo_a)
    data_b = load_categories_data(repo_b)

    assert set(data_a) == {"alpha", "beta"}
    assert set(data_b) == {"gamma", "delta"}

    # Повторные вызовы (уже из кэша) по-прежнему изолированы по пути.
    assert set(load_categories_data(repo_a)) == {"alpha", "beta"}
    assert set(load_categories_data(repo_b)) == {"gamma", "delta"}

    assert len(categories_module._CATEGORIES_CACHE) == 2


def test_change_invalidates_only_own_entry(tmp_path: Path) -> None:
    """Изменение файла инвалидирует только его запись; неизменённый — из кэша."""
    categories_module._CATEGORIES_CACHE.clear()
    repo_a = _make_repo(tmp_path, "repo_a", YAML_A)
    repo_b = _make_repo(tmp_path, "repo_b", YAML_B)

    data_b_first = load_categories_data(repo_b)
    data_a_first = load_categories_data(repo_a)
    assert set(data_a_first) == {"alpha", "beta"}
    assert "обновлено" not in data_a_first["alpha"]["description"]

    # Меняем файл repo_a (mtime_ns и size меняются).
    yaml_a = repo_a / "docs" / "categories.yaml"
    yaml_a.write_text(YAML_A_CHANGED, encoding="utf-8")

    data_a_second = load_categories_data(repo_a)
    assert "обновлено" in data_a_second["alpha"]["description"]
    assert set(data_a_second) == {"alpha", "beta"}

    # Запись repo_b не тронута и осталась в кэше.
    cache_b = categories_module._CATEGORIES_CACHE[(repo_b / "docs" / "categories.yaml").resolve()]
    assert cache_b[1] is data_b_first or cache_b[1] == data_b_first

    # Повторный вызов неизменённого файла использует кэш (тот же объект).
    assert load_categories_data(repo_b) is cache_b[1]


def test_missing_and_broken_yaml_fallback(tmp_path: Path) -> None:
    """Отсутствующий/ошибочный YAML сохраняет fallback-поведение."""
    categories_module._CATEGORIES_CACHE.clear()

    # Отсутствующий файл.
    empty_repo = tmp_path / "empty_repo"
    (empty_repo / "docs").mkdir(parents=True)
    data_missing = load_categories_data(empty_repo)
    assert set(data_missing) == set(DEFAULT_CATEGORIES)
    # Ошибки загрузки не кэшируются.
    assert len(categories_module._CATEGORIES_CACHE) == 0

    # Ошибочный YAML.
    broken_repo = _make_repo(tmp_path, "broken_repo", "categories: [unclosed")
    data_broken = load_categories_data(broken_repo)
    assert set(data_broken) == set(DEFAULT_CATEGORIES)
    assert len(categories_module._CATEGORIES_CACHE) == 0

    # Валидный YAML после fallback-вызовов работает как обычно.
    ok_repo = _make_repo(tmp_path, "ok_repo", YAML_A)
    assert set(load_categories_data(ok_repo)) == {"alpha", "beta"}
