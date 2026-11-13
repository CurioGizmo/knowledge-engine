"""Property-based тесты архитектурного разворота AUDIT-ARCH-2026-10 (шаг 5).

Тестируются СВОЙСТВА инвариантов, а не сценарии конкретных фиксов:

P1. select_canonical: порядок обхода файлов не меняет канон
    (перестановки входа → один результат).
P2. Мутация любого дубля (canonical/неканоничный, dirty/clean) не
    уничтожает FTS-запись card_id — ровно одна запись, заголовок канона.
P3. Два конкурентных create_unique одного task_id → ровно один успех.
P4. get_unique через индекс и по файловой системе имеет одинаковую
    семантику неоднозначности.
P5. parse → mutate → serialize сохраняет структуру Markdown
    (уровни заголовков H2/H3, preamble).
"""

from __future__ import annotations

import itertools
import os
import threading
from pathlib import Path

import pytest

from mcp_audit.models import PlanArtifact, PlanStatus, PlanStep
from mcp_audit.parser import serialize_plan_markdown
from mcp_audit.repository import AmbiguousPlanError, PlanRepository
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer
from mcp_core.markdown import SectionCollection
from mcp_knowledge.parser import parse_card_markdown, serialize_card_from_parts
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.resolver import CanonicalCardResolver
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


# ----------------------------------------------------------------------
# Общие фикстуры
# ----------------------------------------------------------------------
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

symptom text for property tests

## Решение / Практика

