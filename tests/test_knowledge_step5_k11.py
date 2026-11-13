"""Шаг 5 плана AUDIT-VERIFY-2026-10: K11 — легаси-статус/тип в FTS-индексе
не валит весь search (деградация с warning).

Сценарий (репорт реверификатора rv_k11_k12): легаси-строка в индексе
видна при include_archived=True (обычный WHERE-фильтр её исключает).
До фикса: KnowledgeStatus(c_status) падал ValueError в _row_to_snippet
и валил выдачу целиком. После — деградация + warning.
"""

from __future__ import annotations

from pathlib import Path

from mcp_knowledge.storage import KnowledgeStorage


def _make_storage(tmp_path: Path) -> KnowledgeStorage:
    return KnowledgeStorage(
        db_path=tmp_path / "cache.db",
        knowledge_dir=tmp_path / "knowledge",
        create_dirs=True,
    )


VALID_CARD_MD = """---
id: {card_id}
title: Card {card_id}
category: backend
type: lesson
status: active
source: auto_inferred
confidence: 0.75
created_at: '2026-10-28'
---

# Card

## Симптом / Контекст

legacy symptom text for K11
"""


def _insert_legacy_row(storage: KnowledgeStorage, card_id: str, status: str, type_: str) -> None:
    """Инъекция легаси-значения в уже проиндексированную карточку.

    Сценарий K11 (репорт rv_k11_k12): валидная карточка на диске,
    file_sync актуален (sync не dirty), но status/type в кэше — легаси-мусор.
    Виден при include_archived=True (WHERE-фильтр обычного поиска его
    исключает). Соединение требует явного commit (autocommit выключен).
    """
    k_dir = storage.knowledge_dir  # type: ignore[attr-defined]
    card_dir = k_dir / "backend"
    card_dir.mkdir(parents=True, exist_ok=True)
    card_file = card_dir / f"{card_id}.md"
    card_file.write_text(VALID_CARD_MD.format(card_id=card_id), encoding="utf-8")

    storage.sync()  # формирует валидный file_sync
    with storage.get_connection() as conn:
        if status != "active":
            conn.execute("UPDATE knowledge_index SET status = ? WHERE id = ?", (status, card_id))
        if type_ != "lesson":
            conn.execute("UPDATE knowledge_index SET type = ? WHERE id = ?", (type_, card_id))
        conn.commit()


def test_legacy_status_in_index_does_not_break_search(tmp_path: Path) -> None:
    storage = _make_storage(tmp_path)
    _insert_legacy_row(storage, "backend-k11-legacy-card", "legacy_broken_status", "lesson")

    results = storage.search(query="legacy", include_archived=True)
    assert isinstance(results, list), "search обязан возвращать список, не падать"
    match = [r for r in results if r.id == "backend-k11-legacy-card"]
    assert match, f"карточка с легаси-статусом обязана попасть в выдачу: {results}"
    assert match[0].warning and "CARD_STATUS_UNKNOWN" in match[0].warning, (
        f"warning обязан сигнализировать о легаси-статусе: {match[0]}"
    )


def test_legacy_type_in_index_does_not_break_search(tmp_path: Path) -> None:
    storage = _make_storage(tmp_path)
    _insert_legacy_row(storage, "backend-k11-legacy-type", "active", "legacy_broken_type")

    results = storage.search(query="legacy", include_archived=True)
    assert isinstance(results, list), "search обязан возвращать список, не падать"
    match = [r for r in results if r.id == "backend-k11-legacy-type"]
    assert match, f"карточка с легаси-type обязана попасть в выдачу: {results}"
    assert match[0].warning and "CARD_TYPE_UNKNOWN" in match[0].warning, (
        f"warning обязан сигнализировать о легаси-type: {match[0]}"
    )
