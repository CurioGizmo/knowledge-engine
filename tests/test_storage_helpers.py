"""Юнит-тесты модульных хелперов storage.py: token-overlap, категории, FTS-prelude, константы."""

import pytest

from mcp_knowledge.storage import (
    ANTIPATTERN_CONFIRMED_THRESHOLD,
    ANTIPATTERN_REFUTED_THRESHOLD,
    DUP_DETAIL_THRESHOLD,
    DUP_PENALTY,
    DUP_SYMPTOM_THRESHOLD,
    HINT_LIMIT_REACHED,
    HYP_DETAIL_THRESHOLD,
    HYP_PENALTY,
    HYP_SYMPTOM_THRESHOLD,
    MAX_TOTAL_SNIPPETS,
    MIN_HYPO_TOKENS_FOR_OVERLAP,
    MIN_TOKENS_FOR_OVERLAP,
    _CARD_COLUMNS,
    _build_symptom_fts_query,
    _clean_categories,
    _symptom_similarity,
    _tokenize,
)


# --- _tokenize ---------------------------------------------------------------


def test_tokenize_lowercases_and_drops_short_words():
    assert _tokenize("Database Connection Pool ERROR") == {
        "database",
        "connection",
        "pool",
        "error",
    }


def test_tokenize_empty_and_punctuation_only():
    assert _tokenize("") == set()
    assert _tokenize("!!! ... ---") == set()


# --- _symptom_similarity ------------------------------------------------------


def test_symptom_similarity_identical_texts():
    overlap, min_tokens = _symptom_similarity(
        "connection pool exhausted", "connection pool exhausted"
    )
    assert overlap == 1.0
    assert min_tokens == 3


def test_symptom_similarity_disjoint_texts():
    text_a = "database connection pool"
    text_b = "network timeout error"
    overlap, min_tokens = _symptom_similarity(text_a, text_b)
    assert overlap == 0.0
    assert min_tokens == 3


def test_symptom_similarity_partial_overlap_ratio():
    # a ∩ b = {alpha, beta}; min(|a|, |b|) = 3 → 2/3
    overlap, min_tokens = _symptom_similarity("alpha beta gamma", "alpha beta delta")
    assert overlap == pytest.approx(2 / 3)
    assert min_tokens == 3


def test_symptom_similarity_empty_returns_zero():
    assert _symptom_similarity("", "some tokens here") == (0.0, 0)
    assert _symptom_similarity("some tokens here", "") == (0.0, 0)
    assert _symptom_similarity("", "") == (0.0, 0)


# --- _clean_categories -----------------------------------------------------------


def test_clean_categories_strips_lowercases_and_dedupes():
    assert _clean_categories([" Database ", "database", " ML "]) == ["database", "ml"]


def test_clean_categories_drops_empty_and_none_entries():
    assert _clean_categories(["", None, "  ", "ui"]) == ["ui"]  # type: ignore[list-item]


def test_clean_categories_limit_five():
    categories = [f"cat{i}" for i in range(8)]
    assert _clean_categories(categories) == [f"cat{i}" for i in range(5)]


def test_clean_categories_none_input():
    assert _clean_categories(None) == []


# --- _build_symptom_fts_query --------------------------------------------------


def test_build_symptom_fts_query_wraps_sanitized():
    query = _build_symptom_fts_query("connection pool error")
    assert query is not None
    assert query.startswith("symptom : (")
    assert query.endswith(")")


def test_build_symptom_fts_query_none_for_empty():
    assert _build_symptom_fts_query("") is None
    assert _build_symptom_fts_query("   ") is None


def test_build_symptom_fts_query_none_for_quotes_only():
    # Санитизатор вырождает строку из кавычек до '""' → guard возвращает None
    assert _build_symptom_fts_query('""') is None


# --- Пороговые константы (значения сохранены 1:1 с прежним кодом) --------------


def test_duplicate_threshold_constants_unchanged():
    assert DUP_SYMPTOM_THRESHOLD == 0.6
    assert DUP_DETAIL_THRESHOLD == 0.5
    assert DUP_PENALTY == -0.1
    assert HYP_SYMPTOM_THRESHOLD == 0.7
    assert HYP_DETAIL_THRESHOLD == 0.6
    assert HYP_PENALTY == -0.2
    assert ANTIPATTERN_REFUTED_THRESHOLD == 0.45
    assert ANTIPATTERN_CONFIRMED_THRESHOLD == 0.75
    assert MIN_TOKENS_FOR_OVERLAP == 4
    assert MIN_HYPO_TOKENS_FOR_OVERLAP == 3


# --- _CARD_COLUMNS — источник колонок для _rows_to_cards -----------------------


def test_card_columns_shape():
    assert len(_CARD_COLUMNS) == 16
    assert _CARD_COLUMNS[0] == "id"
    assert _CARD_COLUMNS[-1] == "rationale"
    # Порядок колонок байт-идентичен прежним inline SELECT (get_cards_by_ids / list_recent_cards)
    assert _CARD_COLUMNS == (
        "id",
        "title",
        "category",
        "type",
        "status",
        "source",
        "generated_by",
        "created_at",
        "stale_after",
        "verified_count",
        "confidence",
        "task_id",
        "symptom",
        "root_cause",
        "solution",
        "rationale",
    )


# --- MAX_TOTAL_SNIPPETS / HINT_LIMIT_REACHED (аудит L-magic-12) ----------------


def test_max_total_snippets_constant():
    assert MAX_TOTAL_SNIPPETS == 12


def test_hint_limit_reached_text_uses_constant():
    # f-строка вычисляется на уровне модуля — текст байт-идентичен прежнему литералу
    assert HINT_LIMIT_REACHED == (
        "Достигнут лимит выдачи (12 сниппетов). "
        "Уточните запрос или сократите список categories для более детальной выборки."
    )
    assert f"{MAX_TOTAL_SNIPPETS} сниппетов" in HINT_LIMIT_REACHED
