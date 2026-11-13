"""Characterization-тесты общего Markdown-ядра ``mcp_core/markdown`` (фаза 2 плана core-dedup).

Снимки поведения сняты ДО миграции с исходных парсеров:

- ``header_match="prefix"`` + дефолты == ``mcp_audit/parser._parse_h2_sections``;
- ``header_match="regex", header_levels=(2, 3), fence_aware=True,
  preamble_on_whitespace=False, store_empty_sections=True`` ==
  секционный цикл ``mcp_knowledge/parser.parse_card_markdown``.

Матрица ``header_match`` × ``preamble_on_whitespace`` обязательна
(инвариант §4.9); CRLF-фикстура — синтетическая.
"""

from mcp_core.markdown import (
    FRONTMATTER_SPLIT_REGEX,
    find_section,
    find_section_key,
    split_sections,
)

# Параметры knowledge-семейства (обёртка parse_card_markdown).
_KN = dict(  # noqa: C408
    header_match="regex",
    header_levels=(2, 3),
    fence_aware=True,
    preamble_on_whitespace=False,
    store_empty_sections=True,
)

BODY = "### Симптом / Контекст\nFFmpeg зависает.\n\n### Решение / Выводы\nФлаг -nostdin.\n"
PREAMBLE_BODY = "> Внимание: заметка\n\n## Заголовок\nТекст.\n"


def test_frontmatter_split_regex_byte_identical_contract() -> None:
    """M1: регэксп разрезает frontmatter; байт-идентичен исходникам audit/skills."""
    assert FRONTMATTER_SPLIT_REGEX.pattern == r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    m = FRONTMATTER_SPLIT_REGEX.match("---\nid: x\n---\nbody\n")
    assert m is not None
    assert m.group(1) == "id: x"
    assert m.group(2) == "body\n"


def test_prefix_mode_matches_audit_parser() -> None:
    """Дефолт split_sections == _parse_h2_sections (audit): H2-префикс, преамбула при whitespace."""
    # Снимок с audit: whitespace-only преамбула сохраняет ключ "_preamble"
    body = "\n\n## ❌ Симптом\nБуфер.\n"
    assert split_sections(body) == {"_preamble": "", "❌ Симптом": "Буфер."}

    # Заголовки подряд: секция без строк тела пропускается
    assert split_sections("## Один\n## Два\nТекст\n") == {"Два": "Текст"}

    # Пустое/whitespace-only тело — {"_preamble": ""}: split("\n") даёт [""].
    assert split_sections("") == {"_preamble": ""}


def test_regex_mode_matches_knowledge_parser() -> None:
    """KN-параметры == секционный цикл parse_card_markdown (knowledge)."""
    assert split_sections(BODY, **_KN) == {
        "Симптом / Контекст": "FFmpeg зависает.",
        "Решение / Выводы": "Флаг -nostdin.",
    }

    # Преамбула: knowledge создаёт ключ только при содержательном тексте
    assert split_sections(PREAMBLE_BODY, **_KN) == {
        "_preamble": "> Внимание: заметка",
        "Заголовок": "Текст.",
    }
    assert split_sections("\n\n## Заголовок\nТекст\n", **_KN) == {"Заголовок": "Текст"}

    # Заголовки подряд: knowledge пишет пустую секцию ""
    assert split_sections("## Один\n## Два\nТекст\n", **_KN) == {"Один": "", "Два": "Текст"}


