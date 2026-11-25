"""Интеграционные тесты для плана fts-trigram-fuzzy-rerank.

Проверяют:
1. Токенизатор trigram во всех четырёх FTS5-таблицах.
2. Версионирование схем через PRAGMA user_version == 3 и авто-rebuild legacy баз (user_version=0).
3. Fuzzy-фоллбэк (опечатки, вызов только при нехватке, дедупликация MATCH > fuzzy).
4. Rerank (арность bm25-проб, отсутствие silent no-op в skills_index, confidence как тай-брейкер).
5. Audit: artifacts_fts external-content репопуляция после rebuild и отсутствие executescript в rebuild.
6. Документированный Non-Goal: короткие токены (<3 символов) возвращают 0 хитов на trigram.
7. flows.db не затрагивается.
"""

from __future__ import annotations

import contextlib
import inspect
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_audit.storage import AuditStorage, _rebuild_audit
from mcp_core.fts import SCHEMA_VERSION
from mcp_core.rerank import matched_column_flags
import mcp_knowledge.storage as k_storage
from mcp_knowledge.models import (
    CardRecordRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import serialize_card_markdown
from mcp_knowledge.storage import (
    KNOWLEDGE_PROBE_WEIGHTS,
    KnowledgeStorage,
)
from mcp_skills.models import SkillCard
from mcp_skills.storage import (
    SKILLS_PROBE_WEIGHTS,
    SkillsStorage,
)


def _norm_sql(sql: str) -> str:
    """Нормализует пробелы вокруг знаков равенства для проверки DDL."""
    return re.sub(r"\s*=\s*", "=", sql)


def test_all_indexes_use_trigram(tmp_path: Path) -> None:
    """Все 4 FTS-индекса создаются с tokenize='trigram...'."""
    k_db = tmp_path / "k.db"
    s_db = tmp_path / "s.db"
    a_db = tmp_path / "a.db"

    k_dir = tmp_path / "k_dir"
    k_dir.mkdir()

    KnowledgeStorage(k_db, knowledge_dir=k_dir)
    SkillsStorage(s_db)
    AuditStorage(a_db)

    # knowledge_index (E1: unicode61 remove_diacritics 2)
    with contextlib.closing(sqlite3.connect(k_db)) as conn:
        k_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'knowledge_index';"
        ).fetchone()[0]
        assert "tokenize='unicode61 remove_diacritics 2'" in _norm_sql(k_sql)

    # skills_index
    with contextlib.closing(sqlite3.connect(s_db)) as conn:
        s_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'skills_index';"
        ).fetchone()[0]
        assert "tokenize='trigram" in _norm_sql(s_sql)

    # audit_index и artifacts_fts
    with contextlib.closing(sqlite3.connect(a_db)) as conn:
        a_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'audit_index';"
        ).fetchone()[0]
        assert "tokenize='trigram" in _norm_sql(a_sql)

        art_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'artifacts_fts';"
        ).fetchone()[0]
        assert "tokenize='trigram" in _norm_sql(art_sql)
        assert "content='artifacts_index'" in _norm_sql(art_sql)
        assert "content_rowid='rowid'" in _norm_sql(art_sql)


def test_schema_version_persisted(tmp_path: Path) -> None:
    """PRAGMA user_version == SCHEMA_VERSION после _init_db; повторный _init_db не делает rebuild.

    v4 (R6, 2026-10-19): запрет среза -ат/-ят и одиночных гласных 2-м проходом
    в stem_russian меняет indexing-side стемы mcp_skills — версии <4 устарели.
    v5 (5бис, 2026-10-28): conflict-колонка file_sync (K12 canonical-резолвер)
    — версии <5 устарели (guard пересоздаёт эфемерный кэш).
    """
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)

    with storage.get_connection() as conn:
        ver = conn.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == SCHEMA_VERSION == 6

    # Проверяем, что при повторном _init_db коллбэк rebuild не выполняется
    rebuild_called = False

    def spy_ensure_schema(conn: sqlite3.Connection, spec: object) -> None:
        def spy_rebuild(c: sqlite3.Connection) -> None:
            nonlocal rebuild_called
            rebuild_called = True
            spec.rebuild(c)  # type: ignore[attr-defined]

        from mcp_core.schema_guard import StorageSchemaSpec

        replaced = StorageSchemaSpec(
            schema_version=spec.schema_version,  # type: ignore[attr-defined]
            fts_checks=spec.fts_checks,  # type: ignore[attr-defined]
            service_ddl=spec.service_ddl,  # type: ignore[attr-defined]
            rebuild=spy_rebuild,
            rebuild_on_missing=spec.rebuild_on_missing,  # type: ignore[attr-defined]
        )
        from mcp_core.schema_guard import ensure_storage_schema as real_ensure_storage_schema

        real_ensure_storage_schema(conn, replaced)

    with patch("mcp_skills.storage.ensure_storage_schema", side_effect=spy_ensure_schema):
        storage._init_db()

    assert not rebuild_called, (
        "Повторный _init_db не должен вызывать rebuild при user_version == SCHEMA_VERSION"
    )


