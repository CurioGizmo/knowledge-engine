"""CardCodec — единый frontmatter-кодек карточек (Ф3 плана core-dedup v4).

Примитивы parse/dump/rebuild frontmatter, поглощающие разошедшиеся копии
парсеров пакетов. Контракт (план, Ф3):

- вход — RAW-текст, не stripped (обёртки пакетов стрипают сами);
- ``parse_frontmatter`` → ``dict | None``: CRLF+EOF-толерантный разбор
  (регэксп-семейство knowledge: ``\\r?\\n`` и ``(?:\\r?\\n|$)``), non-dict
  валидный YAML и невалидный YAML → доменная ошибка
  :class:`FrontmatterError`;
- ``dump_frontmatter``: CRLF→LF на записи, нормализация ``\\r\\n`` в
  строковых значениях (кейс 21: дамп эскейпит ``\\r`` навсегда — v2
  нормализует при сборке меты);
- ``rebuild_card``: двойная сборка меты (parser — условно,
  synchronizer — безусловно) выносится в мутатор: обёртка сама решает,
  обновлять ли поле.

Модуль НЕ входит в stdlib-only-набор ядра: единственная сторонняя
зависимость — PyYAML (общая для всех MCP-пакетов). Ошибки — доменные
классы, чтобы обёртки пакетов не пропускали raw ParserError/ScannerError
наружу (кейсы 13/20 матрицы: v2_target = domain-error).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import yaml

__all__ = [
    "FRONTMATTER_TOLERANT",
    "FrontmatterError",
    "dump_frontmatter",
    "parse_frontmatter",
    "rebuild_card",
    "split_frontmatter",
]

# CRLF+EOF-толерантный сплиттер (семейство knowledge: байт-совместим по
# валидным путям с FRONTMATTER_SPLIT_REGEX, плюс:
#   - `\r?\n` допускает CRLF-сепараторы (кейсы 02/22);
#   - `(?:\r?\n|$)` допускает EOF сразу после закрывающего `---` (кейс 04b/22).
# Реэкспорт для пакетов-потребителей: audit-обёртка различает «нет блока»
# и «блок есть, но жёсткий сплиттер его не матчит» (кейсы 04b/22 → ok).
FRONTMATTER_TOLERANT = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)$",
    re.DOTALL,
)


class FrontmatterError(ValueError):
    """Доменная ошибка формата frontmatter (v2_target: domain-error).

    Единый род для: отсутствия/некорректного блока, невалидного YAML,
    non-dict корня. Обёртки пакетов мапят её в свои доменные ошибки
    (InvalidSkillFormatError / InvalidFlowFormatError / ValueError).
    """


def parse_frontmatter(raw: str) -> dict[str, Any] | None:
    """Разбирает frontmatter-блок Markdown: ``dict`` мета | ``None``.

    Контракт: raw-текст без предварительного strip (обёртка стрипает).
    Тонкая обёртка над :func:`split_frontmatter` (мета без тела).

    None — блок frontmatter отсутствует (flow: опциональный frontmatter).
    FrontmatterError — блок сломан: невалидный YAML, non-dict корень,
    отсутствует закрывающий разделитель.
    """
    meta, _body = split_frontmatter(raw)
    return meta


def split_frontmatter(raw: str) -> tuple[dict[str, Any] | None, str]:
    """Разделяет карточку на (мета, тело) одним матчем толерантного регэкспа.

    Композиция parse_frontmatter + body-извлечение без двойного матча
    (замечание ревью Ф3/audit): обёртки пакетов получают и мету, и тело
    за один проход. Семантика слоёв идентична :func:`parse_frontmatter`:
    ``FrontmatterError`` на сломанном блоке, ``(None, raw)`` — блок
    отсутствует.
    """
    match = FRONTMATTER_TOLERANT.match(raw)
    if match is None:
        # Различаем «нет блока» и «сломанный блок» по ведущему `---`
        # (после снятия BOM-префикса, кейс 11) — guard ниже поднимает
        # FrontmatterError для сломанного блока, «нет блока» → None.
        if raw.lstrip("\ufeff").startswith("---"):
            raise FrontmatterError(
                "Некорректный блок frontmatter: блок не соответствует формату "
                "'---\\n<meta>\\n---' (маркер с мусорным префиксом или без закрытия)"
            )
        return None, raw
    yaml_content = match.group(1)
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        raise FrontmatterError(f"Невалидный YAML в frontmatter: {e}") from e
    except ValueError as e:
        # Ревью Ф3-финал: PyYAML поднимает голый ValueError на невалидных
        # дат-литералах (2026-13-01: «month must be in 1..12») — не ParserError.
        # Без обёртки он утекал наружу как сырой ValueError мимо доменной
        # ошибки (регрессия: skills обязан мапить в InvalidSkillFormatError).
        raise FrontmatterError(f"Невалидный YAML в frontmatter: {e}") from e
    if data is None:
        # Пустая мета (кейс 08): доменная ошибка — «пустой блок меты».
        raise FrontmatterError("Frontmatter пуст: ожидается YAML-словарь")
    if not isinstance(data, dict):
        raise FrontmatterError("Frontmatter не является YAML-словарём")
    return data, match.group(2)


def dump_frontmatter(meta: dict[str, Any]) -> str:
    """Сериализует мету в текст YAML-блока frontmatter (без `---`).

    Канонические параметры (пин тестами: мутация M6):
    ``sort_keys=False, allow_unicode=True, default_flow_style=False``.
    Нормализация (v2): CRLF→LF в строковых значениях и итоговом тексте —
    кейс 21: raw `\\r` в значении не должен эскейпиться навсегда.

    Потребители (ревью Ф3-финал): сериализаторы пакетов обязаны собирать
    YAML-блок меты через этот примитив, а не сырой ``yaml.dump`` — только
    так CRLF-нормализация v2 реально применяется на записи.
    """
    normalized = _normalize_crlf_values(meta)
    text = yaml.dump(
        normalized,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return text.replace("\r\n", "\n")


def _normalize_crlf_values(meta: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно заменяет CRLF→LF в строках и строковых значениях меты."""

    def norm(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("\r\n", "\n").replace("\r", "\n")
        if isinstance(value, dict):
            return {k: norm(v) for k, v in value.items()}
        if isinstance(value, list):
            return [norm(v) for v in value]
        return value

    return {k: norm(v) for k, v in meta.items()}


def rebuild_card(
    text: str,
    meta_mutator: Callable[[dict[str, Any]], dict[str, Any]],
) -> str:
    """Пересобирает карточку: parse → мутация меты → dump → сборка.

    Двойная сборка меты плана audit (parser — условно, synchronizer —
    безусловно, drift updated_at) сводится к одному механизму: обёртка
    передаёт мутатор, решающий, обновлять ли поле.
    """
    meta, body = split_frontmatter(text)
    if meta is None:
        raise FrontmatterError("rebuild_card: карточка без frontmatter-блока")
    mutated = meta_mutator(meta)
    dump = dump_frontmatter(mutated).strip()

    return f"---\n{dump}\n---\n{body}"
