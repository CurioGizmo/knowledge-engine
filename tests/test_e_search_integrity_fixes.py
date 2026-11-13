"""Regression tests for search defects confirmed by measurements (plan item E).

Defects covered (see docs/plans/2026-10-18-search-and-data-integrity-followup.md,
section E, and the measurement report):
- P1-1: fuzzy candidate channel on knowledge (trigram MATCH over unicode61
  index) is dead code; typo recall must come from a measured, working channel.
- P1-2: unordered LIMIT 200 fallback cuts the tail — a relevant card inserted
  beyond the first 200 rows must still be found.
- P1-3: single FUZZY_DOC_CHAR_CAP=600 blob cap zeroes jaccard for words in the
  tail of long documents — per-field/chunk budgets keep tail words visible.
- P2: short acronyms (db/ui/pr/ci/0%) on skills (trigram index) need a
  substring/prefix SQL channel in addition to the token-whitelist channel.

All tests use temporary DBs (tmp_path); no user data is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_core.fuzzy import (  # noqa: E402
    FUZZY_THRESHOLD,
    jaccard_word_best,
)
from mcp_knowledge.models import (  # noqa: E402
    CardRecordRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeType,
)
from mcp_knowledge.parser import serialize_card_markdown  # noqa: E402
from mcp_knowledge.storage import KnowledgeStorage  # noqa: E402
from mcp_skills.models import SkillCard  # noqa: E402
from mcp_skills.storage import SkillsStorage  # noqa: E402


def _card(
    cid: str,
    *,
    title: str,
    symptom: str = "Симптом общего характера производительности системы при конкурентном доступе",
    root_cause: str = "Коренная причина ошибки описана в контексте карточки достаточно подробно",
    solution: str = "Решение описано в карточке с достаточной длиной текста и подробным объяснением",
    rationale: str = "Обоснование решения с подробным объяснением архитектурных причин выбора",
) -> CardRecordRequest:
    return CardRecordRequest(
        title=title,
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom=symptom,
        root_cause=root_cause,
        solution=solution,
        rationale=rationale,
    )


def _filler(prefix: str, n: int) -> str:
    return " ".join(f"{prefix}{i:03d}" for i in range(n))


# ---------------------------------------------------------------- P1-1 + P1-2
# Typo at start/middle of word; relevant card inserted BEYOND the first 200 rows.


def test_fuzzy_typo_middle_of_word_found(tmp_path: Path) -> None:
    """Typo in the middle of the word ('sqllite' vs indexed 'sqlite') is found."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(tmp_path / "k.db", knowledge_dir=k_dir)

    req = _card("sqlite-wal", title="Настройка SQLite WAL режима")
    (k_dir / "sqlite-wal.md").write_text(
        serialize_card_markdown("sqlite-wal", req), encoding="utf-8"
    )
    storage.sync()

    res = storage.search(query="sqllite", limit=5)
    assert any(r.id == "sqlite-wal" for r in res)


def test_fuzzy_typo_start_of_word_found(tmp_path: Path) -> None:
    """Typo at the START of the word ('tsqlite'/'qslite') must not be lost.

    A prefix-only channel (tok[:3]*) loses start-of-word typos; the fuzzy
    channel must still find the card.
    """
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(tmp_path / "k.db", knowledge_dir=k_dir)

    req = _card("sqlite-wal", title="Настройка SQLite WAL режима")
    (k_dir / "sqlite-wal.md").write_text(
        serialize_card_markdown("sqlite-wal", req), encoding="utf-8"
    )
    storage.sync()

    for typo in ("tsqlite", "qslite"):
        res = storage.search(query=typo, limit=5)
        assert any(r.id == "sqlite-wal" for r in res), f"typo {typo!r} lost the target card"


