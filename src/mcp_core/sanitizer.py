"""Модуль санитизации запросов и Query Expansion для SQLite FTS5.

Предотвращает синтаксические ошибки при запросах с дефисами (-nostdin),
процентами (0%), двоеточиями и кавычками.
"""

import re
from typing import Any

__all__ = [
    "FTS5_RESERVED",
    "LLM_QUERY_STOP_WORDS",
    "SHORT_ACRONYMS",
    "build_multi_field_fts_query",
    "extract_short_acronym_tokens",
    "normalize_category_slug",
    "safe_category_slug",
    "sanitize_fts_query",
    "sanitize_task_id",
    "stem_russian",
    "title_to_slug",
]

from mcp_core.text import (
    LLM_QUERY_STOP_WORDS,
    SHORT_ACRONYMS,
    is_technical_short_token,
    stem_russian,
)

FTS5_RESERVED = {"AND", "OR", "NOT", "NEAR"}


def _is_kept_fts_token(tok: str, *, allow_all_len2: bool) -> bool:
    if len(tok) >= 3 or re.search(r"\d", tok):
        return True
    low = tok.lower()
    return low in SHORT_ACRONYMS or is_technical_short_token(tok, allow_all_len2=allow_all_len2)


def _parse_fts_token(token: str, *, allow_all_len2: bool = False) -> tuple[str, list[str]] | None:
    """Парсит отдельный токен запроса в (clause, variants)."""
    token = token.strip()
    if not token:
        return None

    # Фильтрация коротких шумовых токенов (< 3 символов) с алгоритмическим пропуском технических акронимов/цифр
    if not _is_kept_fts_token(token, allow_all_len2=allow_all_len2):
        return None

    # Если токен содержит дефис, процент или подчеркивание — оборачиваем в двойные кавычки
    if re.search(r"[-_%/]", token):
        token_clean = token.replace('"', "")
        quoted = f'"{token_clean}"'
        return (quoted, [quoted])

    # Обычные слова: только буквы/цифры -> префиксный поиск
    clean_word = re.sub(r"[^\w]+", "", token)
    clean_lower = clean_word.lower()
    if not _is_kept_fts_token(clean_word, allow_all_len2=allow_all_len2):
        return None

    if clean_word.upper() in FTS5_RESERVED:
        term = f'"{clean_word}"'
        return (term, [term])

    variants = [f"{clean_word}*"]
    # Лёгкий русский стемминг: `word* OR stem*` матчит словоформы
    # («оптимизировать» -> «оптимизация» в индексе, «базы» -> «баз»)
    stem = stem_russian(clean_lower)
    if stem and stem != clean_lower:
        variants.append(f"{stem}*")

    clause = f"({' OR '.join(variants)})" if len(variants) > 1 else variants[0]
    return (clause, variants)


def sanitize_fts_query(raw_query: str, operator: str = "OR") -> str:
    """Очищает поисковый запрос от опасных операторов FTS5 и строит безопасное MATCH-выражение.

    1. Удаляет служебные операторы FTS5 (: ^ ") и разделяет пунктуацию (.,!?;:()[]{}).
    2. Отсекает шумовые короткие токены (< 3 символов), алгоритмически сохраняя технические
       акронимы (верхний регистр, цифры, консонантные или короткие запросы).
    3. Закавычивает токены с дефисами, процентами или подчеркиваниями ("-nostdin", "0%").
    4. Добавляет префиксный поиск (*) к алфавитно-цифровым словам (за исключением операторов FTS5)
       и лёгкий русский стемминг (word* OR stem*).
    5. Объединяет токены оператором operator (по умолчанию 'OR', либо 'AND').
    """
    if not raw_query or not raw_query.strip():
        return '""'

    # Заменяем опасные для FTS5 операторы и разделители пунктуации на пробелы,
    # чтобы предотвратить склеивание терминов вокруг пунктуации: foo(bar) -> foo bar, baz,qux -> baz qux.
    # Сохраняем дефисы (-), подчеркивания (_), слэши (/) и знаки процента (%) для составных терминов.
    cleaned = re.sub(r'[:\^"\.()[\]{};,!?+=<>\\|~@#$]+', " ", raw_query)

    # Разбиваем на токены
    raw_tokens = cleaned.split()
    has_long = any(len(re.sub(r"[^\w]+", "", t)) >= 3 for t in raw_tokens)
    parsed_items: list[tuple[str, list[str]]] = []

    for token in raw_tokens:
        parsed = _parse_fts_token(token, allow_all_len2=not has_long)
        if parsed is not None:
            parsed_items.append(parsed)

    if not parsed_items:
        # Fallback на закавыченный исходный запрос
        safe_fallback = re.sub(r"[^\w\s-]+", "", raw_query).strip()
        return f'"{safe_fallback}"' if safe_fallback else '""'

    if operator.upper() == "AND":
        return " AND ".join(item[0] for item in parsed_items)
    else:
        # Для OR разворачиваем варианты без лишних скобок (сохраняет совместимость)
        seen: set[str] = set()
        flattened: list[str] = []
        for item in parsed_items:
            for v in item[1]:
                if v not in seen:
                    seen.add(v)
                    flattened.append(v)
        return " OR ".join(flattened)


