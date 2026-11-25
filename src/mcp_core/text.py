"""Модуль централизованной обработки текста, морфологии и алгоритмической токенизации.

Консолидирует текстовые инварианты, стемминг (Snowball Russian/English) и
алгоритмическое распознавание коротких технических токенов без словарных списков.
"""

from __future__ import annotations

import re
import threading

import snowballstemmer  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Акронимы и стоп-слова (упразднены в пользу алгоритмов без словарей)
# Оставлены пустыми frozenset для обратной совместимости внешних импортов.
# ---------------------------------------------------------------------------

SHORT_ACRONYMS: frozenset[str] = frozenset()
TECH_ACRONYMS: frozenset[str] = frozenset()
LLM_QUERY_STOP_WORDS: frozenset[str] = frozenset()
REASON_STOPWORDS: frozenset[str] = frozenset()

_VOWELS: frozenset[str] = frozenset("aeiouyаеёиоуыэюя")


def is_technical_short_token(token: str, *, allow_all_len2: bool = False) -> bool:
    """Алгоритмически распознаёт короткий (2–3 символа) технический токен без словарей.

    Критерии (не зависят от языка и списков слов):
    1. Записан в верхнем регистре (UI, DB, API, SQL, MCP, LLM, БД, ДБ) при длине >= 2.
    2. Содержит цифру вместе с буквами или '%' (S3, K8, V2, 0%).
    3. Консонантный акроним без гласных (sql, fts, mcp, llm, jwt, css, db, ml, pr, ci, cd, дб, бд).
    4. Если ``allow_all_len2=True`` (короткий запрос без длинных слов >= 3),
       допускает любой 2-буквенный токен (например, «ui», «qa», «go», «id»).
    """
    clean = token.strip()
    if not clean or len(clean) < 2 or len(clean) >= 4:
        return False
    if "%" in clean and any(c.isdigit() for c in clean):
        return clean.replace("%", "").isdigit()
    if not clean.isalnum() or not any(c.isalpha() for c in clean):
        return False
    if clean.isupper():
        return True
    if any(c.isdigit() for c in clean):
        return True
    low = clean.lower()
    if not any(c in _VOWELS for c in low):
        return True
    return allow_all_len2 and len(clean) == 2


# ---------------------------------------------------------------------------
# Стемминг (Snowball)
# ---------------------------------------------------------------------------

_STEMMER_LOCAL = threading.local()


def _get_russian_stemmer() -> snowballstemmer.RussianStemmer:
    """Возвращает изолированный экземпляр RussianStemmer для текущего потока.

    snowballstemmer.RussianStemmer мутабелен (self.cursor, self.limit модифицируются
    in-place в stemWord), поэтому разделение одного экземпляра между потоками небезопасно.
    """
    stemmer = getattr(_STEMMER_LOCAL, "ru_stemmer", None)
    if stemmer is None:
        stemmer = snowballstemmer.RussianStemmer()
        _STEMMER_LOCAL.ru_stemmer = stemmer
    return stemmer


def _get_english_stemmer() -> snowballstemmer.EnglishStemmer:
    """Возвращает изолированный экземпляр EnglishStemmer для текущего потока.

    snowballstemmer.EnglishStemmer мутабелен (self.cursor, self.limit модифицируются
    in-place в stemWord), поэтому разделение одного экземпляра между потоками небезопасно.
    """
    stemmer = getattr(_STEMMER_LOCAL, "en_stemmer", None)
    if stemmer is None:
        stemmer = snowballstemmer.EnglishStemmer()
        _STEMMER_LOCAL.en_stemmer = stemmer
    return stemmer


def stem_russian(word: str) -> str | None:
    """Стемминг русского слова через алгоритм Snowball для FTS5-префиксного поиска.

    Возвращает основу слова (стем) либо None, если стемминг неприменим
    (слово короткое, не содержит кириллицы, стем совпадает со словом или стем < 3 символов).
    Потокобезопасен (использует thread-local экземпляр стеммера).
    """
    if not word:
        return None
    clean = word.lower().strip()
    if len(clean) < 4 or not re.search(r"[а-яё]", clean):
        return None
    stem = _get_russian_stemmer().stemWord(clean)
    if stem == clean or len(stem) < 3:
        return None
    return stem


