"""D7 (review-fixes-2026-10): category-слой search_faceted обязан учитывать task_id.

Дефект (verdict_7c, вопрос 1): query-слой search_faceted идёт через search()
и применяет task_id-ветку видимости (storage.py ~704-709: active OR
(hypothesis|draft AND task_id = ?)), а category-слой строит status_cond
(storage.py ~1163-1171) без task_id — агент не видит свои draft-карточки
в своей задаче даже при явном task_id.

Ожидание: search_faceted(categories=[...], task_id=T) находит draft-карточку
задачи T; без task_id и с чужим task_id draft остаётся скрыт.
"""

from pathlib import Path

import pytest

from mcp_knowledge.models import (
    CardRecordRequest,
    KnowledgeCategory,
    KnowledgeType,
)
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


@pytest.fixture
def env(tmp_path: Path):
    k_dir = tmp_path / "knowledge"
    c_db = tmp_path / "cache.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    return storage, service


TASK_ID = "task-d7-category"


def _record_draft(service: KnowledgeService) -> str:
    """Автономная (без человека) запись → draft-карантин с task_id."""
    req = CardRecordRequest(
        title="Ловушка category-слоя фасетного поиска",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        symptom="category-слой search_faceted не применяет task_id-ветку видимости draft",
        root_cause="status_cond в search_faceted собирается без ветки task_id",
        solution="Добавить в status_cond ветку (status IN (hypothesis, draft) AND task_id = ?)",
        rationale="Однородность видимости draft между query-слоем и category-слоем",
        slug="faceted-task-id-blindness",
        task_id=TASK_ID,
    )
    resp = service.record(req)
    assert resp["status"] == "created"
    assert resp["card_status"] == "draft"
    return resp["path"]


def test_category_layer_sees_own_draft_by_task_id(env):
    """Draft-карточка с task_id=T видна в category-слое при поиске с тем же task_id."""
    _, service = env
    _record_draft(service)
    draft_id = "backend-faceted-task-id-blindness"

    resp = service.search_faceted(categories=["backend"], task_id=TASK_ID)
    category_ids = [s.id for s in resp.category_results.get("backend", [])]
    assert draft_id in category_ids, (
        f"category-слой слеп к task_id: ожидается {draft_id}, получено {category_ids}"
    )


def test_category_layer_hides_draft_without_task_id(env):
    """Без task_id (и include_drafts=False) draft-карточка в category-слое скрыта."""
    _, service = env
    _record_draft(service)
    draft_id = "backend-faceted-task-id-blindness"

    resp = service.search_faceted(categories=["backend"])
    category_ids = [s.id for s in resp.category_results.get("backend", [])]
    assert draft_id not in category_ids


def test_category_layer_hides_draft_from_foreign_task_id(env):
    """Чужой task_id не открывает draft-карточку другой задачи."""
    _, service = env
    _record_draft(service)
    draft_id = "backend-faceted-task-id-blindness"

    resp = service.search_faceted(categories=["backend"], task_id="task-other")
    category_ids = [s.id for s in resp.category_results.get("backend", [])]
    assert draft_id not in category_ids
