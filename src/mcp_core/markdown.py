"""Общее Markdown-ядро для разбора frontmatter и секций (mcp_core, stdlib-only).

Строительные блоки, вынесенные из ``mcp_audit/parser.py`` и
``mcp_knowledge/parser.py`` (план core-dedup, фаза 2):

- :data:`FRONTMATTER_SPLIT_REGEX` — байт-копия регэкспа (M1);
- :func:`split_sections` — параметризованный H2/H3-сплиттер (M2);
- :func:`find_section` / :func:`find_section_key` — keyword-поиск (M3).

Расхождения семейств алгоритмов зафиксированы ручками (инвариант §4.9
плана core-dedup):

- ``header_match``: ``"prefix"`` — литеральное ``## `` (audit: таб после
  ``##`` невалиден, ровно один пробел); ``"regex"`` — ``^#{min,max}\\s+``
  по ``header_levels`` (knowledge: ``\\s+`` допускает таб);
- ``preamble_on_whitespace``: сохранять ли ключ ``_preamble`` при
  whitespace-only теле до первого заголовка (audit — да, knowledge — нет);
- ``store_empty_sections``: сохранять ли секцию для заголовка без единой
  строки тела — подряд идущие заголовки (knowledge — да, audit — нет;
  дополнительная ручка сверх §4.9, чтобы обёртки пакетов сохранили
  поведение байт-в-байт);
- ``fence_aware``: трекинг code-fence, чтобы ``###`` внутри блока кода не
  открывал секцию (только knowledge).

Разбивка строк — ``str.splitlines()`` (как в knowledge); для LF-тел
эквивалент ``split("\\n")`` из audit.

Доменные словари (keywords) остаются в пакетах и передаются аргументами.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass

__all__ = [
    "FRONTMATTER_SPLIT_REGEX",
    "Fence",
    "SectionCollection",
    "SectionNode",
    "check_fence_line",
    "find_section",
    "find_section_key",
    "serialize_section_nodes",
    "split_section_nodes",
    "split_sections",
]

# Байт-копия из mcp_audit/parser.py:25 == mcp_skills/parser.py:16 (M1).
FRONTMATTER_SPLIT_REGEX = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

_FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})([^`~]*)$")
_PREFIX_BY_LEVEL = {2: "## ", 3: "### "}

# Состояние code fence: (символ маркера, длина маркера) или None вне блока.
Fence = tuple[str, int]


def check_fence_line(
    line: str,
    active_fence: Fence | None,
) -> tuple[bool, Fence | None]:
    """Проверяет строку на открытие/закрытие кодового блока по правилам CommonMark.

    active_fence: (fence_char, fence_len) или None, если вне блока.
    Возвращает:
      (is_fence_boundary, new_active_fence)
    где is_fence_boundary=True если строка открыла или закрыла блок.
    """
    m = _FENCE_PATTERN.match(line)
    if not m:
        return False, active_fence

    marker = m.group(1)
    fence_char = marker[0]
    fence_len = len(marker)
    info = m.group(2).strip()

    if active_fence is None:
        # Открытие кодового блока: инфо-строка для ``` не может содержать символ `
        if fence_char == "`" and "`" in info:
            return False, None
        return True, (fence_char, fence_len)

    # Внутри кодового блока: закрытие требует того же символа, длины >= открывающей
    # и отсутствия дополнительного содержимого (кроме пробелов) после маркера
    open_char, open_len = active_fence
    if fence_char == open_char and fence_len >= open_len and not info:
        return True, None

    # Иначе это просто текст внутри кодового блока (например ~~~ внутри ```)
    return False, active_fence


def _match_header(
    line: str,
    *,
    header_match: str,
    header_levels: Sequence[int],
) -> str | None:
    """Возвращает заголовок секции, если строка — заголовок; иначе ``None``."""
    if header_match == "prefix":
        # Литеральное сравнение: только ``## <текст>`` (одиночный пробел),
        # ровно как mcp_audit/parser._parse_h2_sections.
        level = header_levels[0] if header_levels else 2
        prefix = _PREFIX_BY_LEVEL.get(level, "## ")
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
        return None
    levels = sorted(set(header_levels)) or [2]
    pattern = rf"^#{{{min(levels)},{max(levels)}}}\s+(.+)$"
    if m := re.match(pattern, line):
        return m.group(1).strip()
    return None


def split_sections(
    body: str,
    *,
    header_match: str = "prefix",
    header_levels: Sequence[int] = (2,),
    fence_aware: bool = False,
    preamble_key: str = "_preamble",
    preamble_on_whitespace: bool = True,
    store_empty_sections: bool = False,
) -> dict[str, str]:
    """Разбивает тело Markdown на словарь секций по заголовкам.

    Обёртки пакетов фиксируют свои значения ручек дефолтами вызова:

    - audit: ``header_match="prefix"`` (дефолт), ``preamble_on_whitespace=True``
      (дефолт), ``store_empty_sections=False`` (дефолт);
    - knowledge: ``header_match="regex"``, ``header_levels=(2, 3)``,
      ``fence_aware=True``, ``preamble_on_whitespace=False``,
      ``store_empty_sections=True``.
    """
    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []
    active_fence: tuple[str, int] | None = None

    # Итерация строк — в паре с режимом, как в исходных парсерах:
    # prefix/audit — split("\n") (пустое тело даёт [""] → "_preamble": ""),
    # regex/knowledge — splitlines() (CRLF-толерантен, пустое тело → []).
    lines = body.split("\n") if header_match == "prefix" else body.splitlines()

    for line in lines:
        if fence_aware:
            is_fence, active_fence = check_fence_line(line, active_fence)
            if is_fence:
                current_lines.append(line)
                continue

        header = (
            None
            if active_fence is not None
            else _match_header(
                line,
                header_match=header_match,
                header_levels=header_levels,
            )
        )
        if header is not None:
            _flush(
                sections,
                current_heading,
                current_lines,
                preamble_key=preamble_key,
                preamble_on_whitespace=preamble_on_whitespace,
                store_empty_sections=store_empty_sections,
            )
            current_heading = header
            current_lines = []
        else:
            current_lines.append(line)

    _flush(
        sections,
        current_heading,
        current_lines,
        preamble_key=preamble_key,
        preamble_on_whitespace=preamble_on_whitespace,
        store_empty_sections=store_empty_sections,
    )
    return sections


def _flush(
    sections: dict[str, str],
    heading: str | None,
    lines: list[str],
    *,
    preamble_key: str,
    preamble_on_whitespace: bool,
    store_empty_sections: bool,
) -> None:
    """Записывает накопленную секцию (или преамбулу) в словарь."""
    text = "\n".join(lines).strip()
    if heading is None:
        # Преамбула до первого заголовка: audit пишет ключ при любых
        # непустых lines, knowledge — только при содержательном тексте.
        if lines and (preamble_on_whitespace or any(item.strip() for item in lines)):
            sections[preamble_key] = text
    elif lines or store_empty_sections:
        # Audit пропускает заголовок без строк тела; knowledge пишет "".
        sections[heading] = text


def find_section(
    sections: Mapping[str, str],
    *keywords: str,
    skip_preamble: bool = True,
) -> str | None:
    """Ищет содержимое секции по совпадению ключевых слов в заголовке.

    Возврат ``str | None`` (конвенция audit); обёртка knowledge делает
    ``or ""``. ``skip_preamble``: у audit ключ ``_preamble`` не
    фильтровался, но не содержит доменных keyword — эквивалентно (§M3).
    """
    for heading, text in sections.items():
        if skip_preamble and heading == "_preamble":
            continue
        heading_lower = heading.lower()
        if any(kw.lower() in heading_lower for kw in keywords):
            return text
    return None


def find_section_key(
    sections: Mapping[str, str],
    *keywords: str,
    skip_preamble: bool = True,
    default: str | None = None,
) -> str | None:
    """Ищет название секции по ключевым словам (нечувствительно к регистру)."""
    for heading in sections:
        if skip_preamble and heading == "_preamble":
            continue
        heading_lower = heading.lower()
        if any(kw.lower() in heading_lower for kw in keywords):
            return heading
    return default


# ---------------------------------------------------------------------------
# K9 (план AUDIT-VERIFY-2026-10): узлы секций с сохранением уровня заголовка.
# ---------------------------------------------------------------------------


@dataclass
class SectionNode:
    """Одна секция Markdown: уровень заголовка, название и тело.

    Уровень сохраняется при round-trip — мутация карточки (статусный апдейт,
    append_note) больше не превращает исходные ``##``-секции в ``###``.
    """

    level: int
    title: str
    body: str

    def prefix(self) -> str:
        """Литеральный префикс заголовка: ``'## '`` для level=2."""
        return "#" * self.level + " "


def split_section_nodes(
    body: str,
    *,
    header_match: str = "prefix",
    header_levels: Sequence[int] = (2,),
    fence_aware: bool = False,
    preamble_key: str = "_preamble",
    preamble_on_whitespace: bool = True,
    store_empty_sections: bool = False,
) -> list[SectionNode]:
    """Разбивает тело Markdown на узлы секций с уровнями заголовков.

    Воспроизводит все 6 ручек :func:`split_sections` (байт-идентичность
    ``title``/``body`` сохранена: сплиттер один и тот же, добавлен только
    трекинг уровня).
    """
    lines = body.split("\n") if header_match == "prefix" else body.splitlines()
    nodes: list[SectionNode] = []
    current_title: str | None = None
    current_level: int = header_levels[0] if header_levels else 2
    current_lines: list[str] = []
    active_fence: tuple[str, int] | None = None

    def _flush(heading: str | None, level: int, chunk: list[str]) -> None:
        text = "\n".join(chunk).strip()
        if heading is None:
            if chunk and (preamble_on_whitespace or any(item.strip() for item in chunk)):
                nodes.append(SectionNode(level=0, title=preamble_key, body=text))
        elif chunk or store_empty_sections:
            nodes.append(SectionNode(level=level, title=heading, body=text))

    for line in lines:
        if fence_aware:
            is_fence, active_fence = check_fence_line(line, active_fence)
            if is_fence:
                current_lines.append(line)
                continue

        header = (
            None
            if active_fence is not None
            else _match_header(
                line,
                header_match=header_match,
                header_levels=header_levels,
            )
        )
        if header is not None:
            _flush(current_title, current_level, current_lines)
            level = len(line) - len(line.lstrip("#"))
            current_title = header
            current_level = level
            current_lines = []
        else:
            current_lines.append(line)

    _flush(current_title, current_level, current_lines)
    return nodes


def serialize_section_nodes(nodes: Sequence[SectionNode]) -> str:
    """Сериализует узлы обратно в Markdown с исходными уровнями заголовков.

    Preamble-узел (level=0) пишется без заголовка. Секция без тела
    сериализуется заголовком без пустого тела (round-trip устойчив).
    """
    lines: list[str] = []
    for node in nodes:
        if node.level == 0:
            if node.body:
                lines.append(node.body)
                lines.append("")
            continue
        lines.append(f"{node.prefix()}{node.title}")
        if node.body:
            lines.append(node.body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class SectionCollection(MutableMapping[str, str]):
    """Mapping-обёртка над узлами секций (двусторонняя).

    Контракт словаря сохранён для существующих use-cases (K9): ``__getitem__``
    возвращает body, ``__setitem__`` — upsert с СОХРАНЕНИЕМ уровня заголовка
    существующей секции (или default_level для новой), ``keys``/``items`` —
    названия/пары как у словаря. Preamble хранится под ``_preamble``.
    Round-trip serialize сохраняет уровни всех узлов.

    Divergence от dict (aud-backend-5377): при дублирующихся заголовках
    во входном Markdown коллекция хранит ВСЕ узлы (serialize выводит
    каждый), но ``__getitem__``/``get`` возвращают ПЕРВЫЙ узел с этим
    заголовком, а не последний (dict давал last-wins). Заголовки в
    карточках ожидаемо уникальны, поэтому расхождение проявляется
    только на нестандартном входе.
    """

    def __init__(
        self,
        nodes: Sequence[SectionNode] | None = None,
        *,
        default_level: int = 3,
    ) -> None:
        self._nodes: list[SectionNode] = list(nodes) if nodes else []
        self._default_level = default_level

    # --- Sequence-of-nodes API ---
    @property
    def nodes(self) -> list[SectionNode]:
        return self._nodes

    @classmethod
    def from_body(
        cls,
        body: str,
        *,
        header_match: str = "regex",
        header_levels: Sequence[int] = (2, 3),
        fence_aware: bool = True,
        preamble_on_whitespace: bool = False,
        store_empty_sections: bool = True,
    ) -> SectionCollection:
        nodes = split_section_nodes(
            body,
            header_match=header_match,
            header_levels=header_levels,
            fence_aware=fence_aware,
            preamble_on_whitespace=preamble_on_whitespace,
            store_empty_sections=store_empty_sections,
        )
        return cls(nodes)

    # --- Mapping API (контракт K9: use-cases без правок кода) ---
    def __getitem__(self, key: str) -> str:
        for node in self._nodes:
            if node.title == key:
                return node.body
        raise KeyError(key)

    def __setitem__(self, key: str, value: str) -> None:
        for node in self._nodes:
            if node.title == key:
                node.body = value
                return
        self._nodes.append(SectionNode(level=self._default_level, title=key, body=value))

    def __delitem__(self, key: str) -> None:
        for i, node in enumerate(self._nodes):
            if node.title == key:
                del self._nodes[i]
                return
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter([node.title for node in self._nodes])

    def __len__(self) -> int:
        return len(self._nodes)

    def serialize(self) -> str:
        """Сериализует тело карточки (без frontmatter) с исходными уровнями."""
        return serialize_section_nodes(self._nodes)
