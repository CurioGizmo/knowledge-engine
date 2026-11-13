"""Шаг 5бис/K12 плана AUDIT-VERIFY-2026-10: canonical-резолвер дубликатов
frontmatter.id.

Проверяет:
- два файла с одним frontmatter.id → один индекс-хит, warning записан,
  неканоничный в file_sync с conflict=1;
- mtime-равенство → детерминированный выбор по минимальному пути;
- удаление дубля → индекс = диск (оставшийся находится);
- index_single_card(id, B) при каноне A → no-op, canonical остаётся A;
- id.md неканоничен, canonical с другим именем → update_status меняет canonical;
- миграция v4→v5: guard пересоздаёт кэш при устаревшей схеме (conflict-колонка).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from mcp_knowledge.parser import parse_card_markdown
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.synchronizer import resolve_canonical_card_file


CARD_MD = """---
id: {card_id}
title: {title}
category: backend
type: lesson
status: active
source: auto_inferred
confidence: 0.75
created_at: '2026-10-28'
---

# {title}

## Симптом / Контекст

symptom text for k12 duplicate test

## Решение / Практика

solution body text
"""


def _write_card(path: Path, card_id: str, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CARD_MD.format(card_id=card_id, title=title), encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path):
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=tmp_path / "cache.db", knowledge_dir=k_dir, create_dirs=True)
    repo = CardRepository(k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)
    return storage, repo, service, server, k_dir


def test_resolver_max_mtime_then_min_path(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("x", encoding="utf-8")
    b.write_text("x", encoding="utf-8")
    import os

    os.utime(a, ns=(1000, 1000))
    os.utime(b, ns=(1000, 1000))  # равные mtime и size
    assert resolve_canonical_card_file([a, b]) == a  # минимальный путь
    assert resolve_canonical_card_file([b, a]) == a  # порядок не важен

    os.utime(b, ns=(2000, 2000))
    assert resolve_canonical_card_file([a, b]) == b  # больший mtime
    assert resolve_canonical_card_file([b, a]) == b


def test_duplicates_one_index_hit_and_conflict_flag(env, tmp_path: Path) -> None:
    storage, _, _, _, k_dir = env
    _write_card(k_dir / "backend" / "k12-dup.md", "backend-k12-dup", "Dup A")
    _write_card(k_dir / "backend" / "k12-dup-copy.md", "backend-k12-dup", "Dup B")
    # k12-dup-copy новее (последний write)
    storage.sync()

    sync_obj = storage.get_synchronizer()
    assert any("DUPLICATE_CARD_ID" in w for w in sync_obj.last_sync_warnings), (
        sync_obj.last_sync_warnings
    )

    with storage.get_connection() as conn:
        rows = conn.execute(
            "SELECT path, conflict FROM file_sync WHERE card_id = 'backend-k12-dup' ORDER BY path"
        ).fetchall()
        assert len(rows) == 2, rows
        canonical_row = [r for r in rows if r[1] == 0]
        conflict_row = [r for r in rows if r[1] == 1]
        assert len(canonical_row) == 1 and len(conflict_row) == 1, rows
        assert canonical_row[0][0].endswith("k12-dup-copy.md")  # новее — canonical

        hits = conn.execute(
            "SELECT COUNT(*) FROM knowledge_index WHERE id = 'backend-k12-dup'"
        ).fetchall()
        assert hits[0][0] == 1, "дубли frontmatter.id → ровно один индекс-хит"


def test_delete_duplicate_restores_index(env, tmp_path: Path) -> None:
    storage, _, _, _, k_dir = env
    f1 = k_dir / "backend" / "k12-del.md"
    f2 = k_dir / "backend" / "k12-del-copy.md"
    _write_card(f1, "backend-k12-del", "Del A")
    _write_card(f2, "backend-k12-del", "Del B")
    storage.sync()

    # удаляем canonical (f2 новее) — оставшийся f1 обязан переиндексироваться
    f2.unlink()
    storage.sync()
    results = storage.search(query="symptom text for k12 duplicate test")
    assert any(r.id == "backend-k12-del" for r in results), (
        "удаление дубля → search обязан находить оставшийся"
    )
    with storage.get_connection() as conn:
        rows = conn.execute(
            "SELECT path FROM file_sync WHERE card_id = 'backend-k12-del'"
        ).fetchall()
        assert len(rows) == 1, f"призраки в file_sync: {rows}"
        assert rows[0][0].endswith("k12-del.md")


def test_index_single_card_non_canonical_noop(env, tmp_path: Path) -> None:
    """Full sync выбрал A → index_single_card(id, B) → canonical остаётся A."""
    storage, _, _, _, k_dir = env
    f1 = k_dir / "backend" / "k12-iso.md"
    f2 = k_dir / "backend" / "k12-iso-copy.md"
    _write_card(f1, "backend-k12-iso", "Iso A")
    _write_card(f2, "backend-k12-iso", "Iso B")
    storage.sync()

    sync_obj = storage.get_synchronizer()
    # canonical — f2 (записан последним, новее). index_single_card(f1) — no-op
    sync_obj.index_single_card("backend-k12-iso", f1)
    assert any("INDEX_NON_CANONICAL" in w for w in sync_obj.last_sync_warnings), (
        sync_obj.last_sync_warnings
    )
    with storage.get_connection() as conn:
        row = conn.execute(
            "SELECT title FROM knowledge_index WHERE id = 'backend-k12-iso'"
        ).fetchone()
        assert row is not None and "Iso B" in str(row[0]), f"canonical затёрт неканоничным: {row}"


def test_update_status_targets_canonical_not_id_md(env, tmp_path: Path) -> None:
    """id.md неканоничен, canonical = <id>-copy.md → update_status меняет canonical."""
    storage, _, service, server, k_dir = env
    repo = service.repo  # repo с index_lookup-колбэком (conflict=0)
    f1 = k_dir / "backend" / "k12-upd.md"
    f2 = k_dir / "backend" / "k12-upd-copy.md"
    _write_card(f1, "backend-k12-upd", "Upd A")
    _write_card(f2, "backend-k12-upd", "Upd B")
    storage.sync()

    # lookup обязан вернуть canonical (f2, новее)
    found = repo.find_card_file("backend-k12-upd")
    assert found is not None and found.name == "k12-upd-copy.md", found

    # ищем сервисный путь через MCP: update_status меняет canonical
    import asyncio

    async def _run() -> str:
        res = await server.call_tool(
            "update_knowledge_status",
            {"id": "backend-k12-upd", "action": "append_note", "note": "K12 canonical note"},
        )
        return res.content[0].text if res.content else ""

    # карточка auto_inferred: append_note пройдёт gate (note чистый)
    asyncio.run(_run())

    fm1, _ = parse_card_markdown(f1.read_text(encoding="utf-8"))
    fm2, sec2 = parse_card_markdown(f2.read_text(encoding="utf-8"))
    assert "K12 canonical note" in "\n".join(sec2.values()), (
        "update_status обязан мутировать canonical-файл"
    )
    assert "K12 canonical note" not in "\n".join(
        parse_card_markdown(f1.read_text(encoding="utf-8"))[1].values()
    ), "неканоничный id.md не должен мутироваться"


def test_dirty_noncanonical_keeps_canonical_fts(env, tmp_path: Path) -> None:
    """K12-блокер: dirty-неканоничный B + чистый каноничный A → FTS остаётся."""
    storage, _, _, _, k_dir = env
    a = k_dir / "backend" / "k12-dirty.md"
    b = k_dir / "backend" / "k12-dirty-copy.md"
    _write_card(a, "backend-k12-dirty", "Dirty A")
    _write_card(b, "backend-k12-dirty", "Dirty B")
    storage.sync()

    # делаем A каноничным (новее) и «чистым» для file_sync:
    import os

    future_ns = 10**19  # заведомо новее любых файлов песочницы
    os.utime(a, ns=(future_ns, future_ns))
    storage.sync()  # A стал canonical, его mtime зафиксирован в file_sync

    with storage.get_connection() as conn:
        holder = conn.execute(
            "SELECT path, conflict FROM file_sync WHERE path LIKE '%k12-dirty%' ORDER BY path"
        ).fetchall()
        # A — держатель (conflict=0), B — conflict=1
        assert any(r[0].endswith("k12-dirty.md") and r[1] == 0 for r in holder), holder
        assert any(r[0].endswith("k12-dirty-copy.md") and r[1] == 1 for r in holder), holder

    # B становится dirty (правим контент) и одновременно «новее» A:
    content = b.read_text(encoding="utf-8").replace("Dirty B", "Dirty B edited")
    b.write_text(content, encoding="utf-8")

    storage.sync()

    with storage.get_connection() as conn:
        # Каноничная FTS-запись обязана сохраниться (dirty-дубль её не стирает)
        row = conn.execute(
            "SELECT title FROM knowledge_index WHERE id = 'backend-k12-dirty'"
        ).fetchone()
        assert row is not None, "K12: dirty-неканоничный удалил FTS-запись каноничного"
        # Инвариант: ровно одна запись в FTS на id.
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_index WHERE id = 'backend-k12-dirty'"
        ).fetchone()[0]
        assert count == 1, f"ожидалась ровно одна FTS-запись, получено {count}"


def test_schema_v5_guard_rebuilds_old_cache(tmp_path: Path) -> None:
    """Миграция v4→v5: старая БД (без conflict-колонки) пересоздаётся guard'ом."""
    from mcp_core.fts import SCHEMA_VERSION
    from mcp_knowledge.storage import (
        FILE_SYNC_CREATE_SQL,
        KNOWLEDGE_STORAGE_SCHEMA_SPEC,
    )
    from mcp_core.schema_guard import ensure_storage_schema

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        # эмуляция старой схемы: file_sync без conflict
        conn.execute(
            """
            CREATE TABLE file_sync (
                path TEXT PRIMARY KEY,
                mtime REAL,
                size INTEGER,
                card_id TEXT
            );
            """
        )
        conn.execute("PRAGMA user_version = 4;")
        conn.commit()

    with sqlite3.connect(db) as conn:
        ensure_storage_schema(conn, KNOWLEDGE_STORAGE_SCHEMA_SPEC)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(file_sync)").fetchall()]
        assert "conflict" in cols, cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
