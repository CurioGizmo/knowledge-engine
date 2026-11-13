"""Тесты для справочной системы get_help MCP Skills Engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_skills.help_docs import SKILLS_HELP_DOCS, get_skills_help_text


def test_get_help_overview_from_engine_docs() -> None:
    """Без tool_name возвращает содержимое docs/help/overview.md репозитория движка."""
    text = get_skills_help_text()
    assert "Skills Engine" in text
    assert "search_skills" in text
    # Источник — файл репозитория движка, а не встроенный fallback
    engine_root = Path(__file__).resolve().parent.parent
    expected = (engine_root / "docs" / "help" / "overview.md").read_text(encoding="utf-8").strip()
    assert text == expected


def test_get_help_known_tool_from_engine_docs() -> None:
    """Справка по известной тулзе читается из docs/help/<tool>.md движка."""
    text = get_skills_help_text("load_skill")
    assert "load_skill" in text
    assert "SLOT" in text or "slot" in text.lower()

    text_search = get_skills_help_text("search_skills")
    assert "search_skills" in text_search


def test_get_help_normalizes_key() -> None:
    """tool_name нормализуется: пробелы и регистр не ломают поиск."""
    assert get_skills_help_text("  LOAD_SKILL  ") == get_skills_help_text("load_skill")


def test_get_help_unknown_tool_hint() -> None:
    """Неизвестный tool_name — понятная подсказка со списком доступных разделов."""
    text = get_skills_help_text("nonexistent_tool")
    assert "не найдена" in text
    assert "overview" in text
    # Подсказка не должна быть пустой
    assert len(text.strip()) > 20


def test_get_help_fallback_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При отсутствии docs/help/<tool>.md возвращает встроенный fallback из SKILLS_HELP_DOCS."""
    # Указываем help_docs на временную директорию без файлов справки
    monkeypatch.setattr(
        "mcp_skills.help_docs.Path",
        lambda *_a, **_k: tmp_path / "nonexistent",
    )
    text = get_skills_help_text("overview")
    assert text == SKILLS_HELP_DOCS["overview"]


def test_fallback_dict_in_sync_with_docs_dir() -> None:
    """Ключи SKILLS_HELP_DOCS совпадают с файлами docs/help/ (защита от рассинхрона)."""
    engine_root = Path(__file__).resolve().parent.parent
    docs_dir = engine_root / "docs" / "help"
    doc_files = {p.stem for p in docs_dir.glob("*.md")}
    assert doc_files == set(SKILLS_HELP_DOCS.keys()), (
        f"Рассинхрон: docs/help={sorted(doc_files)}, fallback={sorted(SKILLS_HELP_DOCS)}"
    )


def test_get_help_returns_non_empty_for_all_keys() -> None:
    """Все ключи fallback-словаря содержат осмысленный (непустой) текст."""
    for key, text in SKILLS_HELP_DOCS.items():
        assert len(text.strip()) > 50, f"Пустая справка в fallback: {key}"
