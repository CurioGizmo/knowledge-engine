"""MINOR-7: поисковые корпуса воспроизводимы из репозитория (не scratch).

Ревизия 4: search-corpus.json, known-debt-cases.json, golden-stemming.json
перенесены из scratch-артефакта агента в tests/fixtures/ — корпуса измерений
качества поиска (R6) обязаны быть частью репозитория для воспроизводимости
после фиксации контрактов стеммера и порога check_known_debt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict[str, object]] | dict[str, object]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, (list, dict))
    return data


def test_search_corpus_structure() -> None:
    """90 запросов с релевантными ID, группами и notes; без синтетики-пустышек."""
    corpus = _load("search-corpus.json")
    assert isinstance(corpus, list)
    assert len(corpus) == 90
    groups = {case.get("group") for case in corpus}
    expected_groups = {
        "short-terms",
        "russian-forms",
        "punctuation-hyphens",
        "typos",
        "tail-match",
        "mixed",
    }
    assert groups == expected_groups
    for case in corpus:
        assert case.get("query"), "пустой запрос"
        assert "notes" in case, "нет notes"
        if not case.get("synthetic"):
            # реальные случаи обязаны иметь непустые relevant_ids
            assert case.get("relevant_ids"), (
                f"реальный кейс без relevant_ids: {case.get('query')!r}"
            )
        # Ф0 (core-dedup v4): per-package атрибуция ожиданий.
        # relevant_ids остаются источником истины; expected — производное
        # (knowledge = всё без префикса aud-, audit = с префиксом aud-).
        exp = case.get("expected")
        assert isinstance(exp, dict), f"{case.get('query')!r}: нет expected-словаря"
        assert set(exp) == {"knowledge", "audit"}, "expected обязан иметь только knowledge/audit"
        derived_kn = [i for i in (case.get("relevant_ids") or []) if not i.startswith("aud-")]
        derived_au = [i for i in (case.get("relevant_ids") or []) if i.startswith("aud-")]
        assert exp["knowledge"] == derived_kn, (
            f"{case.get('query')!r}: expected.knowledge расходится с relevant_ids"
        )
        assert exp["audit"] == derived_au, (
            f"{case.get('query')!r}: expected.audit расходится с relevant_ids"
        )


def test_known_debt_cases_structure() -> None:
    """36 кейсов: minimum 10 known / 10 regression / 10 new; симптом непустой."""
    cases = _load("known-debt-cases.json")
    assert isinstance(cases, list)
    assert len(cases) == 36
    counts = {"known": 0, "regression": 0, "new": 0}
    for case in cases:
        expected = case.get("expected")
        assert expected in counts, f"неизвестный expected: {expected!r}"
        counts[expected] += 1  # type: ignore[index]
        assert case.get("symptom"), "пустой симптом"
    assert counts["known"] >= 10
    assert counts["regression"] >= 10
    assert counts["new"] >= 10


def test_golden_stemming_structure() -> None:
    """33 золотые пары + отрицательные; каждая с префиксом и must_not_equal."""
    data = _load("golden-stemming.json")
    assert isinstance(data, dict)
    golden = data["golden_pairs"]
    negative = data["negative_pairs"]
    assert len(golden) == 33
    assert len(negative) >= 10
    for pair in golden:
        assert pair["word"], "пустое слово"
        # expected_stem_prefix обязателен для репрезентативных пар: пустое
        # значение встречается у инвариантных пар (stem == word, срезать
        # нечего) — они проверяются только must_not_equal
        if pair.get("expected_stem_prefix"):
            assert pair["word"].startswith(pair["expected_stem_prefix"])
        assert pair["must_not_equal"], "пустой must_not_equal"
    # ключевые репродукции R6 из отчёта измерений
    words = {p["word"] for p in golden}
    for word in ("результатами", "кандидатами", "сертификатами"):
        assert word in words


def test_fixtures_dir_committed() -> None:
    """Каталог fixtures существует и содержит три корпуса (перенос из scratch)."""
    for name in ("search-corpus.json", "known-debt-cases.json", "golden-stemming.json"):
        assert (FIXTURES / name).is_file(), f"корпус не перенесён: {name}"
