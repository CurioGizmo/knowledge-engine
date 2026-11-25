"""Классификатор кандидатов и дубликатов гипотез для MCP Knowledge Engine."""

from __future__ import annotations

import re
from typing import Literal

from mcp_core.text import (
    morpho_diff as _morpho_diff,
)
from mcp_core.text import (
    morpho_intersection as _morpho_intersection,
)
from mcp_core.text import (
    stem_russian,
)
from mcp_core.text import (
    tokens_morpho_equal as _tokens_morpho_equal,
)

__all__ = [
    "ANTIPATTERN_CONFIRMED_THRESHOLD",
    "ANTIPATTERN_REFUTED_THRESHOLD",
    "DUP_SYMPTOM_THRESHOLD",
    "MIN_HYPO_TOKENS_FOR_OVERLAP",
    "MIN_TOKENS_FOR_OVERLAP",
    "_REASON_ALIAS_PAIRS",
    "_SHORT_ALIAS_CANON",
    "_all_unique_pairs_symmetrically_related",
    "_canonicalize_short_aliases",
    "_classify_empty_distinct_reasons",
    "_classify_single_difference",
    "_distinct_reason_tokens",
    "_has_paronym_in_full",
    "_reason_tokens_all_matched",
    "_reason_tokens_related",
    "_symptom_similarity",
    "_symptom_uniques_have_tech_differentiator",
    "_tokenize",
    "_unique_tokens_are_paronyms",
    "_verdict_equal_reasons",
    "classify_confirm_candidate",
]

DUP_SYMPTOM_THRESHOLD = 0.6  # check_duplicate: сильное совпадение симптомов
ANTIPATTERN_REFUTED_THRESHOLD = 0.45  # гипотезы: разные сути — не дубликат
ANTIPATTERN_CONFIRMED_THRESHOLD = 0.75  # check_active_card_consistency: похожие активные карточки
MIN_TOKENS_FOR_OVERLAP = 4
MIN_HYPO_TOKENS_FOR_OVERLAP = 3

_TOKEN_PATTERN = re.compile(r"\w{3,}")

_SHORT_ALIAS_CANON = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "pg": "postgres",
}

_SHORT_ALIAS_WORD_PATTERN = re.compile(r"\b(?:js|ts|py|pg)\b", re.IGNORECASE)


def _canonicalize_short_aliases(text: str) -> str:
    """Заменяет известные 2-символьные алиасы каноническими формами."""
    return _SHORT_ALIAS_WORD_PATTERN.sub(lambda m: _SHORT_ALIAS_CANON[m.group(0).lower()], text)


def _tokenize(text: str) -> set[str]:
    """Разбивает текст на множество токенов (слова длиной 3+, lowercase)."""
    return set(_TOKEN_PATTERN.findall(_canonicalize_short_aliases(text.lower())))


def _symptom_similarity(text_a: str, text_b: str) -> tuple[float, int]:
    """Считает overlap-сходство двух текстов по методу Jaccard-минимум."""
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    if not tokens_a or not tokens_b:
        return 0.0, 0
    min_tokens = min(len(tokens_a), len(tokens_b))
    overlap = len(tokens_a & tokens_b) / min_tokens
    return overlap, min_tokens


_REASON_ALIAS_PAIRS = frozenset(
    {
        tuple(sorted(pair))
        for pair in (
            ("postgres", "postgresql"),
            ("postgres", "pgsql"),
            ("pgsql", "postgresql"),
            ("postgres", "pg"),
            ("mongo", "mongodb"),
            ("sqlite", "sqlite3"),
            ("memcache", "memcached"),
            ("node", "nodejs"),
            ("k8s", "kubernetes"),
            ("memory", "ram"),
        )
    }
)

_ALIAS_SURFACE_FORMS = frozenset(term for pair in _REASON_ALIAS_PAIRS for term in pair) | set(
    _SHORT_ALIAS_CANON.values()
)
_INDEPENDENT_TECH_TERMS = frozenset(
    {
        "mysql",
        "mariadb",
    }
)
_KNOWN_TECH_TERMS = _ALIAS_SURFACE_FORMS | _INDEPENDENT_TECH_TERMS


