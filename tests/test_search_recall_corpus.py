"""Ф0 (core-dedup v4): per-package recall-раннер по корпусу search-corpus.json.

Семантика (docs/plans/2026-10-26-core-dedup-v4.md, Ф0 «Recall-раннер»):
- Корпус knowledge-центричен (76 real: knowledge 68, audit 18, skills 0 —
  skills не грейдируется, его защищают существующие поисковые тесты).
- Запросы грейдируются ТОЛЬКО по своему expected-набору пакета; пустой
  expected для пакета = пропуск (не провал).
- Hard-rollback только по real-запросам (synthetic=false); 14 synthetic —
  контракты будущего, non-blocking (по ним только отчёт-заглушка ниже).
- Baseline: fixtures/search-recall-baseline.json — сгенерирован на Ф0;
  деградация относительно baseline = откат-сигнал Ф5.

Раннер запускается на tmp-хранилищах, засеянных РЕАЛЬНЫМИ карточками .agents/
(та же база, что репо-инвариант: 77 файлов, 5 планов expected-fail) — это
максимально честная замена «corpus-атрибуты к карточкам».
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
AGENTS = FIXTURES / "corpus" if (FIXTURES / "corpus").is_dir() else REPO_ROOT / ".agents"

CORPUS = json.loads((FIXTURES / "search-corpus.json").read_text(encoding="utf-8"))


def _load_baseline() -> dict:
    return json.loads((FIXTURES / "search-recall-baseline.json").read_text(encoding="utf-8"))


# --- сидинг хранилищ реальными карточками ---
@pytest.fixture(scope="module")
def knowledge_storage(tmp_path_factory: pytest.TempPathFactory):
    from mcp_knowledge.storage import KnowledgeStorage
    from mcp_knowledge.synchronizer import KnowledgeSynchronizer

    db = tmp_path_factory.mktemp("kn") / "knowledge.db"
    k_dir = tmp_path_factory.mktemp("kn") / "knowledge"
    k_dir.mkdir(exist_ok=True)
    # сидируем реальными knowledge-карточками
    for card in (AGENTS / "knowledge").rglob("*.md"):
        target = k_dir / card.parent.name / card.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(card.read_text(encoding="utf-8"), encoding="utf-8")
    storage = KnowledgeStorage(db, knowledge_dir=k_dir)
    from mcp_knowledge.repository import CardRepository

    repo = CardRepository(k_dir)
    KnowledgeSynchronizer(storage, repo).sync()
    return storage


@pytest.fixture(scope="module")
def audit_storage(tmp_path_factory: pytest.TempPathFactory):
    from mcp_audit.storage import AuditStorage
    from mcp_audit.synchronizer import AuditSynchronizer

    db = tmp_path_factory.mktemp("aud") / "audit.db"
    a_dir = tmp_path_factory.mktemp("aud") / "agents" / "audit"
    # сидируем реальными audit-карточками с сохранением todo/done-структуры
    for card in (AGENTS / "audit").rglob("*.md"):
        rel = card.relative_to(AGENTS / "audit")
        target = a_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(card.read_text(encoding="utf-8"), encoding="utf-8")
    storage = AuditStorage(db_path=db)
    AuditSynchronizer(storage=storage, audit_dir=a_dir).sync_all()
    return storage


def _recall_metrics(hits: list[str], expected: list[str]) -> dict:
    if not expected:
        return {"graded": False}
    hit_set = set(hits)
    exp_set = set(expected)
    return {
        "graded": True,
        "hits": hits,
        "found": sorted(exp_set & hit_set),
        "missed": sorted(exp_set - hit_set),
        "recall": len(exp_set & hit_set) / len(exp_set),
    }


def _run_query(storage, package: str, query: str, limit: int = 10) -> list[str]:
    if package == "knowledge":
        return [c.id for c in storage.search(query, limit=limit)]
    if package == "audit":
        return [r["id"] for r in storage.search_issues(query=query, status=None, limit=limit)]
    raise ValueError(f"неизвестный пакет: {package}")


@pytest.mark.parametrize(
    "package,idx",
    [
        (p, i)
        for p in ("knowledge", "audit")
        for i, c in enumerate(CORPUS)
        if not c.get("synthetic") and c["expected"][p]
    ],
)
def test_recall_not_worse_than_baseline(
    package: str, idx: int, knowledge_storage, audit_storage
) -> None:
    """Real-запрос: hit-set по expected не хуже baseline (hard-гейт Ф5)."""
    case = CORPUS[idx]
    storage = knowledge_storage if package == "knowledge" else audit_storage
    hits = _run_query(storage, package, case["query"])
    metrics = _recall_metrics(hits, case["expected"][package])

    baseline = _load_baseline()[f"{package}:{idx}"]
    assert metrics["recall"] >= baseline["recall"], (
        f"{package} '{case['query']}': recall деградировал "
        f"({metrics['recall']:.2f} < {baseline['recall']:.2f}); "
        f"missed: {metrics['missed']}"
    )


def test_recall_baseline_covers_all_real_graded_cases() -> None:
    """Страж baseline: каждая real-грейдируемая ячейка корпуса имеет запись."""
    baseline = _load_baseline()
    for idx, case in enumerate(CORPUS):
        if case.get("synthetic"):
            continue
        for package in ("knowledge", "audit"):
            if case["expected"][package]:
                assert f"{package}:{idx}" in baseline, (
                    f"нет baseline для {package}:{idx} ({case['query']!r})"
                )


def test_recall_synthetic_cases_are_non_blocking() -> None:
    """14 synthetic-кейсов не участвуют в hard-гейте — только перечислены."""
    synthetic = [c["query"] for c in CORPUS if c.get("synthetic")]
    assert len(synthetic) == 14, "состав корпуса изменился — пересобрать baseline"
