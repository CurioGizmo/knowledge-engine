"""Ф3 (core-dedup v4): юнит-тесты card_codec — dump/rebuild и контракт parse.

Срез 3 (audit): ревью срезов 1–2 отметило, что ``dump_frontmatter`` и
``rebuild_card`` не имели прямых юнит-тестов (покрытие было только через
golden-матрицу parse-веток). Здесь пинится контракт сериализации:

- канонические параметры дампа (``sort_keys=False, allow_unicode=True,
  default_flow_style=False``) — мутация M6;
- CRLF→LF-нормализация в строковых значениях (кейс 21: raw ``\\r`` не
  эскейпится навсегда);
- rebuild_card: parse → мутатор → dump → сборка (round-trip тела).
"""

from __future__ import annotations

import pytest

from mcp_core.card_codec import (
    FrontmatterError,
    dump_frontmatter,
    parse_frontmatter,
    rebuild_card,
)


class TestParseFrontmatterContract:
    """Контракт parse_frontmatter, не покрытый матрицей напрямую."""

    def test_none_when_no_block(self) -> None:
        assert parse_frontmatter("Просто Markdown без блока.") is None

    def test_dict_roundtrip(self) -> None:
        meta = parse_frontmatter("---\nid: kn-x\nsymptom: сбой\n---\ntext")
        assert meta == {"id": "kn-x", "symptom": "сбой"}

    def test_invalid_date_literal_raises_frontmatter_error(self) -> None:
        """PyYAML поднимает голый ValueError на невалидных дат-литералах
        (``2026-13-01`` — месяц 13). Кодек обязан замапить его в
        FrontmatterError, иначе в skills утекает сырой ValueError вместо
        InvalidSkillFormatError (замечание 1 ревью Ф3-финал; мутация M5).
        """
        with pytest.raises(FrontmatterError):
            parse_frontmatter("---\nexpires: 2026-13-01\n---\ntext")


class TestDumpFrontmatter:
    def test_canonical_params(self) -> None:
        """Канонический YAML-дамп: без сортировки ключей, unicode, block style."""
        text = dump_frontmatter({"symptom": "сбой", "id": "kn-x"})
        assert text == "symptom: сбой\nid: kn-x\n"

    def test_unicode_not_escaped(self) -> None:
        text = dump_frontmatter({"title": "Проверка"})
        assert "Проверка" in text
        assert "\\u" not in text

    def test_crlf_normalized_in_values(self) -> None:
        """Кейс 21: CR/CRLF в значениях нормализуются в LF, не эскейпятся.

        YAML-дамп многострочного значения эскейпит переносы (folded-стиль),
        но НЕ сырой ``\\\\r``: нормализованное значение возвращается к
        многострочности при повторном parse без «вечной» эскейп-контаминации.
        """
        text = dump_frontmatter({"symptom": "a\r\nb\rc"})
        assert "\\r" not in text
        meta = parse_frontmatter(f"---\n{text}---\n")
        assert meta is not None
        assert meta["symptom"] == "a\nb\nc"

    def test_crlf_normalized_in_text(self) -> None:
        assert "\r\n" not in dump_frontmatter({"a": "x"})

    def test_nested_structures(self) -> None:
        text = dump_frontmatter({"aliases": ["a", "b"], "nested": {"k": "v"}})
        meta = parse_frontmatter(f"---\n{text}---\n")
        assert meta == {"aliases": ["a", "b"], "nested": {"k": "v"}}


class TestRebuildCard:
    def test_roundtrip_with_mutator(self) -> None:
        card = "---\nid: aud-x\nstatus: planned\n---\n\n## Симптом\n\nтекст\n"
        rebuilt = rebuild_card(
            card,
            lambda m: {**m, "status": "completed", "updated_at": "2026-10-27"},
        )
        meta = parse_frontmatter(rebuilt)
        assert meta is not None
        assert meta["status"] == "completed"
        assert meta["updated_at"] == "2026-10-27"
        assert "## Симптом" in rebuilt
        assert "текст" in rebuilt

    def test_crlf_card_normalized(self) -> None:
        card = "---\r\nid: aud-x\r\n---\r\n\r\n## Симптом\r\n\r\nтекст\r\n"
        rebuilt = rebuild_card(card, lambda m: {**m, "status": "done"})
        # Мета и сборка карточки — LF; CRLF в теле сохраняется как есть
        # (normalize scope: мета + дамп, не body — known limitation Ф3).
        meta = parse_frontmatter(rebuilt)
        assert meta is not None
        assert meta["id"] == "aud-x"
        assert meta["status"] == "done"
        assert "## Симптом" in rebuilt
        assert "текст" in rebuilt

    def test_mutator_receives_parsed_meta(self) -> None:
        seen: list[dict] = []

        def mutator(meta: dict) -> dict:
            seen.append(dict(meta))
            return meta

        rebuild_card("---\nid: x\n---\nbody", mutator)
        assert seen == [{"id": "x"}]

    def test_no_frontmatter_raises(self) -> None:
        with pytest.raises(FrontmatterError, match="без frontmatter"):
            rebuild_card("Просто текст без блока.", lambda m: m)

    def test_body_preserved(self) -> None:
        # Ведущий пустой перевод строки тела поглощается закрывающим
        # маркером (regexp-семантика `\s*(?:\n|$)`), остальное тело — как есть.
        body = "## Секция\n\n- пункт с --- внутри\n\n```python\nx = '---'\n```\n"
        card = f"---\nid: x\n---\n{body}"
        rebuilt = rebuild_card(card, lambda m: m)
        assert rebuilt == f"---\nid: x\n---\n{body}"