solution body text
"""


def _write_card(path: Path, card_id: str, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CARD_MD.format(card_id=card_id, title=title), encoding="utf-8")


def _fts_row(storage: KnowledgeStorage, card_id: str) -> tuple[int, str | None]:
    with storage.get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_index WHERE id = ?", (card_id,)
        ).fetchone()[0]
        title = None
        if count:
            row = conn.execute(
                "SELECT title FROM knowledge_index WHERE id = ?", (card_id,)
            ).fetchone()
            title = str(row[0]) if row else None
    return count, title


@pytest.fixture
def k_env(tmp_path: Path) -> tuple[KnowledgeStorage, CardRepository, KnowledgeService, Path]:
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=tmp_path / "cache.db", knowledge_dir=k_dir, create_dirs=True)
    repo = CardRepository(k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    return storage, repo, service, k_dir


@pytest.fixture
def a_env(tmp_path: Path) -> tuple[Path, AuditStorage, AuditSynchronizer]:
    audit_dir = tmp_path / ".agents" / "audit"
    db_path = tmp_path / ".agents" / ".cache" / "audit_fts.db"
    storage = AuditStorage(db_path)
    sync = AuditSynchronizer(storage=storage, audit_dir=audit_dir)
    return audit_dir, storage, sync


def _make_plan(plan_id: str, task_id: str, title: str, category: str = "general") -> PlanArtifact:
    return PlanArtifact(
        id=plan_id,
        task_id=task_id,
        title=title,
        category=category,
        status=PlanStatus.DISCUSSED,
        steps=[PlanStep(index=1, title="Step 1", completed=False)],
        content="Goal",
        created_at="2026-10-28",
        updated_at="2026-10-28",
    )


# ----------------------------------------------------------------------
# P1. select_canonical: перестановки входа → один результат
# ----------------------------------------------------------------------
@pytest.mark.parametrize("permutation", list(itertools.permutations(range(4))))
def test_select_canonical_permutations(permutation: tuple[int, ...], tmp_path: Path) -> None:
    """K12/rev.3.6: max(mtime_ns, size), tie-break min(path) — детерминировано.

    mtime одинаков у всех → правило сводится к (size, min(path)); все 24
    перестановки входа обязаны дать один результат.
    """
    files = [tmp_path / f"p1-{i}.md" for i in range(4)]
    for i, f in enumerate(files):
        f.write_text(f"x{i}" * (i + 1), encoding="utf-8")
        os.utime(f, ns=(10**9, 10**9))
    shuffled = [files[i] for i in permutation]
    expected = CanonicalCardResolver.select_canonical(list(files))
    assert expected is not None
    result = CanonicalCardResolver.select_canonical(shuffled)
    assert result == expected, (
        f"порядок обхода изменил канон: {permutation} → {result} вместо {expected}"
    )


def test_select_canonical_equal_stats_min_path_all_permutations(tmp_path: Path) -> None:
    """Полное mtime/size-равенство → минимальный путь при любом порядке."""
    files = [tmp_path / n for n in ("d.md", "c.md", "b.md", "a.md")]
    for f in files:
        f.write_text("same", encoding="utf-8")
        os.utime(f, ns=(10**9, 10**9))
    expected = tmp_path / "a.md"
    for permutation in itertools.permutations(files):
        assert CanonicalCardResolver.select_canonical(list(permutation)) == expected


# ----------------------------------------------------------------------
# P2. Мутация любого дубля не уничтожает FTS-запись
# ----------------------------------------------------------------------
@pytest.mark.parametrize("mutate_canonical", [True, False])
@pytest.mark.parametrize("dirty", [True, False])
def test_mutation_of_any_duplicate_keeps_fts(
    k_env: tuple[KnowledgeStorage, CardRepository, KnowledgeService, Path],
    mutate_canonical: bool,
    dirty: bool,
) -> None:
    """Изменение любого дубля (канон/неканон, dirty/clean) → FTS жива и одна."""
    storage, _, _, k_dir = k_env
    cid = f"p2-{'can' if mutate_canonical else 'non'}-{'dirty' if dirty else 'clean'}"
    a = k_dir / "backend" / f"{cid}.md"
    b = k_dir / "backend" / f"{cid}-copy.md"
    _write_card(a, cid, "P2 A")
    _write_card(b, cid, "P2 B")
    storage.sync()

    # a делаем каноничным: записываем последним (mtime новее)
    a.write_text(CARD_MD.format(card_id=cid, title="P2 A"), encoding="utf-8")
    storage.sync()

    target = a if mutate_canonical else b
    if dirty:
        target.write_text(
            CARD_MD.format(card_id=cid, title="P2 A edited").replace(
                "solution body text", "solution body edited"
            ),
            encoding="utf-8",
        )

    storage.sync()

    count, title = _fts_row(storage, cid)
    assert count == 1, f"FTS-запись потеряна при мутации дубля: count={count}"
    assert title is not None and "P2 A" in title, (
        f"FTS-запись замещена неканоничным дублем: title={title}"
    )


# ----------------------------------------------------------------------
# P3. Конкурентные create_unique → ровно один успех
# ----------------------------------------------------------------------
def test_two_concurrent_create_unique_exactly_one_succeeds(
    a_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    _, storage, sync = a_env
    repo = PlanRepository(sync)
    results: list[Path] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker(n: int) -> None:
        plan = _make_plan(f"p3-plan-{n}", "p3-task", f"P3 concurrent {n}")
        try:
            barrier.wait(timeout=5)
            results.append(repo.create_unique(plan))
        except Exception as e:  # noqa: BLE001 — фиксируем любой отказ
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 1, f"ожидался ровно один успех, создано: {results}"
    assert len(errors) == 1, f"ожидался ровно один reject, получено: {errors}"
    assert "PLAN_EXISTS" in str(errors[0]), errors[0]

    # на диске и в индексе — ровно один план этого task_id
    assert PlanRepository(sync).get_unique("p3-task") is not None
    del storage


# ----------------------------------------------------------------------
# P4. indexed- и filesystem-lookup: одна семантика неоднозначности
# ----------------------------------------------------------------------
def test_get_unique_ambiguity_same_for_indexed_and_disk(
    a_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Дубли task_id в разных категориях.

    ДО индексации дубля (filesystem-only) и ПОСЛЕ (index+disk) —
    get_unique обязан вести себя одинаково: без category —
    AmbiguousPlanError; с category — детерминированное разрешение.
    """
    _, storage, sync = a_env
    repo = PlanRepository(sync)
    p1 = _make_plan("p4-a", "p4-task", "P4 core", category="general")
    repo.create_unique(p1)

    dup = _make_plan("p4-b", "p4-task", "P4 backend dup", category="backend")
    target = sync._plan_target_dir(dup.status, dup.category)  # noqa: SLF001
    target.mkdir(parents=True, exist_ok=True)
    f = target / "plan-backend-p4-task-dup.md"
    f.write_text(serialize_plan_markdown(dup), encoding="utf-8")

    # файловая система ещё не знает о дубле через индекс: два файла
    # на диске → неоднозначность уже обязана быть видна (инвариант:
    # быстрый индексный путь не шорткатит решение)
    with pytest.raises(AmbiguousPlanError):
        PlanRepository(sync).get_unique("p4-task")

    # после sync семантика та же (индекс просто пополняет набор)
    sync.sync_artifacts()
    with pytest.raises(AmbiguousPlanError):
        PlanRepository(sync).get_unique("p4-task")

    # с category — детерминированное разрешение, без raise
    resolved = PlanRepository(sync).get_unique("p4-task", category="backend")
    assert resolved is not None and resolved.name == f.name