def test_legacy_db_user_version_zero_auto_rebuild(tmp_path: Path) -> None:
    """База со старой схемой (unicode61) и user_version=0 автоматически пересоздаётся в trigram."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE VIRTUAL TABLE skills_index USING fts5(
            name, category UNINDEXED, title, description, stack, content, metadata UNINDEXED,
            tokenize = 'unicode61 remove_diacritics 2'
        );
    """)
    conn.execute(
        "CREATE TABLE file_sync (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, skill_name TEXT);"
    )
    conn.execute("PRAGMA user_version = 0;")
    conn.commit()
    conn.close()

    storage = SkillsStorage(db_path)

    with storage.get_connection() as c:
        ver = c.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == SCHEMA_VERSION  # v4 (R6): актуальная версия схемы
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'skills_index';").fetchone()[0]
        assert "tokenize='trigram" in _norm_sql(sql)


def test_bm25_probe_vector_arity() -> None:
    """Векторы matched_column_flags имеют строгую арность: knowledge=20, skills=7."""
    for col, weights in KNOWLEDGE_PROBE_WEIGHTS.items():
        assert len(weights) == 20, (
            f"Knowledge probe weight '{col}' имеет длину {len(weights)}, ожидалось 20"
        )
        assert weights.count(1.0) == 1, (
            f"Knowledge probe weight '{col}' должен содержать ровно одну 1.0"
        )

    for col, weights in SKILLS_PROBE_WEIGHTS.items():
        assert len(weights) == 7, (
            f"Skills probe weight '{col}' имеет длину {len(weights)}, ожидалось 7"
        )
        assert weights.count(1.0) == 1, (
            f"Skills probe weight '{col}' должен содержать ровно одну 1.0"
        )


