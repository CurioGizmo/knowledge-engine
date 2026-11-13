"""Тесты верификации канонической архитектуры FTS5, aliases и поиска по категориям."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_audit.models import (
    AuditIssue,
    AuditPriority,
    AuditStatus,
    PlanArtifact,
    PlanStatus,
    PlanStep,
    ReviewArtifact,
    ReviewComment,
    ReviewVerdict,
)
from mcp_audit.storage import (
    ARTIFACTS_BM25_WEIGHTS,
    AUDIT_BM25_WEIGHTS,
    AUDIT_FTS_COLUMNS,
    AuditStorage,
)
from mcp_core.fuzzy import (
    FUZZY_THRESHOLD,
    build_fuzzy_match,
    jaccard_word_best,
    token_trigrams,
)
from mcp_knowledge.storage import (
    BM25_WEIGHTS,
    REQUIRED_FTS_COLUMNS,
    KnowledgeStorage,
)
from mcp_skills.models import SkillCard
from mcp_skills.storage import (
    REQUIRED_SKILLS_FTS_COLUMNS,
    SKILLS_BM25_WEIGHTS,
    SkillsStorage,
)


def test_bm25_arity_after_rollback():
    """1. Проверка арностей кортежей весов BM25: 20/15/7/2."""
    assert len(BM25_WEIGHTS) == 20
    assert BM25_WEIGHTS[18] == 2.0  # tags
    assert BM25_WEIGHTS[19] == 0.15  # body
    assert len(AUDIT_BM25_WEIGHTS) == 15
    assert AUDIT_BM25_WEIGHTS[12] == 2.5  # aliases
    assert AUDIT_BM25_WEIGHTS[13] == 0.0  # created_at
    assert AUDIT_BM25_WEIGHTS[14] == 0.0  # updated_at
    assert len(SKILLS_BM25_WEIGHTS) == 7
    assert SKILLS_BM25_WEIGHTS[4] == 2.5  # stack
    assert len(ARTIFACTS_BM25_WEIGHTS) == 2
    assert ARTIFACTS_BM25_WEIGHTS == (2.0, 1.0)


def test_required_fts_columns_no_legacy(tmp_path: Path):
    """2. Подтверждение отсутствия устаревшей колонки в REQUIRED_*_FTS_COLUMNS и PRAGMA table_info."""
    legacy_col = "trans" + "lit"

    assert legacy_col not in REQUIRED_FTS_COLUMNS
    # schema-guard: после миграции актуальный контракт audit — ordered AUDIT_FTS_COLUMNS
    # (REQUIRED_AUDIT_FTS_COLUMNS удалён: guard сравнивает ordered-кортеж, не frozenset)
    assert legacy_col not in AUDIT_FTS_COLUMNS
    assert len(AUDIT_FTS_COLUMNS) == 15
    assert legacy_col not in REQUIRED_SKILLS_FTS_COLUMNS

    # Knowledge
    k_storage = KnowledgeStorage(tmp_path / "k.db", tmp_path / "k_dir")
    with k_storage.get_connection() as conn:
        k_cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_index)").fetchall()}
        assert legacy_col not in k_cols
        assert len(k_cols) == 20

    # Audit
    a_storage = AuditStorage(tmp_path / "a.db")
    with a_storage.get_connection() as conn:
        a_cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_index)").fetchall()}
        assert legacy_col not in a_cols
        assert len(a_cols) == 15

        art_cols = {r[1] for r in conn.execute("PRAGMA table_info(artifacts_fts)").fetchall()}
        assert legacy_col not in art_cols
        assert art_cols == {"title", "body"}

    # Skills
    s_storage = SkillsStorage(tmp_path / "s.db")
    with s_storage.get_connection() as conn:
        s_cols = {r[1] for r in conn.execute("PRAGMA table_info(skills_index)").fetchall()}
        assert legacy_col not in s_cols
        assert len(s_cols) == 7


def test_latin_terms_native_search(tmp_path: Path):
    """3. Поиск по латинским терминам (postgres, fastapi, whisper) находит документы нативно."""
    # Knowledge
    k_dir = tmp_path / "knowledge" / "backend"
    k_dir.mkdir(parents=True)
    card_file = k_dir / "backend-postgres-tuning.md"
    card_file.write_text(
        "---\n"
        "id: backend-postgres-tuning\n"
        "title: PostgreSQL connection pooling\n"
        "category: backend\n"
        "type: lesson\n"
        "status: active\n"
        "source: internal\n"
        "generated:\n"
        "  by: human\n"
        "  created_at: '2026-10-14T10:00:00'\n"
        "aliases: ['pg']\n"
        "tags: ['db', 'pool']\n"
        "---\n\n"
        "### Контекст\nPostgreSQL pool latency\n\n"
        "### Решение\nUse pgbouncer\n",
        encoding="utf-8",
    )
    k_storage = KnowledgeStorage(tmp_path / "k.db", tmp_path / "knowledge")
    k_res = k_storage.search("postgres")
    assert len(k_res) == 1
    assert k_res[0].id == "backend-postgres-tuning"

    # Audit
    a_storage = AuditStorage(tmp_path / "a.db")
    a_issue = AuditIssue(
        id="aud-fastapi-auth",
        title="FastAPI dependency injection issue",
        symptom="fastapi auth middleware failure",
        root_cause="Scope lifecycle mismatch",
        solution="Use async generator dependency",
        category="backend",
        status=AuditStatus.PLANNED,
        priority=AuditPriority.IMPORTANT,
    )
    a_storage.index_card(a_issue, str(tmp_path / "issue.md"), 100.0, 200, "todo")
    a_res = a_storage.search_issues(query="fastapi")
    assert len(a_res) == 1
    assert a_res[0]["id"] == "aud-fastapi-auth"

    # Skills
    s_storage = SkillsStorage(tmp_path / "s.db")
    s_card = SkillCard(
        name="whisper-stt",
        title="Whisper STT Integration",
        description="Speech-to-text pipeline with OpenAI Whisper model",
        category="audio-ml",
        stack=["python", "whisper", "audio"],
    )
    s_storage.index_skill(s_card, content="# Whisper STT Guide\nLoad model and transcribe.")
    s_res = s_storage.search_skills(query="whisper")
    assert len(s_res) == 1
    assert s_res[0].name == "whisper-stt"


def test_aliases_slang_search(tmp_path: Path):
    """4. Поиск по сленгу ('постгрес', 'раст', 'нода') находит документы через поле aliases во всех 3 движках."""
    # Knowledge: aliases=['постгрес']
    k_dir = tmp_path / "knowledge" / "backend"
    k_dir.mkdir(parents=True, exist_ok=True)
    card_file = k_dir / "backend-pg-replication.md"
    card_file.write_text(
        "---\n"
        "id: backend-pg-replication\n"
        "title: Replication lag analysis\n"
        "category: backend\n"
        "type: lesson\n"
        "status: active\n"
        "source: internal\n"
        "generated:\n"
        "  by: human\n"
        "  created_at: '2026-10-14T10:00:00'\n"
        "aliases: ['постгрес', 'pg-repl']\n"
        "tags: ['database']\n"
        "---\n\n"
        "### Контекст\nWAL sender latency\n\n"
        "### Решение\nTune max_wal_senders\n",
        encoding="utf-8",
    )
    k_storage = KnowledgeStorage(tmp_path / "k.db", tmp_path / "knowledge")
    k_res = k_storage.search("постгрес")
    assert len(k_res) == 1
    assert k_res[0].id == "backend-pg-replication"

    # Audit: aliases=['раст']
    a_storage = AuditStorage(tmp_path / "a.db")
    a_issue = AuditIssue(
        id="aud-rust-ffi",
        title="Rust FFI boundary checks",
        symptom="Memory safety at C ABI boundary",
        root_cause="Missing null check",
        solution="Validate pointer before deref",
        category="backend",
        status=AuditStatus.PLANNED,
        priority=AuditPriority.IMPORTANT,
        aliases=["раст", "ffi"],
    )
    a_storage.index_card(a_issue, str(tmp_path / "rust_issue.md"), 100.0, 200, "todo")
    a_res = a_storage.search_issues(query="раст")
    assert len(a_res) == 1
    assert a_res[0]["id"] == "aud-rust-ffi"

    # Skills: aliases=['нода']
    s_storage = SkillsStorage(tmp_path / "s.db")
    s_card = SkillCard(
        name="nodejs-microservice",
        title="Node.js Microservice Architecture",
        description="Event loop patterns and stream processing",
        category="backend",
        aliases=["нода", "node"],
        stack=["javascript", "typescript"],
    )
    s_storage.index_skill(s_card, content="# Node.js Guide\nHandling backpressure in streams.")
    s_res = s_storage.search_skills(query="нода")
    assert len(s_res) == 1
    assert s_res[0].name == "nodejs-microservice"


def test_category_keywords_search(tmp_path: Path):
    """5. Поиск по русским ключевым словам категорий ('ревью', 'тесты', 'бэкенд') находит скиллы через CATEGORY_KEYWORDS."""
    s_storage = SkillsStorage(tmp_path / "s.db")

    # quality-review: содержит 'ревью', 'тесты'
    review_card = SkillCard(
        name="code-reviewer",
        title="Code Review Checklist",
        description="Automated and manual pull request inspection",
        category="quality-review",
        stack=["git", "ci"],
    )
    s_storage.index_skill(review_card, content="# Review guidelines")

    # backend-db: содержит 'бэкенд'
    backend_card = SkillCard(
        name="database-migrations",
        title="Database Schema Migrations",
        description="Versioned schema evolution with rollback scripts",
        category="backend-db",
        stack=["sql", "alembic"],
    )
    s_storage.index_skill(backend_card, content="# Migration procedures")

    # Проверка поиска по ключевым словам категорий
    res_review = s_storage.search_skills(query="ревью")
    assert any(c.name == "code-reviewer" for c in res_review)

    res_tests = s_storage.search_skills(query="тесты")
    assert any(c.name == "code-reviewer" for c in res_tests)

    res_backend = s_storage.search_skills(query="бэкенд")
    assert any(c.name == "database-migrations" for c in res_backend)


def test_artifacts_fts_triggers_without_legacy(tmp_path: Path):
    """6. Индексация плана и ревью через AuditStorage, подтверждение корректной работы триггеров artifacts_fts."""
    a_storage = AuditStorage(tmp_path / "a.db")

    # Индексация плана
    plan = PlanArtifact(
        id="plan-auth-v2",
        schema_version=1,
        task_id="AUTH-101",
        title="Auth service redesign",
        category="backend",
        status=PlanStatus.IN_PROGRESS,
        pr_url="https://github.com/org/repo/pull/12",
        steps=[
            PlanStep(index=1, title="Extract JWT verifier", completed=True, note="Done"),
            PlanStep(index=2, title="Migrate session store", completed=False),
        ],
        content="Refactor authentication layer for scalability.",
    )
    a_storage.index_plan(
        plan=plan,
        file_path=str(tmp_path / "plan_auth.md"),
        mtime_ns=1000,
        size=500,
        location="plans",
    )

    with a_storage.get_connection() as conn:
        # Проверяем, что artifacts_fts нашел план через title
        cur = conn.cursor()
        cur.execute(
            "SELECT rowid, title, body FROM artifacts_fts WHERE artifacts_fts MATCH 'redesign';"
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "Auth service redesign"

    # Индексация ревью
    review = ReviewArtifact(
        id="rev-auth-101",
        task_id="AUTH-101",
        category="backend",
        verdict=ReviewVerdict.CHANGES_REQUESTED,
        summary="Security review found session leakage risks",
        comments=[
            ReviewComment(
                file_path="src/auth.py",
                line=12,
                symptom="Insecure cookie flags; recommendation: set Secure and HttpOnly flags",
            )
        ],
    )
    a_storage.index_review(
        review=review,
        file_path=str(tmp_path / "rev_auth.md"),
        mtime_ns=2000,
        size=400,
        location="reviews",
    )

    with a_storage.get_connection() as conn:
        # Проверяем, что artifacts_fts нашел ревью через body
        cur = conn.cursor()
        cur.execute(
            "SELECT rowid, title, body FROM artifacts_fts WHERE artifacts_fts MATCH 'leakage';"
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        assert "Review rev-auth-101" in rows[0][1]

        # Проверка триггера UPDATE (art_au)
        updated_plan = PlanArtifact(
            id="plan-auth-v2",
            schema_version=1,
            task_id="AUTH-101",
            title="Auth service supersonic rewrite",
            category="backend",
            status=PlanStatus.DONE,
            steps=[PlanStep(index=1, title="Supersonic speedup", completed=True)],
            content="Rewrite everything in pure performance mode.",
        )
        a_storage.index_plan(
            plan=updated_plan,
            file_path=str(tmp_path / "plan_auth.md"),
            mtime_ns=3000,
            size=600,
            location="plans",
        )

        cur.execute(
            "SELECT rowid, title, body FROM artifacts_fts WHERE artifacts_fts MATCH 'supersonic';"
        )
        upd_rows = cur.fetchall()
        assert len(upd_rows) == 1
        assert upd_rows[0][1] == "Auth service supersonic rewrite"

        # Старый термин больше не находится
        cur.execute("SELECT rowid FROM artifacts_fts WHERE artifacts_fts MATCH 'redesign';")
        assert len(cur.fetchall()) == 0


def test_fuzzy_and_typos_search():
    """7. Сохранение регрессионного покрытия mcp_core/fuzzy.py."""
    # Триграммы
    tg = token_trigrams("отладка")
    assert "отл" in tg
    assert "тла" in tg
    assert len(token_trigrams("ab")) == 0

    # Построение MATCH-выражения
    match_expr = build_fuzzy_match("постгрес")
    assert " OR " in match_expr
    assert '"пос"' in match_expr

    # Jaccard сходство с опечатками: J("отлдка", "отладка") = 2/7 = 0.285714... >= FUZZY_THRESHOLD
    j_score = jaccard_word_best("отлдка", "В системе требуется отладка соединения с БД")
    assert j_score >= FUZZY_THRESHOLD
    assert abs(j_score - 2 / 7) < 1e-4

    # Без опечатки = 1.0
    assert jaccard_word_best("fastapi", "Использование fastapi фреймворка") == 1.0

    # Несвязанный текст = 0.0
    assert jaccard_word_best("kubernetes", "Чистый sqlite кэш") == 0.0
