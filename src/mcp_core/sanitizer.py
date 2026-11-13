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

FTS5_RESERVED = {"AND", "OR", "NOT", "NEAR"}
SHORT_ACRONYMS = {
    "ai",
    "ml",
    "ui",
    "os",
    "db",
    "ci",
    "cd",
    "go",
    "js",
    "ts",
    "py",
    "ip",
    "io",
    "s3",
    "vm",
    "pr",
    "qa",
    # Частотные технические акронимы 2 букв: uv/ux (инструментарий),
    # di/ws (dependency injection, websocket), fk/id/fs (СУБД), tg/vk (мессенджеры).
    # Префикс id* коллизирует с idea/identity — приемлемо при OR-семантике запроса.
    "di",
    "fk",
    "fs",
    "id",
    "tg",
    "uv",
    "ux",
    "vk",
    "ws",
    "бд",
    "ии",
}

# Стоп-слова разговорных и вводных LLM-запросов (вопросы, вежливость, мета-команды).
# Отфильтровываются при наличии других смысловых терминов, чтобы не размывать FTS-выдачу.
LLM_QUERY_STOP_WORDS = frozenset(
    {
        # Русские вопросительные, разговорные и модальные слова
        "как",
        "мне",
        "нужно",
        "надо",
        "пожалуйста",
        "подскажи",
        "подскажите",
        "помоги",
        "помогите",
        "что",
        "делать",
        "почему",
        "зачем",
        "скажи",
        "скажите",
        "найди",
        "найдите",
        "покажи",
        "покажите",
        "хочу",
        "может",
        "можно",
        "быть",
        "будет",
        "какой",
        "какая",
        "какие",
        "какое",
        "каких",
        "каким",
        "этого",
        "этом",
        "эти",
        "это",
        "все",
        "всё",
        "при",
        "для",
        "или",
        # Английские вопросительные и служебные слова
        "how",
        "what",
        "why",
        "where",
        "when",
        "who",
        "which",
        "please",
        "help",
        "tell",
        "show",
        "find",
        "should",
        "could",
        "would",
        "must",
        "need",
        "the",
        "and",
        "for",
        "with",
        "about",
        "from",
        "into",
        "that",
        "this",
        "does",
        "have",
        "has",
    }
)

# Курируемые списки. Поиск суффикса — самое длинное совпадение конца слова.
_RU_VERB_SUFFIXES = (  # глагольные
    "ировать",
    "ывать",
    "ивать",
    "овать",
    "евать",
    "нуть",
    "ать",
    "ять",
    "еть",
    "ить",
    "ла",
    "ло",
    "ли",
    "ет",
    "ут",
    "ют",
    "ит",
    "ат",
    "ят",
    "ен",
    "на",
    "но",
    "ны",
)
_RU_NOUN_ADJ_SUFFIXES = (  # сущ./прилаг., включая -ия/-ии для -ция/-ние слов
    "иями",
    "ями",
    "ами",
    "иях",
    "ях",
    "ах",
    "ого",
    "его",
    "ому",
    "ему",
    "ыми",
    "ими",
    "ия",
    "ии",
    "ов",
    "ев",
    "ам",
    "ям",
    "ой",
    "ей",
    "ий",
    "ый",
    "ое",
    "ее",
    "ые",
    "ие",
    "ом",
    "ем",
    "ым",
    "им",
    "ых",
    "их",
    "ую",
    "юю",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "е",
)
_RU_ALL_SUFFIXES = _RU_VERB_SUFFIXES + _RU_NOUN_ADJ_SUFFIXES
_RU_ADJ_SUFFIXES = frozenset(
    {"ый", "ой", "ий", "ого", "его", "ому", "ему", "ыми", "ими", "ым", "им", "ых", "их", "ом", "ем"}
)  # прилагательные (для н-эпентезы)
_RU_STOP_STEMS = frozenset({"котор", "потом", "толь"})  # супер-частотные стемы


# R6: суффиксы -ат/-ят исключены из среза полностью («результат»/«кандидат» —
# корневое -ат; см. докстринг stem_russian, правило 7).
_RU_FORBIDDEN_CUT_SUFFIXES = ("ат", "ят")