def test_matrix_header_match_and_preamble_on_whitespace() -> None:
    """Обязательная матрица §4.9: обе ручки × оба значения на одном входе."""
    body = "\n\n## Заголовок\nТекст\n"  # whitespace-only преамбула
    matrix = {
        ("prefix", True): {"_preamble": "", "Заголовок": "Текст"},
        ("prefix", False): {"Заголовок": "Текст"},
        ("regex", True): {"_preamble": "", "Заголовок": "Текст"},
        ("regex", False): {"Заголовок": "Текст"},
    }
    for (mode, ws), expected in matrix.items():
        got = split_sections(body, header_match=mode, preamble_on_whitespace=ws)
        assert got == expected, (mode, ws, got)

    # Таб после ##: валиден для regex (\s+), невалиден для prefix
    tab_body = "##\tЗаголовок\nТекст\n"
    assert split_sections(tab_body, header_match="regex", header_levels=(2,)) == {
        "Заголовок": "Текст"
    }
    # prefix: ``##\t`` не матчится -> всё уходит в преамбулу
    assert split_sections(tab_body) == {"_preamble": "##\tЗаголовок\nТекст"}

    # ## без пробела — не заголовок ни в одном режиме (regex требует \s+)
    nospace = "##Заголовок\nТекст\n"
    assert split_sections(nospace, header_match="regex", header_levels=(2,)) == {
        "_preamble": "##Заголовок\nТекст"
    }
    assert split_sections(nospace) == {"_preamble": "##Заголовок\nТекст"}


def test_header_levels_regex_h2_h3() -> None:
    """header_levels=(2, 3): и H2, и H3 — заголовки (семантика knowledge)."""
    body = "## H2\nтекст\n\n### H3\nещё\n"
    assert split_sections(body, header_match="regex", header_levels=(2, 3)) == {
        "H2": "текст",
        "H3": "ещё",
    }
    # H4 не входит в уровни — остаётся телом секции
    body_h4 = "## H2\n#### H4\nтекст\n"
    assert split_sections(body_h4, header_match="regex", header_levels=(2, 3)) == {
        "H2": "#### H4\nтекст"
    }


def test_fence_aware_header_inside_code_block() -> None:
    """fence_aware=True: ### внутри блока кода не открывает секцию (knowledge)."""
    body = "## Решение / Выводы\n```bash\n### Комментарий\nffmpeg -nostdin\n```\n"
    res = split_sections(body, **_KN)
    assert res == {"Решение / Выводы": "```bash\n### Комментарий\nffmpeg -nostdin\n```"}

    # fence_aware=False (audit-семейство): ### внутри fence был бы заголовком
    res_plain = split_sections(body, header_match="regex", header_levels=(2, 3))
    assert res_plain == {
        "Решение / Выводы": "```bash",
        "Комментарий": "ffmpeg -nostdin\n```",
    }


def test_crlf_body_synthetic_fixture() -> None:
    """Синтетическая CRLF-фикстура: splitlines() съедает \\r — секции идентичны LF."""
    lf = split_sections(BODY, **_KN)
    crlf = split_sections(BODY.replace("\n", "\r\n"), **_KN)
    assert crlf == lf

    # CRLF-тело для prefix-режима: splitlines() тоже нормализует
    audit_body = "## Симптом\r\nТекст\r\n"
    assert split_sections(audit_body) == {"Симптом": "Текст"}


def test_find_section_and_key_conventions() -> None:
    """find_section/find_key: lowercase-substring, skip _preamble, str | None."""
    sections = {
        "_preamble": "шум",
        "Симптом / Контекст": "FFmpeg зависает.",
        "Решение / Выводы": "Флаг -nostdin.",
    }
    assert find_section(sections, "симптом") == "FFmpeg зависает."
    assert find_section(sections, "выводы") == "Флаг -nostdin."
    assert find_section(sections, "preamble") is None  # _preamble пропущен
    assert find_section(sections, "нет такого") is None

    assert find_section_key(sections, "решение") == "Решение / Выводы"
    assert find_section_key(sections, "нет такого") is None
    assert find_section_key(sections, "нет такого", default="Лог") == "Лог"

    # skip_preamble=False возвращает преамбулу по её собственному ключу
    assert find_section(sections, "preamble", skip_preamble=False) == "шум"


def test_find_section_empty_sections_store_empty() -> None:
    """Пустые секции (store_empty_sections=True) участвуют в поиске как \"\"."""
    sections = split_sections("## Один\n## Симптом\nТекст\n", **_KN)
    assert sections == {"Один": "", "Симптом": "Текст"}
    assert find_section(sections, "один") == ""
    assert find_section_key(sections, "один") == "Один"