def test_fuzzy_typo_word_in_body_only_found(tmp_path: Path) -> None:
    """Fuzzy-relevant word lives in the card body (not title): typo still found."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(tmp_path / "k.db", knowledge_dir=k_dir)

    req = _card(
        "sqlite-body",
        title="Настройка режима WAL",
        root_cause="Разбор sqlite таблиц при конкурентных записях блокирует читателей",
    )
    (k_dir / "sqlite-body.md").write_text(
        serialize_card_markdown("sqlite-body", req), encoding="utf-8"
    )
    storage.sync()

    res = storage.search(query="sqllite", limit=5)
    assert any(r.id == "sqlite-body" for r in res)


def test_fuzzy_tail_beyond_200_rows_found(tmp_path: Path) -> None:
    """>250 cards; relevant fuzzy card beyond the first 200 scan rows is found.

    The old unordered LIMIT 200 fallback structurally could not reach it.
    Cards are written in sorted order so the target deterministically gets
    rowid > 200 (stat_cards/rglob order is alphabetical within one directory).
    """
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(tmp_path / "k.db", knowledge_dir=k_dir)

    card_ids = [f"filler-{i:03d}" for i in range(250)] + ["ztarget-999"]
    for cid in card_ids:
        symptom = (
            "Транслитерационный слой скллайт дублирует индекс"
            if cid == "ztarget-999"
            else f"Настройка производительности индексов и кэширование {cid}"
        )
        req = _card(cid, title=f"Карточка {cid}", symptom=symptom)
        (k_dir / f"{cid}.md").write_text(serialize_card_markdown(cid, req), encoding="utf-8")
    storage.sync()

    res = storage.search(query="скллайт", limit=5)
    assert any(r.id == "ztarget-999" for r in res), (
        "relevant card beyond first 200 rows is unreachable by the fuzzy channel"
    )


def test_fuzzy_typo_tail_beyond_200_rows_found(tmp_path: Path) -> None:
    """Typo query over a corpus where the relevant word sits in the tail card."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(tmp_path / "k.db", knowledge_dir=k_dir)

    card_ids = [f"filler-{i:03d}" for i in range(250)] + ["ztarget-999"]
    for cid in card_ids:
        symptom = (
            "Транслитерационный слой sqlite дублирует индекс"
            if cid == "ztarget-999"
            else f"Настройка производительности индексов и кэширование {cid}"
        )
        req = _card(cid, title=f"Карточка {cid}", symptom=symptom)
        (k_dir / f"{cid}.md").write_text(serialize_card_markdown(cid, req), encoding="utf-8")
    storage.sync()

    res = storage.search(query="sqllite", limit=5)
    assert any(r.id == "ztarget-999" for r in res)


# ---------------------------------------------------------------- P1-3
# Single blob cap-600 zeroes jaccard for a word deep inside a long document.


def test_fuzzy_word_in_tail_of_long_document_found(tmp_path: Path) -> None:
    """Typo query must find a card whose relevant word sits at offset > 600."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(tmp_path / "k.db", knowledge_dir=k_dir)

    filler = _filler("безопасныйстандартныйтокен", 40)  # > 600 chars of noise
    req = _card("tail-card", title="Хвостовой документ", symptom=f"{filler} sqlite")
    (k_dir / "tail-card.md").write_text(serialize_card_markdown("tail-card", req), encoding="utf-8")
    storage.sync()

    res = storage.search(query="sqllite", limit=5)
    assert any(r.id == "tail-card" for r in res), (
        "word beyond FUZZY_DOC_CHAR_CAP is invisible to fuzzy"
    )


def test_fuzzy_word_in_tail_of_long_body_found(tmp_path: Path) -> None:
    """Word at the tail of a long solution section is found with a typo query."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(tmp_path / "k.db", knowledge_dir=k_dir)

    filler = _filler("уникальныйструктурныйтекст", 40)
    req = _card(
        "tail-body-card",
        title="Хвостовой документ решения",
        solution=f"Первый этап восстановления {filler} sqlite компоновка страниц",
    )
    (k_dir / "tail-body-card.md").write_text(
        serialize_card_markdown("tail-body-card", req), encoding="utf-8"
    )
    storage.sync()

    res = storage.search(query="sqllite", limit=5)
    assert any(r.id == "tail-body-card" for r in res)


def test_jaccard_tail_word_long_document() -> None:
    """jaccard_word_best sees a fuzzy-relevant word beyond the old 600-char cap."""
    filler = _filler("безопасныйстандартныйтокен", 120)  # ~3.1k chars
    doc = f"{filler} скллайт"
    assert doc.index("скллайт") > 600
    assert jaccard_word_best("скллайт", doc) >= FUZZY_THRESHOLD