def _best_ru_suffix(stem: str) -> str | None:
    """Самое длинное допустимое суффикс-совпадение конца слова.

    Правила фильтрации (докстринг stem_russian, п.4 и п.7):
    - -ат/-ят не срезаются никогда (R6);
    - однобуквенные окончания после «к» не срезаются (склейки «разработк»).
    """
    best: str | None = None
    for suffix in _RU_ALL_SUFFIXES:
        if suffix in _RU_FORBIDDEN_CUT_SUFFIXES:
            continue
        if stem.endswith(suffix) and (best is None or len(suffix) > len(best)):
            best = suffix

    if best is not None and len(best) == 1 and stem[-2:-1] == "к":
        return None
    return best


def stem_russian(word: str) -> str | None:
    """Лёгкий суффикс-стемминг русского слова для FTS5-префиксного поиска.

    Возвращает основу слова (стем) либо None, если стемминг не применим
    (слово короткое, латиница, опасная склейка или супер-частотный стем).

    Правила:
    1. Вход: слово в нижнем регистре, длина >= 6, содержит кириллицу.
    2. Минимальная длина стема: 5 симв. для слов 6-8 букв, 4 симв. для слов >= 9.
    3. Минимальный суммарный срез: >= 2 симв. для слов 6-8 букв (одиночные
       окончания -а/-ы/-е не стрипаются — защита от склеек «сервере»/«запросы»),
       >= 1 симв. для слов >= 9. Порог проверяется кумулятивно после каждого прохода.
    4. Стриппинг итеративный, максимум 2 прохода, самое длинное совпадение
       из _RU_ALL_SUFFIXES. Однобуквенные окончания не срезаются, если перед ними
       стоит «к»: деривационный суффикс -к- даёт склеивающие основы
       («разработку» -> «разработк», как и отвергнутое «ошибка» -> «ошибк»).
    5. Н-эпентеза прилагательных: после среза ый/ой/ий (проход 1) стем на «н»
       может дополнительно терять «н» на проходе 2, если обычный суффикс не совпал.
       Сводит «архитектурный» -> «архитектур» (общая основа с «архитектура»).
    6. Итоговый стем не должен попадать в _RU_STOP_STEMS («котор» и пр.).
    7. R6 (2026-10-19, отчёт измерений): суффиксы -ат/-ят не срезаются НИКОГДА.
       В существительных «результат/кандидат/сертификат» -ат — часть корня;
       срез порождал стемы «результ/кандид/сертифик» и ложные склейки чужих
       лексем («сертификат»~«сертификатор»~«сертификация»). Глагольные формы
       3-го лица мн.ч. реального корпуса покрываются -ут/-ют (см. «работают»).
    """
    if not word or len(word) < 6:
        return None
    if not re.search(r"[а-яё]", word):
        return None

    # Пороги инвариантов зависят от длины слова
    min_stem_len = 5 if len(word) <= 8 else 4
    min_cut_total = 2 if len(word) <= 8 else 1

    stem = word
    total_cut = 0
    adj_suffix_cut = False  # на проходе 1 срезан суффикс прилагательного (ый/ой/ий)

    for pass_no in range(2):
        # Самое длинное совпадение конца слова из общего списка суффиксов
        best = _best_ru_suffix(stem)

        # R6: одиночная гласная не срезается ВТОРЫМ проходом после полного
        # суффикса первым (правило 3 докстринга «одиночные окончания не
        # стрипаются»). «работают» -> «работа» (не «работ»): глагол обязан
        # склеиваться с отглагольным существительным, как и было до бага.
        if (
            pass_no == 1
            and best is not None
            and len(best) == 1
            and best in "аеиоуыэюя"
            and total_cut >= 2
        ):
            break

        cut_len = 0
        if best is not None:
            cut_len = len(best)
        elif (
            pass_no == 1 and adj_suffix_cut and stem.endswith("н") and len(stem) - 1 >= min_stem_len
        ):
            # Н-эпентеза прилагательных: обычный суффикс не совпал,
            # на проходе 1 срезан ый/ой/ий и стем заканчивается на «н»
            cut_len = 1

        if cut_len == 0:
            break

        new_total = total_cut + cut_len
        new_stem_len = len(stem) - cut_len
        # Инварианты по итогу прохода: кумулятивный срез и длина стема
        if new_total < min_cut_total or new_stem_len < min_stem_len:
            break

        if best is not None and best in _RU_ADJ_SUFFIXES:
            adj_suffix_cut = True
        stem = stem[:-cut_len]
        total_cut = new_total

    if total_cut < min_cut_total or len(stem) < min_stem_len:
        return None
    if stem in _RU_STOP_STEMS:
        return None
    if stem == word:
        return None
    return stem