def _reason_tokens_related(a: str, b: str) -> bool:
    """Проверяет, являются ли два токена формами/алиасами одной сущности."""
    if _tokens_morpho_equal(a, b):
        return True
    if ((a, b) if a < b else (b, a)) in _REASON_ALIAS_PAIRS:
        return True
    stem_a = stem_russian(a)
    stem_b = stem_russian(b)
    return bool(
        stem_a is not None
        and stem_b is not None
        and (stem_a == stem_b or stem_a.startswith(stem_b) or stem_b.startswith(stem_a))
    )


def _unique_tokens_are_paronyms(tokens_a: set[str], tokens_b: set[str]) -> bool:
    """Проверяет, являются ли уникальные токены паронимами."""
    for a in tokens_a:
        for b in tokens_b:
            if _reason_tokens_related(a, b):
                continue
            n = 0
            while n < min(len(a), len(b)) and a[n] == b[n]:
                n += 1
            if n >= 5 and not _tokens_morpho_equal(a, b):
                return True
    return False


def _has_paronym_in_full(unique_tokens: set[str], full_tokens: set[str]) -> bool:
    """Проверяет, есть ли у unique-токенов пароним в полном наборе другой причины."""
    for u in unique_tokens:
        for f in full_tokens:
            if _reason_tokens_related(u, f):
                continue
            n = 0
            while n < min(len(u), len(f)) and u[n] == f[n]:
                n += 1
            if n >= 5:
                return True
    return False


def _reason_tokens_all_matched(tokens: set[str], other: set[str]) -> bool:
    """Проверяет, что каждый токен одного набора имеет эквивалент в другом."""
    for t in tokens:
        if t in other:
            continue
        if any(_reason_tokens_related(t, o) for o in other):
            continue
        return False
    return True


def _all_unique_pairs_symmetrically_related(tokens_a: set[str], tokens_b: set[str]) -> bool:
    """Проверяет, что ВСЕ unique-токены ОБЕИХ сторон имеют пару."""
    return _reason_tokens_all_matched(tokens_a, tokens_b) and (
        _reason_tokens_all_matched(tokens_b, tokens_a)
    )


def _verdict_equal_reasons(
    reason: str,
    hypo_symptom: str,
    cand_symptom: str,
) -> Literal["duplicate", "uncertain"]:
    """Вердикт для дословно совпадающих текстов причин."""
    equal_symptom_tokens = _tokenize(hypo_symptom) | _tokenize(cand_symptom)
    own_tokens = _tokenize(reason) - equal_symptom_tokens
    if own_tokens:
        return "duplicate"
    return "uncertain"


def _symptom_uniques_have_tech_differentiator(uniq_req: set[str], uniq_cand: set[str]) -> bool:
    """Проверяет, есть ли в уникальных токенах симптомов различитель технологий."""
    if not uniq_req or not uniq_cand:
        return False
    for a in uniq_req:
        for b in uniq_cand:
            if _reason_tokens_related(a, b):
                continue
            if a in _KNOWN_TECH_TERMS and b in _KNOWN_TECH_TERMS:
                return True
    return False


def _distinct_reason_tokens(
    tokens_hypo: set[str],
    tokens_cand: set[str],
    symptom_tokens: set[str],
) -> tuple[set[str], set[str]]:
    """Вычисляет собственные содержательные токены причин (минус симптомные)."""
    return (
        tokens_hypo - symptom_tokens,
        tokens_cand - symptom_tokens,
    )


def _classify_empty_distinct_reasons(
    tokens_hypo: set[str], tokens_cand: set[str]
) -> Literal["duplicate", "distinct", "uncertain"]:
    """Классифицирует случай, когда обе причины не имеют собственных токенов."""
    full_diff_hypo = _morpho_diff(tokens_hypo, tokens_cand)
    full_diff_cand = _morpho_diff(tokens_cand, tokens_hypo)
    if not full_diff_hypo and not full_diff_cand:
        return "uncertain"
    if full_diff_hypo and full_diff_cand:
        if _all_unique_pairs_symmetrically_related(full_diff_hypo, full_diff_cand):
            return "uncertain"
        return "distinct"
    return "uncertain"


