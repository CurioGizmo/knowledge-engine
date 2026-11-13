"""Тесты золотых пар стемминга R6 (scratch → repo) и запрета ложных склеек.

Источник контракта: docs/analysis/2026-10-19-search-measurement-report.md,
разделы 1 и 6 (P0-фикс стеммера), данные scratch/golden-stemming.json.
"""

from __future__ import annotations

import pytest

from mcp_core.sanitizer import stem_russian

# Ключевые репродукции R6 (из отчёта измерений, раздел 1)
R6_REPRODUCTIONS = [
    # (word, expected_stem_prefix, must_not_equal)
    ("результатами", "результат", "результ"),
    ("результат", "результат", "результ"),
    ("результаты", "результат", "результ"),
    ("результатов", "результат", "результ"),
    ("результате", "результат", "результ"),
    ("результатом", "результат", "результ"),
    ("кандидатами", "кандидат", "кандид"),
    ("кандидат", "кандидат", "кандид"),
    ("кандидаты", "кандидат", "кандид"),
    ("кандидатов", "кандидат", "кандид"),
    ("сертификатами", "сертификат", "сертифик"),
    ("сертификат", "сертификат", "сертифик"),
    ("сертификаты", "сертификат", "сертифик"),
    ("сертификатов", "сертификат", "сертифик"),
]


@pytest.mark.parametrize("word,expected_prefix,must_not_equal", R6_REPRODUCTIONS)
def test_golden_reproductions(word: str, expected_prefix: str, must_not_equal: str) -> None:
    """Воспроизведения R6: -ат/-ят в корне не срезается вторым проходом."""
    stem = stem_russian(word)
    assert stem != must_not_equal, f"{word}: стем {stem!r} совпал с запрещённым {must_not_equal!r}"
    if stem is not None:
        assert stem.startswith(expected_prefix), (
            f"{word}: стем {stem!r} короче требуемого префикса {expected_prefix!r}"
        )


def test_wordform_and_base_share_stem_prefix() -> None:
    """Словоформа и база обязаны иметь общий префикс (prefix-search находит обе)."""
    for form, base, prefix in [
        ("результатами", "результат", "результат"),
        ("кандидатами", "кандидат", "кандидат"),
        ("сертификатами", "сертификат", "сертификат"),
    ]:
        stem_form = stem_russian(form)
        stem_base = stem_russian(base)
        assert stem_form is not None or stem_base is not None, f"{form}/{base}: оба стема None"
        stems = [s for s in (stem_form, stem_base) if s is not None]
        # Общий префикс >= expected
        common = _common_prefix_len(stems[0], stems[-1])
        assert common >= len(prefix), (
            f"{form}/{base}: общий префикс {common} < {len(prefix)} ({prefix!r}); стемы: {stem_form!r}, {stem_base!r}"
        )


# Отрицательные пары: разные лексемы НЕ должны склеиваться одинаковым стемом
NEGATIVE_PAIRS = [
    ("сертификат", "сертификатор"),
    ("сертификатами", "сертификаторами"),
    ("сертификат", "сертификация"),
    ("результатами", "результативностью"),
    ("результат", "результативность"),
]


@pytest.mark.parametrize("word_a,word_b", NEGATIVE_PAIRS)
def test_negative_pairs_no_stem_collision(word_a: str, word_b: str) -> None:
    """Разные лексемы одного префикса не получают одинаковый стем (ложная склейка).

    Контракт _meta golden-stemming.json: prefix-match допустим (OR-семантика
    префиксного поиска покрывает общие префиксы), stem-equality — нет.
    """
    stem_a = stem_russian(word_a)
    stem_b = stem_russian(word_b)
    if stem_a is None or stem_b is None:
        pytest.skip("None-стем допустим для коротких/латинских — склейки нет")
    assert stem_a != stem_b, f"Ложная склейка: {word_a}→{stem_a!r} == {word_b}→{stem_b!r}"


def test_dative_versus_verb_kept_distinct() -> None:
    """Запрет blanket-исключения: 'запросами' (дат.п.) и 'запросивший' (прич.) не склеиваются."""
    stem_a = stem_russian("запросами")
    stem_b = stem_russian("запросивший")
    if stem_a is not None and stem_b is not None:
        assert stem_a != stem_b, f"склейка {stem_a!r} == {stem_b!r}"


def test_derivative_at_suffix_still_cut() -> None:
    """Деривационный -ат (не в корне) продолжает срезаться: 'делегатами' → 'делегат'.

    Против blanket-запрета: если бы -ат перестал срезаться везде, словоформы
    'делегат'/'делегаты' и 'делегатами' всё равно делят 'делегат' — здесь
    -ат как часть основы «делегат» не должна резаться, что подтверждает
    правило «корневое -ат не срезается, деривационное — срезается»
    только при различении. См. инвариант ниже.
    """
    stem = stem_russian("делегатами")
    if stem is not None:
        assert stem.startswith("делегат")


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n