def _parse_fts_token(token: str) -> tuple[str, bool, list[str]] | None:
    """Парсит отдельный токен запроса в (clause, is_stop, variants)."""
    token = token.strip()
    if not token:
        return None

    lower_token = token.lower()

    # Фильтрация токенов короче 3 символов (предлоги "на", "в", "on", "in", "to")
    # Исключение: токены с цифрами/процентами вроде "0%" или технические акронимы (ui, ml, db, ci)
    if len(token) < 3 and not re.search(r"\d", token) and lower_token not in SHORT_ACRONYMS:
        return None

    is_stop = lower_token in LLM_QUERY_STOP_WORDS

    # Если токен содержит дефис, процент или подчеркивание — оборачиваем в двойные кавычки
    if re.search(r"[-_%/]", token):
        token_clean = token.replace('"', "")
        quoted = f'"{token_clean}"'
        return (quoted, is_stop, [quoted])

    # Обычные слова: только буквы/цифры -> префиксный поиск
    clean_word = re.sub(r"[^\w]+", "", token)
    clean_lower = clean_word.lower()
    if not (len(clean_word) >= 3 or re.search(r"\d", clean_word) or clean_lower in SHORT_ACRONYMS):
        return None

    if clean_lower in LLM_QUERY_STOP_WORDS:
        is_stop = True

    if clean_word.upper() in FTS5_RESERVED:
        term = f'"{clean_word}"'
        return (term, is_stop, [term])

    variants = [f"{clean_word}*"]
    # Лёгкий русский стемминг: `word* OR stem*` матчит словоформы
    # («оптимизировать» -> «оптимизация» в индексе)
    stem = stem_russian(clean_lower)
    if stem and stem != clean_lower:
        variants.append(f"{stem}*")
    elif clean_lower.endswith("ы") and len(clean_lower) == 5:
        variants.append(f"{clean_lower[:-1]}*")

    clause = f"({' OR '.join(variants)})" if len(variants) > 1 else variants[0]
    return (clause, is_stop, variants)


def sanitize_fts_query(raw_query: str, operator: str = "OR") -> str:
    """Очищает поисковый запрос от опасных операторов FTS5 и строит безопасное MATCH-выражение.

    1. Удаляет служебные операторы FTS5 (: ^ ") и разделяет точки (.).
    2. Отсекает токены короче 3 символов (кроме технических акронимов и цифр).
    3. Отфильтровывает стоп-слова LLM-запросов (если в запросе есть смысловые термины).
    4. Закавычивает токены с дефисами, процентами или подчеркиваниями ("-nostdin", "0%").
    5. Добавляет префиксный поиск (*) к алфавитно-цифровым словам (за исключением операторов FTS5)
       и лёгкий русский стемминг (word* OR stem*).
    6. Объединяет токены оператором operator (по умолчанию 'OR', либо 'AND').
    """
    if not raw_query or not raw_query.strip():
        return '""'

    # Удаляем опасные для синтаксиса операторы FTS5 и заменяем точки на пробелы,
    # чтобы предотвратить склеивание терминов (sqlite3.OperationalError -> sqlite3, OperationalError)
    cleaned = raw_query.replace(":", " ").replace("^", " ").replace('"', " ").replace(".", " ")

    # Разбиваем на токены
    raw_tokens = cleaned.split()
    parsed_items: list[tuple[str, bool, list[str]]] = []

    for token in raw_tokens:
        parsed = _parse_fts_token(token)
        if parsed is not None:
            parsed_items.append(parsed)

    if not parsed_items:
        # Fallback на закавыченный исходный запрос
        safe_fallback = re.sub(r"[^\w\s-]+", "", raw_query).strip()
        return f'"{safe_fallback}"' if safe_fallback else '""'

    # Если есть не-стоп-слова, удаляем стоп-слова
    non_stop = [item for item in parsed_items if not item[1]]
    kept = non_stop if non_stop else parsed_items

    if operator.upper() == "AND":
        return " AND ".join(item[0] for item in kept)
    else:
        # Для OR разворачиваем варианты без лишних скобок (сохраняет совместимость)
        seen: set[str] = set()
        flattened: list[str] = []
        for item in kept:
            for v in item[2]:
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
    """Извлекает токены < 3 символов из SHORT_ACRONYMS для точного fallback-поиска."""
    return {
        t.lower()
        for t in re.findall(r"[A-Za-zА-Яа-я0-9]+", raw_query)
        if len(t) < 3 and t.lower() in SHORT_ACRONYMS
    }