# ---------------------------------------------------------------------------
# Сравнение и сопоставление
# ---------------------------------------------------------------------------


def short_acronym_matches(short_tokens: set[str], doc_blob: str) -> bool:
    """Точное совпадение коротких акронимов с токенами документа.

    Токенизация ``[A-Za-zА-Яа-яЁё0-9_-]+`` → lower → под-токены по ``-``/``_`` →
    пересечение с ``short_tokens``.
    """
    doc_tokens = {tok.lower() for tok in re.findall(r"[A-Za-zА-Яа-яЁё0-9_-]+", doc_blob)}
    split_subtokens: set[str] = set()
    for tok in doc_tokens:
        if "-" in tok or "_" in tok:
            split_subtokens.update(re.split(r"[-_]+", tok))
    return bool(short_tokens & (doc_tokens | split_subtokens))


def morpho_normalize(token: str) -> str:
    """Морфо-нормализация токена для overlap-сравнения через стемминг Snowball.

    Для кириллических слов использует RussianStemmer, для латинских — EnglishStemmer.
    Сводит отглагольные существительные и причастия к общей глагольной основе (-ован/-еван).
    """
    clean = token.lower().strip()
    if not clean:
        return ""
    if any("а" <= c <= "я" or c in "ёЁ" for c in clean):
        stem = _get_russian_stemmer().stemWord(clean)
        if (stem.endswith("ован") or stem.endswith("еван")) and len(stem) > 6:
            stem = stem[:-4]
        elif (stem.endswith("ова") or stem.endswith("ева")) and len(stem) > 5:
            stem = stem[:-3]
        return stem
    return _get_english_stemmer().stemWord(clean)


def tokens_morpho_equal(a: str, b: str) -> bool:
    """Проверяет, являются ли два токена формами одного слова через стемминг Snowball.

    Критерий:
    1. Дословное равенство.
    2. Одинаковые стемы Snowball (RussianStemmer для кириллицы, EnglishStemmer для латиницы).
    """
    if a == b:
        return True
    clean_a = a.lower().strip()
    clean_b = b.lower().strip()
    if clean_a == clean_b:
        return True
    if not clean_a or not clean_b:
        return False

    stem_a = morpho_normalize(clean_a)
    stem_b = morpho_normalize(clean_b)
    return bool(stem_a and stem_b and stem_a == stem_b)


def morpho_diff(tokens_a: set[str], tokens_b: set[str]) -> set[str]:
    """Токены A, не имеющие морфологического эквивалента в B."""
    result: set[str] = set()
    for a in tokens_a:
        if not any(tokens_morpho_equal(a, b) for b in tokens_b):
            result.add(a)
    return result


def morpho_intersection(tokens_a: set[str], tokens_b: set[str]) -> set[str]:
    """Токены A, имеющие морфологический эквивалент в B."""
    result: set[str] = set()
    for a in tokens_a:
        if any(tokens_morpho_equal(a, b) for b in tokens_b):
            result.add(a)
    return result


def word_overlap(w1: str, w2: str) -> bool:
    """Морфологическое сопоставление двух слов для ранжирования."""
    if not w1 or not w2:
        return False
    if w1 == w2:
        return True
    c1, c2 = w1.lower().strip(), w2.lower().strip()
    if c1 == c2:
        return True
    if (c1.startswith(c2) or c2.startswith(c1)) and min(len(c1), len(c2)) >= 3:
        return True
    stem1 = morpho_normalize(c1)
    stem2 = morpho_normalize(c2)
    if stem1 and stem2:
        if stem1 == stem2:
            return True
        if (stem1.startswith(stem2) or stem2.startswith(stem1)) and min(
            len(stem1), len(stem2)
        ) >= 3:
            return True
    return False
