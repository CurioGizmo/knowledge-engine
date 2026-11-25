"""Tests for AUDIT-FIX-P1: Search quality, tokenization, stemming and reranking.

Step 1:
- Letter Ё/ё in regex tokenizers (mcp_core, mcp_skills, mcp_knowledge, mcp_audit).
- is_cyrillic in knowledge/storage.py recognizes 'ё' (U+0451) and 'Ё' (U+0401).
- Unification of short_acronym_matches in audit/storage.py via mcp_core.fuzzy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_core.fuzzy import (
    jaccard_word_best,
    short_acronym_matches,
)
from mcp_core.sanitizer import extract_short_acronym_tokens
from mcp_knowledge.storage import _tokens_morpho_equal


def test_fuzzy_doc_chunk_regex_captures_yo() -> None:
    # Text with words containing 'ё' and 'Ё'
    doc_text = "Надёжный объём данных и жёсткий диск с Ёмкостью"
    # Word match via jaccard_word_best
    # 'надёжный' has trigrams: над адё дёж ёжн жны ный
    score = jaccard_word_best("надёжный", doc_text)
    assert score == 1.0, f"Expected 1.0 match for 'надёжный', got {score}"

    score_yo = jaccard_word_best("ёмкостью", doc_text)
    assert score_yo >= 0.7, f"Expected high match for 'ёмкостью', got {score_yo}"


def test_short_acronym_matches_captures_yo() -> None:
    # 2-letter tokens with 'ё'
    doc_blob = "всё ещё в силе"
    tokens = {"ёж", "её"}
    assert not short_acronym_matches(tokens, doc_blob)

    doc_blob_match = "зелёный ёж в лесу"
    assert short_acronym_matches({"ёж"}, doc_blob_match)
    # Composite with dash/underscore
    assert short_acronym_matches({"ёж"}, "супер-ёж на базе")


def test_extract_short_acronym_tokens_with_yo(monkeypatch: pytest.MonkeyPatch) -> None:
    # Monkeypatch SHORT_ACRONYMS to include 2-letter cyrillic acronym with ё
    from mcp_core import sanitizer

    monkeypatch.setattr(sanitizer, "SHORT_ACRONYMS", sanitizer.SHORT_ACRONYMS | {"ёж"})
    extracted = extract_short_acronym_tokens("Проверим токен ёж в запросе")
    assert "ёж" in extracted


def test_knowledge_morpho_variants_is_cyrillic_with_yo() -> None:
    # 'надёжный' and 'надёжного' should be recognized as cyrillic morpho variants
    assert _tokens_morpho_equal("надёжный", "надёжного") is True
    # 'чёткий' and 'чёткого' with 'ё' should match
    assert _tokens_morpho_equal("чёткий", "чёткого") is True
    assert _tokens_morpho_equal("ёмкий", "ёмкого") is True


def test_punctuation_splitting_in_sanitize_fts_query() -> None:
    from mcp_core.sanitizer import sanitize_fts_query

    # foo(bar) should not be glued into foobar
    res1 = sanitize_fts_query("foo(bar)")
    assert "foo*" in res1 and "bar*" in res1
    assert "foobar*" not in res1

    # user@host
    res2 = sanitize_fts_query("user@host")
    assert "user*" in res2 and "host*" in res2
    assert "userhost" not in res2

    # baz,qux;hello!world?
    res3 = sanitize_fts_query("baz,qux;hello!world?")
    assert "baz*" in res3 and "qux*" in res3 and "hello*" in res3 and "world*" in res3

    # Compound terms preserved
    res4 = sanitize_fts_query("anti-spam user_auth io/os 0%")
    assert '"anti-spam"' in res4
    assert '"user_auth"' in res4
    assert '"io/os"' in res4
    assert '"0%"' in res4


def test_stem_russian_4_5_letter_golden() -> None:
    from mcp_core.sanitizer import stem_russian

    # база/базы/базе/базу/базой -> баз
    assert stem_russian("база") == "баз"
    assert stem_russian("базы") == "баз"
    assert stem_russian("базе") == "баз"
    assert stem_russian("базу") == "баз"
    assert stem_russian("базой") == "баз"

    # тест/теста/тесты/тесте/тесту -> тест
    assert stem_russian("теста") == "тест"
    assert stem_russian("тесты") == "тест"
    assert stem_russian("тесте") == "тест"
    assert stem_russian("тесту") == "тест"

    # план/плана/планы/плане/плану -> план
    assert stem_russian("плана") == "план"
    assert stem_russian("планы") == "план"
    assert stem_russian("плане") == "план"
    assert stem_russian("плану") == "план"

    # файл/файла/файлы/файле/файлу -> файл
    assert stem_russian("файла") == "файл"
    assert stem_russian("файлы") == "файл"
    assert stem_russian("файле") == "файл"
    assert stem_russian("файлу") == "файл"


def test_stem_russian_short_words_negative() -> None:
    from mcp_core.sanitizer import stem_russian

    # Roots < 3 characters or words <= 3 chars must NOT be stripped
    assert stem_russian("код") is None
    assert stem_russian("пир") is None
    assert stem_russian("ад") is None
    assert stem_russian("ада") is None  # stem 'ад' len 2 < 3
    assert stem_russian("он") is None
    assert stem_russian("эхо") is None  # stem 'эх' len 2 < 3
    assert stem_russian("ухо") is None  # stem 'ух' len 2 < 3
    assert stem_russian("два") is None  # stem 'дв' len 2 < 3


def test_stem_russian_thread_safety() -> None:
    """Проверяет потокобезопасность stem_russian при параллельных вызовах из многих потоков."""
    import concurrent.futures

    from mcp_core.text import stem_russian

    words = [
        "сервере",
        "запросы",
        "системы",
        "версии",
        "миграции",
        "оптимизация",
        "базу",
        "карточками",
    ] * 100

    def task(w: str) -> str | None:
        return stem_russian(w)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(task, words))

    assert len(results) == len(words)
    assert all(r is not None for r in results)
    assert results[0] == "сервер"


def test_audit_english_stemmer_golden() -> None:
    from mcp_audit.storage import _morpho_normalize

    # Sibilants and 'o' should cut -es
    assert _morpho_normalize("boxes") == "box"
    assert _morpho_normalize("watches") == "watch"
    assert _morpho_normalize("processes") == "process"
    assert _morpho_normalize("crashes") == "crash"
    assert _morpho_normalize("buzzes") == "buzz"
    assert _morpho_normalize("heroes") == "hero"

    # Silent -e words must only cut -s, preserving the root 'byte', 'file', etc.
    assert _morpho_normalize("bytes") == "byte"
    assert _morpho_normalize("files") == "file"
    assert _morpho_normalize("codes") == "code"
    assert _morpho_normalize("nodes") == "node"
    assert _morpho_normalize("lines") == "line"
    assert _morpho_normalize("types") == "type"
    assert _morpho_normalize("pipes") == "pipe"
    assert _morpho_normalize("rules") == "rule"
    assert _morpho_normalize("states") == "state"


def test_audit_significant_tokens_it_acronyms() -> None:
    from mcp_audit.storage import _significant_tokens

    tokens = _significant_tokens("ошибка sql fts mcp llm jwt в API WAL UI БД дб и s3")
    for expected in (
        "ошибка",
        "sql",
        "fts",
        "mcp",
        "llm",
        "jwt",
        "api",
        "wal",
        "ui",
        "бд",
        "дб",
        "s3",
    ):
        assert expected in tokens
    assert "в" not in tokens
    assert "и" not in tokens


def test_is_technical_short_token_contract() -> None:
    from mcp_core.text import is_technical_short_token

    # Граничные длины: 1 и >= 4 отклоняются
    assert is_technical_short_token("") is False
    assert is_technical_short_token("A") is False
    assert is_technical_short_token("a", allow_all_len2=True) is False
    assert is_technical_short_token("ABCD") is False
    assert is_technical_short_token("abcd") is False

    # Верхний регистр vs смешанный/нижний с гласными
    assert is_technical_short_token("UI") is True
    assert is_technical_short_token("API") is True
    assert is_technical_short_token("БД") is True
    assert is_technical_short_token("ЁЖ") is True
    assert is_technical_short_token("Ui") is False
    assert is_technical_short_token("ui") is False

    # Цифры, проценты и небуквенно-цифровой мусор
    assert is_technical_short_token("0%") is True
    assert is_technical_short_token("%") is False
    assert is_technical_short_token("50") is False
    assert is_technical_short_token("50", allow_all_len2=True) is False
    assert is_technical_short_token("s3") is True
    assert is_technical_short_token("A!") is False
    assert is_technical_short_token("a!", allow_all_len2=True) is False

    # Консонантные акронимы без гласных (в т.ч. без 'y'/'й') vs слова с гласными
    for term in ("sql", "fts", "mcp", "llm", "jwt", "css", "db", "ml", "дб", "бд"):
        assert is_technical_short_token(term) is True, term
    assert is_technical_short_token("try") is False  # 'y' считается гласной
    assert is_technical_short_token("при") is False
    assert is_technical_short_token("на") is False

    # allow_all_len2=True открывает 2-буквенные токены (ui, qa), но не 3-буквенные стоп-слова
    assert is_technical_short_token("ui", allow_all_len2=True) is True
    assert is_technical_short_token("qa", allow_all_len2=True) is True
    assert is_technical_short_token("the", allow_all_len2=True) is False
    assert is_technical_short_token("при", allow_all_len2=True) is False


def test_section_matches_query_filters_stop_words_and_exact_short_tokens() -> None:
    from mcp_skills.storage import _section_matches_query

    meta = {"type": "section", "slot": "core", "variant": "default"}
    body = "Порядок выполнения задач и последовательность операций."

    # Preposition 'по' must NOT match 'Порядок' or 'последовательность'
    assert _section_matches_query(meta, body, "по") is False
    assert _section_matches_query(meta, body, "за") is False

    # Short acronym 'ui' must match 'ui' exactly, not 'uint'
    body_with_uint = "Используем uint64 для счетчиков."
    assert _section_matches_query(meta, body_with_uint, "ui") is False

    body_with_ui = "Создание ui компонентов для пользователя."
    assert _section_matches_query(meta, body_with_ui, "ui") is True
    # Смешанный короткий запрос с верхним регистром и в нижнем регистре
    assert _section_matches_query(meta, body_with_ui, "UI компоненты") is True
    assert _section_matches_query(meta, body_with_ui, "ui компоненты") is True
    assert _section_matches_query(meta, "Лимит строк 50", "50") is True


def test_flow_search_supports_qa_and_stemming() -> None:
    from mcp_skills.server import _score_flows

    flows = [
        {
            "name": "live-qa-flow",
            "title": "Сценарий живой верификации и QA",
            "description": "Проведение сквозного тестирования и QA приёмки",
            "aliases": ["qa верификация", "запусти qa"],
            "intent_group": "qa",
        },
        {
            "name": "review-flow",
            "title": "Код-ревью",
            "description": "Проверка кода перед мерджем",
            "aliases": ["проверь код"],
            "intent_group": "review",
        },
    ]

    # Search for 'qa' should find live-qa-flow
    found_qa, _ = _score_flows(flows, "qa")
    assert any(f["name"] == "live-qa-flow" for f in found_qa)

    # Search with stop words like 'как провести тестирование'
    found_test, _ = _score_flows(flows, "как провести тестирование")
    assert any(f["name"] == "live-qa-flow" for f in found_test)


def test_sqlite_cyrillic_lower(tmp_path: Path) -> None:
    from mcp_core.db import get_connection

    conn = get_connection(tmp_path / "test_cyr.db")
    res = conn.execute("SELECT lower('АРХИТЕКТУРА И ДИЗАЙН')").fetchone()[0]
    assert res == "архитектура и дизайн"


def test_search_faceted_single_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_knowledge.storage import KnowledgeStorage
    from unittest.mock import MagicMock

    storage = KnowledgeStorage(tmp_path / "k.db", knowledge_dir=tmp_path / "k_dir")
    sync_mock = MagicMock()
    monkeypatch.setattr(storage, "sync", sync_mock)

    storage.search_faceted(query="test", categories=["backend"], include_overview=True)
    assert sync_mock.call_count == 1, f"Expected exactly 1 sync call, got {sync_mock.call_count}"


def test_faceted_search_hypothesis_separation(tmp_path: Path) -> None:
    from mcp_knowledge.models import HypothesisRecordRequest, KnowledgeCategory
    from mcp_knowledge.service import KnowledgeService
    from mcp_knowledge.storage import KnowledgeStorage

    k_dir = tmp_path / "knowledge"
    c_db = tmp_path / "cache.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    req = HypothesisRecordRequest(
        title="Тестовая гипотеза разделения слоев",
        category=KnowledgeCategory.BACKEND,
        hypothesis="Гипотеза не должна попадать в category_results",
        symptom="Утечка гипотез в категории",
        task_id="task-p1-test",
    )
    res = service.propose_hypothesis(req)
    hypo_id = res["id"]

    resp = storage.search_faceted(
        categories=["backend"],
        task_id="task-p1-test",
        include_hypotheses=False,
    )
    cat_ids = [s.id for s in resp.category_results.get("backend", [])]
    assert hypo_id not in cat_ids, (
        "Hypothesis must not leak into category_results when include_hypotheses=False"
    )

    resp_with_hypo = storage.search_faceted(
        categories=["backend"],
        task_id="task-p1-test",
        include_hypotheses=True,
    )
    cat_ids_2 = [s.id for s in resp_with_hypo.category_results.get("backend", [])]
    hypo_ids_2 = [s.id for s in resp_with_hypo.hypotheses]
    assert hypo_id not in cat_ids_2, "Hypothesis must not be in category_results"
    assert hypo_id in hypo_ids_2, "Hypothesis must be in hypotheses layer"
