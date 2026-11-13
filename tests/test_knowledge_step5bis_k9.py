"""Шаг 5бис/K9 плана AUDIT-VERIFY-2026-10: SectionNode/SectionCollection.

Проверяет:
- round-trip H2 → мутация → H2 (уровень заголовка не теряется);
- H3 не меняется при мутации;
- Mapping-контракт (get/set/del/keys/items) для use-cases без правок;
- снепшот-паритет split_sections ↔ split_section_nodes (title/body);
- preamble сохраняется при round-trip.
"""

from __future__ import annotations

from pathlib import Path

from mcp_core.markdown import (
    SectionCollection,
    SectionNode,
    split_section_nodes,
    split_sections,
)
from mcp_knowledge.models import CardStatusUpdateRequest
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage

CARD_H2 = """---
id: audio-h2-mut
title: H2 карточка
category: audio
type: lesson
status: active
source: user_confirmed
confidence: 0.75
created_at: '2026-10-28'
---

## Симптом / Контекст

симптом с достаточно длинным текстом для проверки мутации

## Решение / Практика

решение с достаточно длинным текстом для проверки мутации
"""

CARD_H3 = """---
id: audio-h3-mut
title: H3 карточка
category: audio
type: lesson
status: active
source: user_confirmed
confidence: 0.75
created_at: '2026-10-28'
---

### Симптом / Контекст

симптом текст H3 карточки для проверки мутации уровня

### Решение / Практика

решение текст H3 карточки для проверки мутации уровня
"""


def _make_service(tmp_path: Path) -> KnowledgeService:
    k_dir = tmp_path / "k"
    storage = KnowledgeStorage(db_path=tmp_path / "c.db", knowledge_dir=k_dir)
    return KnowledgeService(storage=storage, knowledge_dir=k_dir)


def test_split_nodes_parity_with_split_sections() -> None:
    """Снепшот-паритет: title/body узлов == словарю split_sections."""
    body = "# Заголовок\n\n## Секция А\n\nтекст А\n\n### Секция Б\n\n```python\n## не секция\n```\n"
    flat = split_sections(
        body,
        header_match="regex",
        header_levels=(2, 3),
        fence_aware=True,
        preamble_on_whitespace=False,
        store_empty_sections=True,
    )
    nodes = split_section_nodes(
        body,
        header_match="regex",
        header_levels=(2, 3),
        fence_aware=True,
        preamble_on_whitespace=False,
        store_empty_sections=True,
    )
    coll = SectionCollection(nodes)
    assert dict(coll) == flat, (dict(coll), flat)
    # уровни корректны
    levels = {n.title: n.level for n in nodes if n.level}
    assert levels == {"Секция А": 2, "Секция Б": 3}, levels


def test_round_trip_h2_survives_mutation() -> None:
    """K9: H2-карточка после append_note сохраняет H2 (не H3)."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    service = _make_service(tmp)
    f = tmp / "k" / "audio" / "audio-h2-mut.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(CARD_H2, encoding="utf-8")

    service.update_status(
        CardStatusUpdateRequest(id="audio-h2-mut", action="append_note", note="K9 заметка")
    )
    out = f.read_text(encoding="utf-8")
    assert "## Симптом / Контекст" in out, "H2 → H3: K9 дефект вернулся"
    assert "## Решение / Практика" in out, "H2 → H3: K9 дефект вернулся"
    assert "K9 заметка" in out


def test_round_trip_h3_unchanged() -> None:
    """K9: H3-карточка после мутации остаётся H3."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    service = _make_service(tmp)
    f = tmp / "k" / "audio" / "audio-h3-mut.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(CARD_H3, encoding="utf-8")

    service.update_status(
        CardStatusUpdateRequest(id="audio-h3-mut", action="append_note", note="K9 H3 note")
    )
    out = f.read_text(encoding="utf-8")
    assert "### Симптом / Контекст" in out
    assert "### Решение / Практика" in out
    assert "K9 H3 note" in out


def test_section_collection_mapping_contract() -> None:
    """Mapping-контракт: get/set (upsert с сохранением уровня)/del/keys/items."""
    coll = SectionCollection.from_body("## A\n\nтекст A\n\n## B\n\nтекст B\n")
    assert coll["A"] == "текст A"
    # upsert существующей — уровень сохраняется
    coll["A"] = "новый текст A"
    assert coll["A"] == "новый текст A"
    assert [n.level for n in coll.nodes if n.title == "A"] == [2]
    # новая секция — default_level (3)
    coll["Новая"] = "текст новой"
    assert [n.level for n in coll.nodes if n.title == "Новая"] == [3]
    # del
    del coll["B"]
    assert "B" not in list(coll.keys())
    assert dict(coll) == {"A": "новый текст A", "Новая": "текст новой"}
    # len
    assert len(coll) == 2


def test_preamble_preserved_in_round_trip() -> None:
    """Preamble (level=0) пишется без заголовка и сохраняется."""
    coll = SectionCollection.from_body("# Заголовок карточки\n\n## A\n\nтекст A\n")
    assert "_preamble" in coll
    out = coll.serialize()
    assert "# Заголовок карточки" in out
    assert "## A" in out


def test_serialize_node_levels() -> None:
    nodes = [
        SectionNode(level=2, title="A", body="текст A"),
        SectionNode(level=3, title="B", body="текст B"),
    ]
    out = SectionCollection(nodes).serialize()
    assert "## A\nтекст A" in out
    assert "### B\nтекст B" in out