def test_skills_rerank_field_matches_not_silent_noop(tmp_path: Path) -> None:
    """Релевантный хит skills имеет хотя бы один field_matches=True (детектор silent no-op)."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)

    card = SkillCard(
        name="sqlite-optimizer",
        title="SQLite Optimizer",
        description="Query performance and FTS5 optimization",
        category="backend-db",
        stack=["sqlite", "python"],
    )
    storage.index_skill(card, content="Detailed content about SQLite and indexes")

    with storage.get_connection() as conn:
        row = conn.execute(
            "SELECT rowid, * FROM skills_index WHERE name = 'sqlite-optimizer'"
        ).fetchone()
        assert row is not None
        # Проверяем, что matched_column_flags с int(rowid) возвращает True для title/description
        flags = matched_column_flags(
            conn,
            "skills_index",
            int(row["rowid"]),
            SKILLS_PROBE_WEIGHTS,
            "sqlite",
        )
        assert any(flags.values()), f"Все флаги совпадения False! flags={flags}"
        assert flags.get("name") is True or flags.get("title") is True


def test_fuzzy_fallback_on_empty_match(tmp_path: Path) -> None:
    """Опечатка ('sqllite') при карточке 'sqlite' находится через fuzzy, score > 0."""
    k_dir = tmp_path / "k_dir"
    k_dir.mkdir()
    db_path = tmp_path / "k.db"
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    req = CardRecordRequest(
        title="Настройка SQLite WAL режима",
        category=KnowledgeCategory.DATABASE,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Ошибки блокировки базы при конкурентном доступе",
        root_cause="Режим отката journal_mode по умолчанию DELETE блокирует читателей",
        solution="Переключение на PRAGMA journal_mode = WAL и synchronous = NORMAL",
        rationale="WAL позволяет читателям и писателям работать параллельно",
        slug="sqlite-wal",
    )
    (k_dir / "sqlite-wal.md").write_text(
        serialize_card_markdown("sqlite-wal", req), encoding="utf-8"
    )
    storage.sync()

    # Ищем с опечаткой 'sqllite'
    res = storage.search(query="sqllite", limit=5)
    assert len(res) == 1, "Карточка должна быть найдена через fuzzy-фоллбэк"
    assert res[0].id == "sqlite-wal"
    assert res[0].score is not None and res[0].score > 0.0


def test_fuzzy_only_as_fallback(tmp_path: Path) -> None:
    """При достатке MATCH-хитов fuzzy-поиск не вызывается."""
    k_dir = tmp_path / "k_dir"
    k_dir.mkdir()
    db_path = tmp_path / "k.db"
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    # Создаём 3 карточки с точным словом 'redis'
    for i in range(3):
        req = CardRecordRequest(
            title=f"Redis кэширование {i}",
            category=KnowledgeCategory.DATABASE,
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.LESSON,
            symptom=f"Медленный доступ к данным {i}",
            root_cause="Отсутствие кэширующего слоя в архитектуре",
            solution="Кэширование ответов через Redis с TTL 300 секунд для снижения нагрузки на базу",
            rationale="Снижает нагрузку на основную базу данных",
            slug=f"redis-cache-{i}",
        )
        (k_dir / f"redis-cache-{i}.md").write_text(
            serialize_card_markdown(f"redis-cache-{i}", req), encoding="utf-8"
        )
    storage.sync()

    with (
        patch.object(
            k_storage, "jaccard_word_best", wraps=k_storage.jaccard_word_best
        ) as mock_jaccard,
        patch(
            "mcp_knowledge.search_scoring.jaccard_word_best", wraps=k_storage.jaccard_word_best
        ) as mock_scoring_jaccard,
    ):
        # Запрашиваем 2 хита при наличии 3 точных
        res = storage.search(query="redis", limit=2)
        assert len(res) == 2
        # jaccard_word_best не должен вызываться, так как MATCH вернул >= limit
        assert mock_jaccard.call_count == 0
        assert mock_scoring_jaccard.call_count == 0


def test_fuzzy_dedup_match_wins(tmp_path: Path) -> None:
    """id, найденный и MATCH'ом, и fuzzy, встречается один раз со score BM25 (не jaccard)."""
    k_dir = tmp_path / "k_dir"
    k_dir.mkdir()
    db_path = tmp_path / "k.db"
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    req = CardRecordRequest(
        title="Оптимизация запросов PostgreSQL",
        category=KnowledgeCategory.DATABASE,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Высокое потребление CPU при сложных запросах",
        root_cause="Последовательное сканирование Seq Scan вместо Index Scan",
        solution="Создание составного B-Tree индекса по фильтруемым полям",
        rationale="Индекс ускоряет фильтрацию до O(log N)",
        slug="postgres-opt",
    )
    (k_dir / "postgres-opt.md").write_text(
        serialize_card_markdown("postgres-opt", req), encoding="utf-8"
    )
    storage.sync()

    # Запрос с 2 токенами: 'postgresql' (точный MATCH) и 'sqllite' (fuzzy мимо)
    # limit=5 > 1, сработает fuzzy-ветка для sqllite, но карточка postgresql уже найдена
    res = storage.search(query="postgresql sqllite", limit=5)
    assert len(res) == 1
    assert res[0].id == "postgres-opt"


def test_rerank_degrades_confidence_to_tiebreaker(tmp_path: Path) -> None:
    """Внутри одного тира доверия порядок определяется score и field_bonuses, а confidence — тай-брейкер."""
    k_dir = tmp_path / "k_dir"
    k_dir.mkdir()
    db_path = tmp_path / "k.db"
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    # Две карточки одного тира (ACTIVE, generated_by: process), с разным confidence
    # card1: совпадение в title (бонус 1.0), но меньший confidence
    req1 = CardRecordRequest(
        title="Docker контейнеризация сборки",
        category=KnowledgeCategory.CICD,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.AUTO_INFERRED,
        status=KnowledgeStatus.ACTIVE,
        symptom="Различие окружений разработки и продакшена",
        root_cause="Отсутствие изолированного окружения сборки",
        solution="Использование многоэтапных Dockerfile с минимальным базовым образом",
        rationale="Гарантирует идентичность артефактов на всех стадиях CI",
        confidence=0.5,
        slug="docker-title-match",
    )
    # card2: совпадение в symptom (бонус 0.5 < бонуса title 1.0), но больший confidence
    req2 = CardRecordRequest(
        title="Стандарт изоляции воркеров",
        category=KnowledgeCategory.CICD,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.AUTO_INFERRED,
        status=KnowledgeStatus.ACTIVE,
        symptom="Различие окружений разработки и продакшена при запуске docker",
        root_cause="Отсутствие изолированного окружения сборки",
        solution="Использование многоэтапных сборок с минимальным базовым образом",
        rationale="Гарантирует идентичность сборки в изолированном контуре",
        confidence=0.9,
        slug="docker-symptom-match",
    )
    (k_dir / "docker-title-match.md").write_text(
        serialize_card_markdown("docker-title-match", req1), encoding="utf-8"
    )
    (k_dir / "docker-symptom-match.md").write_text(
        serialize_card_markdown("docker-symptom-match", req2), encoding="utf-8"
    )
    storage.sync()

    res = storage.search(query="docker", limit=5)
    assert len(res) == 2
    # card1 имеет title match (бонус 1.0) -> должна занять 1-е место, несмотря на меньший confidence
    assert res[0].id == "docker-title-match"
    assert res[1].id == "docker-symptom-match"