# ----------------------------------------------------------------------
# P5. parse → mutate → serialize сохраняет структуру Markdown
# ----------------------------------------------------------------------
def test_parse_mutate_serialize_preserves_structure() -> None:
    """H2-секции и H3-подсекции сохраняют уровни после мутации."""
    raw = CARD_MD.format(card_id="p5", title="P5")
    fm, sections = parse_card_markdown(raw)
    assert isinstance(sections, SectionCollection)

    sections["Решение / Практика"] = (
        sections["Решение / Практика"] + "\n\n### Подробности\n\nedited solution"
    )
    out = serialize_card_from_parts(fm, sections)

    fm2, sections2 = parse_card_markdown(out)
    assert fm2.id == fm.id
    assert fm2.title == fm.title

    body = out.split("---", 2)[2]
    h2_titles = [
        line[3:]
        for line in body.splitlines()
        if line.startswith("## ") and not line.startswith("### ")
    ]
    assert "Симптом / Контекст" in h2_titles, body
    assert "Решение / Практика" in h2_titles, body
    assert "### Подробности" in body


def test_round_trip_idempotent_with_h3_subsections() -> None:
    """parse→serialize без мутаций байт-стабилен (микс H2/H3)."""
    raw = CARD_MD.format(card_id="p5b", title="P5B") + "\n### Нюансы\n\nnuance text\n"
    fm, sections = parse_card_markdown(raw)
    out1 = serialize_card_from_parts(fm, sections)
    fm2, sections2 = parse_card_markdown(out1)
    out2 = serialize_card_from_parts(fm2, sections2)
    assert out1 == out2
    assert "## Симптом / Контекст" in out1
    assert "### Нюансы" in out1


# ----------------------------------------------------------------------
# Ревью разворота (aud-backend-db07 / 17b8): инварианты фиксов
# ----------------------------------------------------------------------
def test_index_lookup_rejects_id_mismatch(k_env) -> None:
    """db07: индексный хит с чужим frontmatter.id не выдаётся как карточка.

    Файл по индексному пути внешне перезаписан (id X→Y) — lookup обязан
    деградировать до дискового резолва, а не вернуть чужую карточку.
    """
    from mcp_knowledge.resolver import CanonicalCardResolver

    storage, _, _, k_dir = k_env
    cid = "db07-card"
    _write_card(k_dir / "backend" / f"{cid}.md", cid, "DB07 original")
    storage.sync()

    resolver = CanonicalCardResolver(
        k_dir, index_lookup=lambda card_id: k_dir / "backend" / f"{card_id}.md"
    )
    assert resolver.lookup(cid) is not None

    # перезаписываем файл тем же путём, но с ДРУГИМ frontmatter.id
    _write_card(k_dir / "backend" / f"{cid}.md", "other-card", "DB07 hijacked")
    result = resolver.lookup(cid)
    # невалидный хит → disk-fallback; совпадений с cid больше нет → None
    assert result is None, f"lookup вернул путь с чужим frontmatter.id: {result}"


def test_merge_target_mutated_under_coordinator_lock(k_env) -> None:
    """17b8: refute+merge мутирует цель через coordinator (с card_lock).

    Инвариант: merge-нота появляется в целевой карточке; опровергнутая
    гипотеза удаляется.
    """
    storage, _, service, k_dir = k_env
    from mcp_knowledge.models import (
        HypothesesResolveRequest,
        HypothesisRecordRequest,
        HypothesisResolution,
    )

    # гипотеза — через сервисный контракт (propose_hypothesis)
    prop_res = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="H17B hypothesis approach",
            category="backend",
            symptom="симптом для h17b гипотезы",
            hypothesis="гипотетический подход h17b",
            test_plan="проверить поведение",
            task_id="t-h17b",
            slug="h17b-hypo",
        )
    )
    hypo_id = prop_res["id"]
    # цель слияния
    _write_card(k_dir / "backend" / "h17b-target.md", "h17b-target", "H17B target")
    storage.sync()

    resp = service.resolve_hypotheses(
        HypothesesResolveRequest(
            task_id="t-h17b",
            resolutions=[
                HypothesisResolution(
                    id=hypo_id,
                    action="refute",
                    refutation_reason="подход не подтвердился на практике",
                    merge_into="h17b-target",
                )
            ],
        )
    )
    assert resp["status"] == "resolved"
    assert resp["details"][0]["merged_into"] == "h17b-target"

    target_text = (k_dir / "backend" / "h17b-target.md").read_text(encoding="utf-8")
    assert f"Опровергнутый подход ({hypo_id})" in target_text, (
        "merge-нота не записана в целевую карточку"
    )
    assert service.get([hypo_id]).cards == [], "опровергнутая гипотеза не удалена"