def _classify_single_difference(
    unique_hypo: set[str],
    unique_cand: set[str],
    overlap_distinct: float,
) -> Literal["duplicate", "distinct", "uncertain"]:
    """Классифицирует единственное расхождение при высоком совпадении остального."""
    related_pairs = {
        (a, b) if a < b else (b, a)
        for a in unique_hypo
        for b in unique_cand
        if a != b and _reason_tokens_related(a, b)
    }
    if overlap_distinct < ANTIPATTERN_CONFIRMED_THRESHOLD and not related_pairs:
        return "distinct"
    if _unique_tokens_are_paronyms(unique_hypo, unique_cand):
        return "distinct"
    if _symptom_uniques_have_tech_differentiator(unique_hypo, unique_cand):
        return "distinct"
    if _all_unique_pairs_symmetrically_related(unique_hypo, unique_cand):
        return "duplicate"
    return "uncertain"


def classify_confirm_candidate(
    hypo_symptom: str,
    hypo_reason: str,
    cand_symptom: str,
    cand_root_cause: str,
) -> Literal["duplicate", "distinct", "uncertain"]:
    """Классифицирует кандидата при confirm гипотезы."""
    s_hypo = hypo_symptom.strip().lower()
    s_cand = cand_symptom.strip().lower()
    symptom_matched = s_hypo == s_cand
    if not symptom_matched:
        overlap, min_tokens = _symptom_similarity(hypo_symptom, cand_symptom)
        symptom_matched = bool(
            min_tokens >= MIN_TOKENS_FOR_OVERLAP and overlap >= DUP_SYMPTOM_THRESHOLD
        )

    if not symptom_matched:
        return "distinct"

    tokens_hypo = _tokenize(hypo_reason or "")
    tokens_cand = _tokenize(cand_root_cause or "")
    if (
        len(tokens_hypo) < MIN_HYPO_TOKENS_FOR_OVERLAP
        or len(tokens_cand) < MIN_HYPO_TOKENS_FOR_OVERLAP
    ):
        return "uncertain"

    h_hypo = hypo_reason.strip().lower()
    h_cand = cand_root_cause.strip().lower()
    if h_hypo == h_cand:
        return _verdict_equal_reasons(hypo_reason, hypo_symptom, cand_symptom)

    overlap_hypo, min_h_tokens = _symptom_similarity(hypo_reason, cand_root_cause)
    raw_overlap_is_rephrase = _reason_tokens_all_matched(
        tokens_hypo, tokens_cand
    ) and _reason_tokens_all_matched(tokens_cand, tokens_hypo)
    if (
        min_h_tokens >= MIN_HYPO_TOKENS_FOR_OVERLAP
        and overlap_hypo < ANTIPATTERN_REFUTED_THRESHOLD
        and not raw_overlap_is_rephrase
        and (
            bool(_morpho_diff(tokens_hypo, tokens_cand))
            or bool(_morpho_diff(tokens_cand, tokens_hypo))
        )
    ):
        return "distinct"

    symptom_tokens = _tokenize(hypo_symptom) | _tokenize(cand_symptom)
    distinct_hypo, distinct_cand = _distinct_reason_tokens(tokens_hypo, tokens_cand, symptom_tokens)
    if not distinct_hypo and not distinct_cand:
        return _classify_empty_distinct_reasons(tokens_hypo, tokens_cand)
    if not distinct_hypo or not distinct_cand:
        return "uncertain"

    unique_hypo = _morpho_diff(distinct_hypo, distinct_cand)
    unique_cand = _morpho_diff(distinct_cand, distinct_hypo)
    matched = _morpho_intersection(distinct_hypo, distinct_cand)
    min_distinct = min(len(distinct_hypo), len(distinct_cand))
    overlap_distinct = len(matched) / min_distinct

    if unique_hypo and _has_paronym_in_full(unique_hypo, tokens_cand):
        return "distinct"
    if unique_cand and _has_paronym_in_full(unique_cand, tokens_hypo):
        return "distinct"

    if unique_hypo and unique_cand:
        return _classify_single_difference(unique_hypo, unique_cand, overlap_distinct)

    if unique_hypo or unique_cand:
        if unique_hypo and _reason_tokens_all_matched(unique_hypo, tokens_cand):
            return "duplicate"
        if unique_cand and _reason_tokens_all_matched(unique_cand, tokens_hypo):
            return "duplicate"
        return "uncertain"
    if overlap_distinct < ANTIPATTERN_REFUTED_THRESHOLD:
        return "distinct"

    return "duplicate"
