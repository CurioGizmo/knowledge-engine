"""Тесты R6: точный канал коротких терминов в audit-поиске (search_issues).

Воспроизведение (docs/analysis/2026-10-19-search-measurement-report.md, раздел 3):
запросы ui/бд/qa/ml/pr/ci/0% возвращают 0 строк — sanitize_fts_query строит
префиксный терм ui*, а trigram-индекс не индексирует 2-символьные токены.
В knowledge-поиске точный канал уже есть (_exact_acronym_fallback);
audit-поиску нужен эквивалент.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer


def _card(card_id: str, title: str, symptom: str, target_file: str | None = None) -> str:
    return (
        "---\n"
        f'id: "{card_id}"\n'
        f'title: "{title}"\n'
        "category: backend\n"
        "status: planned\n"
        "priority: p2\n"
        f"target_file: {target_file or 'null'}\n"
        "---\n\n"
        f"## Симптом\n{symptom}\n"
    )


@pytest.fixture
def audit_stand(tmp_path: Path) -> AuditStorage:
    """Стенд аудита с карточками, содержащими короткие термины."""
    a_dir = tmp_path / ".agents" / "audit" / "todo" / "backend"
    a_dir.mkdir(parents=True)
    (a_dir / "aud-backend-9001.md").write_text(
        _card(
            "aud-backend-9001",
            "UI db pipeline: короткие акронимы не находятся поиском",
            "Поиск по 2-символьным акронимам ui и db возвращает 0 результатов",
            "src/ui_pipeline.py",
        ),
        encoding="utf-8",
    )
    (a_dir / "aud-backend-9002.md").write_text(
        _card(
            "aud-backend-9002",
            "QA гейт пропускает PENDING статус",
            "qa gate принимает PENDING как успешный статус проверки",
        ),
        encoding="utf-8",
    )
    db = tmp_path / "audit_fts.db"
    storage = AuditStorage(db_path=db)
    AuditSynchronizer(storage=storage, audit_dir=tmp_path / ".agents" / "audit").sync_all()
    return storage


@pytest.mark.parametrize(
    "query",
    ["ui", "db", "ui db", "qa"],
)
def test_short_term_queries_find_cards(audit_stand: AuditStorage, query: str) -> None:
    """Короткие термины (2-символьные акронимы) находятся точным каналом."""
    rows = audit_stand.search_issues(query=query, status=None, limit=10)
    ids = [r["id"] for r in rows]
    if query == "qa":
        assert "aud-backend-9002" in ids, f"qa не найден: {ids}"
    else:
        assert "aud-backend-9001" in ids, f"{query!r} не найден: {ids}"


def test_short_term_no_false_positive(audit_stand: AuditStorage) -> None:
    """Точный канал не даёт мусорных substring-совпадений: 'cd' не находит ui/qa-карточки."""
    rows = audit_stand.search_issues(query="cd", status=None, limit=10)
    ids = [r["id"] for r in rows]
    assert "aud-backend-9001" not in ids
    assert "aud-backend-9002" not in ids


def test_parenthesized_short_terms(audit_stand: AuditStorage) -> None:
    """Смешанный запрос со скобками/регистром: 'UI db' матчит ui-карточку."""
    rows = audit_stand.search_issues(query="UI db", status=None, limit=10)
    assert "aud-backend-9001" in [r["id"] for r in rows]
