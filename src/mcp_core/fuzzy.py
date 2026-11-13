"""Триграммный Fuzzy-поиск и Jaccard-метрика сходства слов."""

from __future__ import annotations

import re

__all__ = [
    "FUZZY_CANDIDATE_LIMIT",
    "FUZZY_DOC_CHAR_CAP",
    "FUZZY_DOC_CHUNK_OVERLAP",
    "FUZZY_MAX_EDIT_DISTANCE",
    "FUZZY_MIN_TRIGRAM_OVERLAP",
    "FUZZY_THRESHOLD",
    "build_fuzzy_match",
    "jaccard_word_best",
    "short_acronym_matches",
    "token_trigrams",
]

# Калиброванный нормативный порог: точно соответствует J("отлдка", "отладка") = 2/7 = 0.285714...
FUZZY_THRESHOLD: float = 0.2857
FUZZY_CANDIDATE_LIMIT: int = 200
# Бюджет одного чанка текста документа при fuzzy-скоринге (не общий cap на весь
# документ: слово в хвосте длинного документа обязано оставаться видимым).
FUZZY_DOC_CHAR_CAP: int = 600
# Перекрытие соседних чанков в символах: слово на стыке чанков не должно
# разрезаться пополам (оно попадёт целиком хотя бы в один чанк).
FUZZY_DOC_CHUNK_OVERLAP: int = 32
# Гейты против ложных срабатываний Jaccard-порога (регрессии E-фазы).
# Легитимные опечатки корпуса (sqllite~sqlite, отлдка~отладка, снитаксис~синтаксис)
# имеют пересечение триграмм >= 2 и расстояние редактирования <= 2; ложные пары
# (added~add: пересечение 1 триграмма; cat_keyword~dog_keyword: lev=3) нарушают
# минимум одно из условий.
FUZZY_MIN_TRIGRAM_OVERLAP: int = 2
FUZZY_MAX_EDIT_DISTANCE: int = 2
# Морфоканал (5бис-1): длинные слова (>=8 букв) — русские флексии
# («зависание»~«зависания», «блокировку»~«блокировки») имеют lev=1..2 и
# Jaccard ~0.7+. Пары типа cat_keyword~dog_keyword (lev=3, J=0.5) —
# не флексии: морфо-ветка требует ВЫСОКИЙ Jaccard, ослабляя только
# edit-distance. Короткие слова сохраняют строгий порог (E-фаза регрессий).
FUZZY_MORPHOLOGY_MIN_WORD_LEN: int = 8
FUZZY_MORPHOLOGY_MAX_EDIT_DISTANCE: int = 3
FUZZY_MORPHOLOGY_MIN_JACCARD: float = 0.55


def token_trigrams(token: str) -> set[str]:
    """Разбивает токен на множество перекрывающихся 3-символьных подстрок (триграмм)."""
    t = token.lower().strip()
    if len(t) < 3:
        return set()
    return {t[i : i + 3] for i in range(len(t) - 2)}


def build_fuzzy_match(token: str) -> str:
    """Генерирует FTS5 MATCH OR-выражение из триграмм для поиска кандидатов."""
    trigrams = token_trigrams(token)
    if not trigrams:
        return ""
    # Каждая триграмма в кавычках через OR
    return " OR ".join(f'"{tg}"' for tg in sorted(trigrams))