def test_audit_artifacts_fts_repopulated_after_rebuild(tmp_path: Path) -> None:
    """Seed в artifacts_index -> rebuild -> artifacts_fts репопулируется (rebuild команда работает)."""
    db_path = tmp_path / "audit.db"
    storage = AuditStorage(db_path)

    # Добавляем артефакт в artifacts_index вручную
    with storage.get_connection() as conn:
        conn.execute("""
            INSERT INTO artifacts_index (
                artifact_id, artifact_type, task_id, category, status, title, body, file_path, mtime_ns
            ) VALUES (
                'plan-test-1', 'plan', 'task-1', 'backend', 'approved', 'План миграции FTS',
                'Детальный текст плана с ключевым словом триграмм', '/path/to/plan.md', 1000
            );
        """)
        # Сбрасываем user_version чтобы спровоцировать rebuild при следующей инициализации
        conn.execute("PRAGMA user_version = 0;")
        conn.commit()

    # Инициализация заново -> rebuild
    storage2 = AuditStorage(db_path)
    with storage2.get_connection() as conn:
        cnt = conn.execute("SELECT count(*) FROM artifacts_fts;").fetchone()[0]
        assert cnt > 0, (
            "artifacts_fts должна быть непустой после rebuild благодаря INSERT ... VALUES('rebuild')"
        )


def test_audit_init_rebuild_no_executescript() -> None:
    """rebuild-callback аудита не содержит вызовов executescript (атомарность транзакции).

    Цель — module-level `_rebuild_audit` (SPEC.rebuild аудита): проверка только
    ensure_storage_schema не защитит от будущего executescript() внутри callback.
    """
    source = inspect.getsource(_rebuild_audit)
    assert "executescript" not in source, "rebuild-callback аудита не должен вызывать executescript"


def test_short_acronym_recall(tmp_path: Path) -> None:
    """Точный fallback по 2-буквенным акронимам находит карточки с 'ai' или 'uv'."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)

    card = SkillCard(
        name="ai-agent-patterns",
        title="AI Agent Patterns",
        description="Core patterns for autonomous AI agents",
        category="planning",
        stack=["python", "ai"],
    )
    storage.index_skill(card, content="AI models and agents")

    card_uv = SkillCard(
        name="uv-usage",
        title="Управление зависимостями через uv",
        description="Быстрый менеджер пакетов Python",
        category="backend-db",
        stack=["python", "uv"],
    )
    storage.index_skill(card_uv, content="Использование uv вместо pip")

    # Поиск по одиночному акрониму 'ai' находит карточку через fallback
    res_ai = storage.search_skills("ai")
    assert len(res_ai) == 1
    assert res_ai[0].name == "ai-agent-patterns"

    # Поиск по одиночному акрониму 'uv' находит uv-usage
    res_uv = storage.search_skills("uv")
    assert len(res_uv) == 1
    assert res_uv[0].name == "uv-usage"


def test_flows_db_never_dropped(tmp_path: Path) -> None:
    """База flows.db не подвергается DROP/rebuild операциям поискового слоя."""
    flows_db = tmp_path / "flows.db"
    conn = sqlite3.connect(flows_db)
    conn.execute("CREATE TABLE flow_states (id TEXT PRIMARY KEY, state TEXT);")
    conn.execute("INSERT INTO flow_states VALUES ('flow-1', 'active');")
    conn.commit()
    conn.close()

    # Проверяем, что инициализация любых хранилищ не трогает сторонний flows.db
    SkillsStorage(tmp_path / "skills.db")
    KnowledgeStorage(tmp_path / "knowledge.db", knowledge_dir=tmp_path)
    AuditStorage(tmp_path / "audit.db")

    with contextlib.closing(sqlite3.connect(flows_db)) as conn:
        row = conn.execute("SELECT state FROM flow_states WHERE id = 'flow-1';").fetchone()
        assert row is not None and row[0] == "active"