def normalize_category_slug(v: Any) -> str:
    """Нормализует имя категории в безопасный slug ([a-z0-9_-]+, длина <= 64)."""
    if hasattr(v, "value") and isinstance(v.value, str):
        v = v.value
    if not isinstance(v, str):
        msg = "Категория должна быть строкой"
        raise ValueError(msg)
    if ".." in v:
        msg = f"Недопустимое имя категории: '{v}'. Обнаружена попытка path traversal ('..')."
        raise ValueError(msg)
    v_clean = v.strip().lower()
    # Заменяем пробелы и любые слэши (/ и \) на дефисы
    v_clean = re.sub(r"[\s/\\]+", "-", v_clean)
    # Удаляем любые спецсимволы кроме a-z, 0-9, _, -
    v_clean = re.sub(r"[^a-z0-9_-]+", "", v_clean).strip("-_")
    if not v_clean or not re.match(r"^[a-z0-9][a-z0-9_-]*$", v_clean) or len(v_clean) > 64:
        msg = (
            f"Недопустимое имя категории: '{v}'. "
            "Категория должна содержать только латинские буквы (ASCII), цифры, дефисы или подчеркивания."
        )
        raise ValueError(msg)
    return v_clean


def title_to_slug(title: str, *, max_len: int = 40, fallback: str = "task") -> str:
    """Генерирует безопасный slug из заголовка для имени файла (Ф4, slug-группа).

    Поглощает две inline-копии audit (synchronizer.py: save_and_index_card /
    save_plan): ``title.lower()`` → пробелы/подчёркивания в дефисы → фильтр
    небезопасных для имени файла символов (остаются ``isalnum`` и дефис;
    ``str.isalnum`` пропускает юникод-буквы, включая кириллицу — фильтруются
    только спецсимволы) → обрезка до ``max_len`` → strip дефисов.
    Пустой результат → ``fallback``.
    """
    clean = title.lower().replace(" ", "-").replace("_", "-")
    clean = "".join(c for c in clean if c.isalnum() or c == "-")[:max_len].strip("-")
    return clean or fallback


def safe_category_slug(v: Any) -> str | None:
    """Мягкая нормализация категории: как normalize_category_slug, но None вместо raise."""
    try:
        return normalize_category_slug(v)
    except ValueError:
        return None


_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def sanitize_task_id(v: Any) -> str:
    """Санитизирует task_id, предотвращая path traversal и несовместимость с ФС.

    Разрешены 1..64 символов ASCII: [A-Za-z0-9][A-Za-z0-9_.-]*, без завершающей точки.
    """
    if not isinstance(v, str):
        msg = "task_id должен быть строкой"
        raise ValueError(msg)
    clean = v.strip()
    if not clean or len(clean) > 64:
        msg = f"Недопустимая длина task_id: '{v}' (ожидается от 1 до 64 символов)"
        raise ValueError(msg)
    if ".." in clean or "/" in clean or "\\" in clean or ":" in clean:
        msg = f"Потенциальный path traversal в task_id: '{v}'"
        raise ValueError(msg)
    if clean.endswith("."):
        msg = f"task_id не может заканчиваться на точку: '{v}'"
        raise ValueError(msg)
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", clean):
        msg = f"task_id содержит недопустимые символы: '{v}' (разрешены только ASCII буквы, цифры, '.', '-', '_')"
        raise ValueError(msg)
    if clean.lower() in _WINDOWS_RESERVED_NAMES:
        msg = f"task_id не может использовать зарезервированное имя ОС: '{v}'"
        raise ValueError(msg)
    return clean


def build_multi_field_fts_query(fields: dict[str, str]) -> str | None:
    """Формирует безопасный многопольный FTS5 MATCH запрос вида `col1 : (terms) OR col2 : (terms)`.

    Принимает словарь {column_name: text}.
    Для каждого поля очищает текст через sanitize_fts_query.
    Поля с пустым санитизированным значением пропускаются.
    Возвращает объединённый запрос через ' OR ' или None, если все поля пусты.
    """
    parts = []
    for col, text in fields.items():
        if not text or not str(text).strip():
            continue
        sanitized = sanitize_fts_query(str(text))
        if sanitized and sanitized != '""':
            parts.append(f"{col} : ({sanitized})")
    if not parts:
        return None
    return " OR ".join(parts)


def extract_short_acronym_tokens(raw_query: str) -> set[str]:
    """Извлекает короткие (< 3 символов) технические токены для точного fallback-поиска."""
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", raw_query)
    has_long = any(len(t) >= 3 for t in tokens)
    return {
        t.lower()
        for t in tokens
        if len(t) < 3
        and (
            t.lower() in SHORT_ACRONYMS or is_technical_short_token(t, allow_all_len2=not has_long)
        )
    }