def _edit_distance(a: str, b: str, budget: int = 4) -> int:
    """Расстояние редактирования с ранним выходом (обрезано сверху ``budget + 1``).

    Дешёвая узкополосная Levenshtein: для гейтов опечаток достаточно знать,
    помещается ли расстояние в 2, поэтому полный расчёт обрезается бюджетом.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > budget:
        return budget + 1
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
        if min(prev) > budget:
            return budget + 1
    return prev[-1]


def _word_gate_passes(q_lower: str, w_lower: str, intersection: int, jaccard: float) -> bool:
    """Гейт против ложных срабатываний для пары (запрос, слово документа).

    Морфоканал (5бис-1): слова >= FUZZY_MORPHOLOGY_MIN_WORD_LEN букв проходят
    ослабленный гейт (lev<=3 + Jaccard-порог 0.55) — русские флексии не
    обязаны быть опечатками. Не морф-пара — строгий гейт опечаток как
    fallback (регрессии E-фазы: added~add, cat_keyword~dog_keyword).
    """
    morpho = (
        len(q_lower) >= FUZZY_MORPHOLOGY_MIN_WORD_LEN
        or len(w_lower) >= FUZZY_MORPHOLOGY_MIN_WORD_LEN
    )
    if not morpho:
        return (
            intersection >= FUZZY_MIN_TRIGRAM_OVERLAP
            and _edit_distance(q_lower, w_lower) <= FUZZY_MAX_EDIT_DISTANCE
        )
    if jaccard < FUZZY_MORPHOLOGY_MIN_JACCARD:
        # не морф-пара — строгий гейт опечаток как fallback
        return (
            intersection >= FUZZY_MIN_TRIGRAM_OVERLAP
            and _edit_distance(q_lower, w_lower) <= FUZZY_MAX_EDIT_DISTANCE
        )
    return (
        _edit_distance(q_lower, w_lower, budget=FUZZY_MORPHOLOGY_MAX_EDIT_DISTANCE)
        <= FUZZY_MORPHOLOGY_MAX_EDIT_DISTANCE
    )


def jaccard_word_best(query_token: str, doc_text: str) -> float:
    """Вычисляет максимальный коэффициент Жаккара между токеном запроса и словами документа.

    Документ сканируется чанками по FUZZY_DOC_CHAR_CAP символов с перекрытием
    FUZZY_DOC_CHUNK_OVERLAP: общий бюджет распределён по чанкам, поэтому слова
    в хвосте длинного документа (за пределами первого чанка) остаются видимыми
    (регрессия cap-600: совпадение на offset 3600 обнуляло Jaccard). Общий объём
    обработки ограничен числом чанков, а не длиной документа без предела.
    """
    qt_set = token_trigrams(query_token)
    if not qt_set:
        return 0.0

    # Длина слова, за которой |триграммы| == len - 2: дешёвый префильтр по длине
    # до построения множества триграмм. J(w, q) <= min(|w|, |q|)/(max-подобное);
    # точная оценка не нужна — достаточно отсечь заведомо далёкие длины.
    qt_len = len(qt_set)
    max_word_len = int(qt_len / FUZZY_THRESHOLD) + 3

    max_jaccard = 0.0
    # Извлечение всех слов документа длиной >= 3 (чанками с перекрытием)
    chunk_size = max(FUZZY_DOC_CHAR_CAP, 1)
    overlap = min(FUZZY_DOC_CHUNK_OVERLAP, chunk_size - 1)
    pos = 0
    text_len = len(doc_text)
    while pos < text_len:
        chunk = doc_text[pos : pos + chunk_size]
        for w in re.findall(r"[A-Za-zА-Яа-я0-9_]+", chunk):
            if len(w) < 3 or len(w) > max_word_len:
                continue
            w_set = token_trigrams(w)
            if not w_set:
                continue
            intersection = len(qt_set & w_set)
            if not intersection:
                continue
            # Гейты против ложных срабатываний (регрессии E-фазы): короткое
            # пересечение (added~add: 1 общая триграмма) или далёкое слово
            # (cat_keyword~dog_keyword: расстояние редактирования 3) не опечатка.
            # Морфоканал (5бис-1): слова >= FUZZY_MORPHOLOGY_MIN_WORD_LEN букв
            # проходят ослабленный гейт (lev<=3 + Jaccard-порог 0.30 ниже
            # скоринга) — русские флексии не обязаны быть опечатками.
            w_lower = w.lower()
            q_lower = query_token.lower()
            union = len(qt_set | w_set)
            jaccard = intersection / union if union > 0 else 0.0
            if not _word_gate_passes(q_lower, w_lower, intersection, jaccard):
                continue
            if jaccard > max_jaccard:
                max_jaccard = jaccard
                if max_jaccard == 1.0:
                    return max_jaccard
        if pos + chunk_size >= text_len:
            break
        pos += chunk_size - overlap

    return max_jaccard


def short_acronym_matches(short_tokens: set[str], doc_blob: str) -> bool:
    """Точное совпадение коротких акронимов с токенами документа (Ф5).

    Поглощает байт-идентичную идиому из трёх пакетов (knowledge
    ``_collect_short_acronym_knowledge``, audit ``_short_acronym_search``,
    skills ``_collect_short_acronym_skills``): токенизация
    ``[A-Za-zА-Яа-я0-9_-]+`` → lower → под-токены по ``-``/``_`` →
    пересечение с ``short_tokens``.

    Семантика knowledge/audit (только целые токены/под-токены, без
    подстрочного канала). Skills сохраняет свой дополнительный
    substr-канал локально (см. ``_collect_short_acronym_skills``) —
    это осознанная доменная разница (план Ф5: substring остаётся).
    """
    doc_tokens = {tok.lower() for tok in re.findall(r"[A-Za-zА-Яа-я0-9_-]+", doc_blob)}
    split_subtokens: set[str] = set()
    for tok in doc_tokens:
        if "-" in tok or "_" in tok:
            split_subtokens.update(re.split(r"[-_]+", tok))
    return bool(short_tokens & (doc_tokens | split_subtokens))
