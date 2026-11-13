"""Тесты точного резолва ID карточки в audit-синхронизаторе (задача A1).

find_card_file не должен путать issue-1 и issue-10-other.md:
префиксный fallback по stem совпадает по префиксу, а не по точному ID.
"""

from __future__ import annotations

from pathlib import Path

from mcp_audit.models import AuditStatus
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer


def _make_sync(tmp_path: Path) -> AuditSynchronizer:
    audit_dir = tmp_path / ".agents" / "audit"
    db_path = tmp_path / ".agents" / ".cache" / "audit_fts.db"
    return AuditSynchronizer(AuditStorage(db_path), audit_dir=audit_dir)


CARD_ISSUE_1 = """---
id: issue-1
title: Exact card
category: backend
status: planned
---

## Symptom

Symptom of issue-1.
"""

CARD_ISSUE_10 = """---
id: issue-10
title: Prefixed sibling
category: backend
status: planned
---

## Symptom

Symptom of issue-10.
"""


def test_find_card_file_exact_id_no_prefix_confusion(tmp_path: Path) -> None:
    """find_card_file('issue-1') находит issue-1.md, а не issue-10-other.md."""
    sync = _make_sync(tmp_path)

    cat_dir = sync.todo_dir / "backend"
    cat_dir.mkdir(parents=True, exist_ok=True)
    exact = cat_dir / "issue-1.md"
    sibling = cat_dir / "issue-10-other.md"
    exact.write_text(CARD_ISSUE_1, encoding="utf-8")
    sibling.write_text(CARD_ISSUE_10, encoding="utf-8")

    found = sync.find_card_file("issue-1")
    assert found is not None
    assert found.name == "issue-1.md"

    found10 = sync.find_card_file("issue-10")
    assert found10 is not None
    assert found10.name == "issue-10-other.md"


def test_update_issue_status_does_not_touch_prefixed_sibling(tmp_path: Path) -> None:
    """update_issue_status('issue-1') не меняет файл issue-10-other.md."""
    sync = _make_sync(tmp_path)

    cat_dir = sync.todo_dir / "backend"
    cat_dir.mkdir(parents=True, exist_ok=True)
    exact = cat_dir / "issue-1.md"
    sibling = cat_dir / "issue-10-other.md"
    exact.write_text(CARD_ISSUE_1, encoding="utf-8")
    sibling.write_text(CARD_ISSUE_10, encoding="utf-8")
    sibling_before = sibling.read_text(encoding="utf-8")

    issue, new_path = sync.update_issue_status("issue-1", AuditStatus.COMPLETED)

    assert issue.id == "issue-1"
    assert new_path.name == "issue-1.md"
    # Карточка issue-1 переехала в done/
    assert "done" in str(new_path)

    # issue-10-other.md не тронута: содержимое и расположение неизменны
    assert sibling.read_text(encoding="utf-8") == sibling_before
    assert sibling.parent == cat_dir