def test_jaccard_short_documents_unaffected() -> None:
    """Per-field budgets do not change behaviour on short documents."""
    assert (
        jaccard_word_best("отлдка", "В системе требуется отладка соединения с БД")
        >= FUZZY_THRESHOLD
    )
    assert jaccard_word_best("kubernetes", "Чистый sqlite кэш") == 0.0


# ---------------------------------------------------------------- P2
# Short acronyms on skills (trigram index): substring/prefix SQL channel.


def test_skills_short_acronym_substring_match(tmp_path: Path) -> None:
    """'db' inside a compound token ('db_backup' in description) is found."""
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(
        name="flyway",
        title="Flyway migrations",
        description="db_backup scripts runner",
        category="backend-db",
    )
    storage.index_skill(card, "versioned sql")
    res = storage.search_skills(query="db")
    assert any(c.name == "flyway" for c in res)


def test_skills_short_acronym_prefix_in_name(tmp_path: Path) -> None:
    """'pr' as a prefix of the skill name ('pr-review') is found."""
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(
        name="pr-review",
        title="Code review checklist",
        description="Проверка пулл-реквестов перед слиянием",
        category="process",
    )
    storage.index_skill(card, "review workflow")
    res = storage.search_skills(query="pr")
    assert any(c.name == "pr-review" for c in res)


def test_skills_short_acronym_in_content_body(tmp_path: Path) -> None:
    """'db' as a standalone word inside the content body is found."""
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(
        name="migrations",
        title="Schema migrations",
        description="Alembic workflows",
        category="backend-db",
    )
    storage.index_skill(card, "Запуск миграций db с проверкой версий")
    res = storage.search_skills(query="db")
    assert any(c.name == "migrations" for c in res)


def test_skills_fuzzy_typo_in_content_found(tmp_path: Path) -> None:
    """Typo recall on skills must cover words from the content body too."""
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(
        name="sqlite-tuning",
        title="Производительность баз",
        description="Оптимизация запросов и индексов",
        category="backend-db",
    )
    storage.index_skill(card, "Настройка sqlite WAL и PRAGMA для стабильной производительности")
    res = storage.search_skills(query="sqllite")
    assert any(c.name == "sqlite-tuning" for c in res)


# ------------------------------------------------- False-positive gates (E followup)
# Jaccard threshold alone admits non-typos: added~add shares 1 trigram (J=0.333),
# cat_keyword~dog_keyword has edit distance 3 (J=0.5). Real typos from the corpus
# keep trigram overlap >= 2 AND edit distance <= 2.


def test_fuzzy_gate_rejects_short_common_suffix_match() -> None:
    """A single shared trigram is not a typo match (added vs add)."""
    assert jaccard_word_best("added", "add some value here") < FUZZY_THRESHOLD


def test_fuzzy_gate_rejects_compound_token_prefix_swap() -> None:
    """Different leading subwords of equal-length compound tokens are not typos."""
    assert jaccard_word_best("cat_keyword", "dog_keyword body text") < FUZZY_THRESHOLD


def test_fuzzy_gate_keeps_real_typos() -> None:
    """Corpus typos (edit distance 1-2, overlap >= 2) still match."""
    assert jaccard_word_best("sqllite", "sqlite wal mode") >= FUZZY_THRESHOLD
    assert jaccard_word_best("отлдка", "нужна отладка соединения") >= FUZZY_THRESHOLD
    assert jaccard_word_best("снитаксис", "синтаксис запроса") >= FUZZY_THRESHOLD


def test_skills_search_replaced_content_not_resurrected(tmp_path: Path) -> None:
    """Reindexed skill with replaced content does not match the old query (step15).

    Matches the step15 scenario: frontmatter (title/description) is rewritten by
    the test's sync helper while the fuzzy channel scores title+description+content;
    a stale BM25/trigram hit without a live fuzzy confirmation must not resurrect.
    """
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(
        name="test-skill",
        title="Test keyword",
        description="dog_keyword marker",
        category="planning",
    )
    storage.index_skill(card, "cat_keyword body")
    storage.index_skill(card, "dog_keyword body")
    res = storage.search_skills(query="cat_keyword")
    assert not any(c.name == "test-skill" for c in res)
