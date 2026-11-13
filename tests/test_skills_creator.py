"""Тесты для модуля создания скиллов create_skill и MCP-инструмента."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from mcp_skills.creator import create_skill
from mcp_skills.errors import SkillAlreadyExistsError, SpamGateRejectedError
from mcp_skills.server import create_server, load_skill, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


@pytest.fixture(autouse=True)
def isolate_skills_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолирует репозиторий скиллов и переменные окружения для каждого теста."""
    import mcp_skills.server as server_module

    server_module._STORAGE = None
    server_module._SYNCHRONIZER = None
    server_module._COMPILER = None
    server_module._GIT_MGR = None
    server_module._BUNDLER = None

    repo_dir = tmp_path / "skills-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_SKILLS_PATH", str(repo_dir))
    monkeypatch.delenv("MCP_SKILLS_CACHE_DIR", raising=False)
    yield repo_dir

    server_module._STORAGE = None
    server_module._SYNCHRONIZER = None
    server_module._COMPILER = None
    server_module._GIT_MGR = None
    server_module._BUNDLER = None


def test_create_skill_success(tmp_path: Path, isolate_skills_env: Path) -> None:
    """Успешное создание нового скилла с валидацией, frontmatter и FTS-индексацией."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])

    res = create_skill(
        name="fastapi-di",
        title="FastAPI Dependency Injection",
        description="Best practices for DI in FastAPI applications",
        content="# FastAPI DI Guide\n\nUse Depends() for clean architecture.",
        category="backend-db",
        stack=["python", "fastapi"],
        available_slots=["auth-provider"],
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )

    assert res["status"] == "created"
    assert res["name"] == "fastapi-di"
    assert "fastapi-di" in res["path"]
    assert "reminder" in res

    skill_file = isolate_skills_env / "skills" / "fastapi-di" / "SKILL.md"
    assert skill_file.is_file()
    content = skill_file.read_text(encoding="utf-8")
    assert "name: fastapi-di" in content
    assert "title: FastAPI Dependency Injection" in content
    assert "category: backend-db" in content
    assert "fastapi" in content
    assert "auth-provider" in content
    assert "# FastAPI DI Guide" in content

    # Проверка мгновенного поиска и сборки
    search_res = search_skills("fastapi", storage=storage, synchronizer=sync)
    assert search_res["count"] == 1
    assert search_res["results"][0]["name"] == "fastapi-di"

    load_res = load_skill("fastapi-di", storage=storage, synchronizer=sync, project_root=tmp_path)
    assert load_res["name"] == "fastapi-di"
    assert "FastAPI DI Guide" in load_res["content"]


def test_create_skill_validation_name(isolate_skills_env: Path) -> None:
    """Отклонение недопустимых имен скилла (path traversal, пробелы, спецсимволы)."""
    invalid_names = [
        "../traversal",
        "UPPER_CASE",
        "has spaces",
        "bad/slash",
        "semi;colon",
        "",
    ]
    for bad_name in invalid_names:
        with pytest.raises(ValueError, match="Недопустимое имя скилла"):
            create_skill(
                name=bad_name,
                title="Bad Skill",
                description="desc",
                content="content",
                skills_repo_dir=isolate_skills_env,
            )


def test_create_skill_validation_slots(isolate_skills_env: Path) -> None:
    """Отклонение недопустимых имен слотов."""
    with pytest.raises(ValueError, match="Недопустимое имя слота"):
        create_skill(
            name="valid-skill",
            title="Valid Skill",
            description="desc",
            content="content",
            available_slots=["bad/slot", "valid-slot"],
            skills_repo_dir=isolate_skills_env,
        )


def test_create_skill_validation_category(isolate_skills_env: Path) -> None:
    """Отклонение неизвестных категорий (разрешены general, зарегистрированные в CATEGORIES)."""
    with pytest.raises(ValueError, match="Недопустимая категория"):
        create_skill(
            name="valid-skill",
            title="Valid Skill",
            description="desc",
            content="content",
            category="non-existent-category-xyz",
            skills_repo_dir=isolate_skills_env,
        )

    # general и backend разрешены
    res_gen = create_skill(
        name="skill-general",
        title="Gen",
        description="d",
        content="body",
        category="general",
        skills_repo_dir=isolate_skills_env,
    )
    assert res_gen["status"] == "created"


def test_create_skill_spam_gate_rejection(isolate_skills_env: Path) -> None:
    """Отклонение контента с секретами через SpamGate."""
    leaked_payload = (
        "# Leaked API\n"
        "Here is the production key:\n"
        "AKIAIOSFODNN7EXAMPLE\n"  # AWS Access Key ID pattern
    )
    with pytest.raises(SpamGateRejectedError) as exc_info:
        create_skill(
            name="leaky-skill",
            title="Leaky Skill",
            description="desc",
            content=leaked_payload,
            skills_repo_dir=isolate_skills_env,
        )
    assert exc_info.value.issues


def test_create_skill_duplicate_protection(isolate_skills_env: Path) -> None:
    """Попытка создания дубликата без overwrite=True вызывает SkillAlreadyExistsError."""
    create_skill(
        name="unique-skill",
        title="Unique Skill",
        description="desc",
        content="First version",
        skills_repo_dir=isolate_skills_env,
    )

    with pytest.raises(SkillAlreadyExistsError) as exc_info:
        create_skill(
            name="unique-skill",
            title="Unique Skill Again",
            description="desc",
            content="Second version",
            overwrite=False,
            skills_repo_dir=isolate_skills_env,
        )
    assert exc_info.value.skill_name == "unique-skill"


def test_create_skill_overwrite_success(isolate_skills_env: Path) -> None:
    """Перезапись скилла с overwrite=True успешно обновляет контент."""
    create_skill(
        name="updatable-skill",
        title="Original Title",
        description="Original desc",
        content="Original content",
        skills_repo_dir=isolate_skills_env,
    )

    res = create_skill(
        name="updatable-skill",
        title="Updated Title",
        description="Updated desc",
        content="Updated content",
        overwrite=True,
        skills_repo_dir=isolate_skills_env,
    )
    assert res["status"] == "updated"

    skill_file = isolate_skills_env / "skills" / "updatable-skill" / "SKILL.md"
    content = skill_file.read_text(encoding="utf-8")
    assert "title: Updated Title" in content
    assert "Updated content" in content
    assert "Original content" not in content


def test_create_skill_strip_redundant_frontmatter(isolate_skills_env: Path) -> None:
    """Если переданный content уже содержит YAML-frontmatter, он не дублируется."""
    body_with_fm = (
        "---\n"
        "name: old-name\n"
        "title: Old Title\n"
        "---\n"
        "# Actual Markdown Body\n"
        "Important instructions."
    )

    create_skill(
        name="clean-skill",
        title="New Canonical Title",
        description="Canonical desc",
        content=body_with_fm,
        skills_repo_dir=isolate_skills_env,
    )

    skill_file = isolate_skills_env / "skills" / "clean-skill" / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    assert text.count("---") == 2
    assert "name: clean-skill" in text
    assert "Old Title" not in text
    assert "# Actual Markdown Body" in text


def test_create_skill_project_isolation(tmp_path: Path, isolate_skills_env: Path) -> None:
    """Целевой рабочий проект НЕ содержит папки .agents/skills при создании скиллов."""
    project_root = tmp_path / "target-project"
    project_root.mkdir()

    create_skill(
        name="isolated-skill",
        title="Isolated Skill",
        description="desc",
        content="# Pure Repository Skill",
        skills_repo_dir=isolate_skills_env,
    )

    assert not (project_root / ".agents").exists()
    assert not (project_root / ".agents" / "skills").exists()


@pytest.mark.asyncio
async def test_mcp_create_skill_tool(tmp_path: Path, isolate_skills_env: Path) -> None:
    """Интеграционный тест вызова MCP-инструмента create_skill через MCPServer."""
    server = create_server(
        project_root=tmp_path,
        skills_dir=isolate_skills_env,
    )

    # 1. Успешный вызов create_skill
    call_res = await server.call_tool(
        "create_skill",
        {
            "name": "mcp-test-skill",
            "title": "MCP Test Skill",
            "description": "Integration testing via MCP server",
            "content": "# Test Instructions\nRun tests properly.",
            "category": "quality-review",
        },
    )
    assert not call_res.is_error
    data = (
        call_res.structured_content
        if call_res.structured_content
        else json.loads(call_res.content[0].text)
    )
    assert data["status"] == "created"
    assert data["name"] == "mcp-test-skill"

    # 2. Попытка дубликата через MCP -> структурированный error envelope
    dup_res = await server.call_tool(
        "create_skill",
        {
            "name": "mcp-test-skill",
            "title": "Duplicate",
            "description": "desc",
            "content": "content",
            "overwrite": False,
        },
    )
    dup_data = (
        dup_res.structured_content
        if dup_res.structured_content
        else json.loads(dup_res.content[0].text)
    )
    assert dup_data.get("error") is True
    assert dup_data.get("error_type") == "SkillAlreadyExistsError"
    assert dup_data.get("reason") == "SKILL_ALREADY_EXISTS"

    # 3. Спам-гейт через MCP -> структурированный error envelope
    spam_res = await server.call_tool(
        "create_skill",
        {
            "name": "spam-skill",
            "title": "Spam",
            "description": "desc",
            "content": "Secret: AKIAIOSFODNN7EXAMPLE",
        },
    )
    spam_data = (
        spam_res.structured_content
        if spam_res.structured_content
        else json.loads(spam_res.content[0].text)
    )
    assert spam_data.get("error") is True
    assert spam_data.get("error_type") == "SpamGateRejectedError"
    assert spam_data.get("reason") == "SPAM_GATE_REJECTED"


def test_create_skill_minimal_no_stack_or_slots(isolate_skills_env: Path) -> None:
    """Создание скилла без стека и слотов: frontmatter опускает эти ключи, title fallback."""
    res = create_skill(
        name="minimal-skill",
        title="",  # fallback на name
        description="",
        content="# Minimal Body",
        skills_repo_dir=isolate_skills_env,
    )
    assert res["status"] == "created"
    assert res["skill"]["title"] == "minimal-skill"
    assert res["skill"]["stack"] == []
    assert res["skill"]["available_slots"] == []

    skill_file = Path(res["path"])
    content = skill_file.read_text(encoding="utf-8")
    assert "name: minimal-skill" in content
    assert "title: minimal-skill" in content
    assert "stack:" not in content
    assert "available_slots:" not in content


def test_create_skill_custom_category_from_storage(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """Кастомная категория принимается, если она зарегистрирована в базе данных проекта."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)

    # 1. До добавления в БД кастомная категория отвергается
    with pytest.raises(ValueError, match="Недопустимая категория"):
        create_skill(
            name="custom-category-skill",
            title="Custom Domain",
            description="desc",
            content="content",
            category="audio-dsp-custom",
            skills_repo_dir=isolate_skills_env,
            storage=storage,
        )

    # 2. Индексируем один скилл с этой категорией в БД
    from mcp_skills.models import SkillCard

    seed_card = SkillCard(
        name="seed-skill",
        title="Seed",
        description="seed",
        category="audio-dsp-custom",
        base_dir=isolate_skills_env,
    )
    storage.index_skill(seed_card, "# Seed body")
    assert "audio-dsp-custom" in storage.get_category_counts()

    # 3. Теперь create_skill с кастомной категорией успешен
    res = create_skill(
        name="custom-category-skill",
        title="Custom Domain",
        description="desc",
        content="# Custom category instructions",
        category="audio-dsp-custom",
        skills_repo_dir=isolate_skills_env,
        storage=storage,
    )
    assert res["status"] == "created"
    assert res["skill"]["category"] == "audio-dsp-custom"


def test_create_skill_validation_unicode_and_hyphens(isolate_skills_env: Path) -> None:
    """Валидация имен и слотов: кириллица, дефисы в начале, спецсимволы, пустые слоты."""
    # Кириллица в имени
    with pytest.raises(ValueError, match="Недопустимое имя скилла"):
        create_skill(
            name="скилл-тест",
            title="Русское имя",
            description="desc",
            content="content",
            skills_repo_dir=isolate_skills_env,
        )

    # Имя, начинающееся с дефиса
    with pytest.raises(ValueError, match="Недопустимое имя скилла"):
        create_skill(
            name="-hyphen-start",
            title="Hyphen Start",
            description="desc",
            content="content",
            skills_repo_dir=isolate_skills_env,
        )

    # Недопустимые слоты: верхний регистр, пробелы, кириллица, пустая строка
    for bad_slot in ["SLOT-A", "slot with space", "слот", ""]:
        with pytest.raises(ValueError, match="Недопустимое имя слота"):
            create_skill(
                name="slot-test-skill",
                title="Slot Test",
                description="desc",
                content="content",
                available_slots=[bad_slot],
                skills_repo_dir=isolate_skills_env,
            )


def test_create_skill_unicode_title_and_content_preservation(isolate_skills_env: Path) -> None:
    """Юникод (кириллица, эмодзи) корректно сохраняется в YAML frontmatter и markdown теле."""
    title = "Тестирование и валидация 🚀"
    content = "# Руководство по тестированию 🎯\n\nПроверяйте граничные условия и инварианты."

    res = create_skill(
        name="unicode-skill",
        title=title,
        description="Описание на русском языке",
        content=content,
        category="quality-review",
        skills_repo_dir=isolate_skills_env,
    )

    skill_file = Path(res["path"])
    raw_text = skill_file.read_text(encoding="utf-8")
    assert title in raw_text
    assert "Описание на русском языке" in raw_text
    assert "Руководство по тестированию 🎯" in raw_text
    assert "\\u04" not in raw_text  # ensure YAML didn't escape Cyrillic


def test_create_skill_creates_missing_directories(tmp_path: Path) -> None:
    """create_skill создает несуществующие промежуточные директории на диске."""
    non_existent_repo = tmp_path / "deeply" / "nested" / "non_existent_repo"
    assert not non_existent_repo.exists()

    res = create_skill(
        name="nested-dir-skill",
        title="Nested Dir Skill",
        description="desc",
        content="# Auto Created Dirs",
        skills_repo_dir=non_existent_repo,
    )
    assert res["status"] == "created"
    skill_file = Path(res["path"])
    assert skill_file.is_file()
    assert non_existent_repo.is_dir()


def test_create_skill_duplicate_with_storage_populates_existing_card(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """При конфликте имен и переданном storage ошибка содержит данные существующей карточки."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])

    create_skill(
        name="conflict-skill",
        title="Original Title",
        description="Original Desc",
        content="# Original Body",
        category="planning",
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )

    with pytest.raises(SkillAlreadyExistsError) as exc_info:
        create_skill(
            name="conflict-skill",
            title="Duplicate Title",
            description="Duplicate Desc",
            content="# Duplicate Body",
            overwrite=False,
            skills_repo_dir=isolate_skills_env,
            storage=storage,
            synchronizer=sync,
        )

    assert exc_info.value.skill_name == "conflict-skill"
    assert exc_info.value.existing_card.get("name") == "conflict-skill"
    assert exc_info.value.existing_card.get("title") == "Original Title"
    assert exc_info.value.existing_card.get("category") == "planning"


def test_create_skill_overwrite_updates_slots_category_and_reindexes(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """Перезапись скилла с изменением категории и слотов немедленно обновляет FTS5-индекс."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])

    # 1. Создаем v1
    create_skill(
        name="evolving-skill",
        title="Version 1",
        description="v1 desc",
        content="# Content V1 with postgresql",
        category="backend-db",
        stack=["python"],
        available_slots=["database"],
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )

    found_v1, _ = storage.get_skill("evolving-skill")
    assert found_v1.category == "backend-db"
    assert found_v1.available_slots == ["database"]

    # 2. Перезаписываем v2 с новой категорией, новыми слотами и новым текстом
    res_v2 = create_skill(
        name="evolving-skill",
        title="Version 2",
        description="v2 desc",
        content="# Content V2 with playwright and security audit",
        category="quality-review",
        stack=["typescript"],
        available_slots=["e2e-runner"],
        overwrite=True,
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )
    assert res_v2["status"] == "updated"

    # 3. Проверка FTS5 и хранилища
    found_v2, body_v2 = storage.get_skill("evolving-skill")
    assert found_v2.title == "Version 2"
    assert found_v2.category == "quality-review"
    assert found_v2.available_slots == ["e2e-runner"]
    assert "playwright and security audit" in body_v2

    # Поиск по новой категории находит скилл, а по старому — нет
    res_qr = search_skills(
        "evolving", category="quality-review", storage=storage, synchronizer=sync
    )
    assert res_qr["count"] == 1
    assert res_qr["results"][0]["name"] == "evolving-skill"

    res_be = search_skills("evolving", category="backend-db", storage=storage, synchronizer=sync)
    assert res_be["count"] == 0


def test_create_skill_spam_gate_size_limit_and_empty_content(isolate_skills_env: Path) -> None:
    """SpamGate отклоняет контент размером более 100 КБ или пустой контент."""
    # 1. Превышение 100 КБ
    huge_content = "# Massive Guide\n" + ("x" * (100 * 1024 + 50))
    with pytest.raises(SpamGateRejectedError) as exc_huge:
        create_skill(
            name="huge-skill",
            title="Huge Skill",
            description="desc",
            content=huge_content,
            skills_repo_dir=isolate_skills_env,
        )
    assert any("100 КБ" in issue for issue in exc_huge.value.issues)

    # 2. Пустой контент
    for empty_val in ["", "   \n\t  "]:
        with pytest.raises(SpamGateRejectedError) as exc_empty:
            create_skill(
                name="empty-skill",
                title="Empty Skill",
                description="desc",
                content=empty_val,
                skills_repo_dir=isolate_skills_env,
            )
        assert any("пуст" in issue for issue in exc_empty.value.issues)


def test_create_skill_crlf_frontmatter(isolate_skills_env: Path) -> None:
    """CRLF переводы строк в существующем frontmatter корректно обрабатываются."""
    crlf_body = (
        "---\r\nname: old-crlf\r\ntitle: Old CRLF\r\n---\r\n# Clean Body CRLF\r\nBody content.\r\n"
    )

    create_skill(
        name="crlf-skill",
        title="CRLF Skill",
        description="desc",
        content=crlf_body,
        skills_repo_dir=isolate_skills_env,
    )

    skill_file = isolate_skills_env / "skills" / "crlf-skill" / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    assert text.count("---") == 2
    assert "name: crlf-skill" in text
    assert "Old CRLF" not in text
    assert "# Clean Body CRLF" in text


@pytest.mark.asyncio
async def test_mcp_create_skill_validation_error_and_server_liveness(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """MCP сервер оборачивает ValueError в error-envelope и сохраняет работоспособность."""
    server = create_server(
        project_root=tmp_path,
        skills_dir=isolate_skills_env,
    )

    # 1. Недопустимое имя -> ValueError envelope
    name_err_res = await server.call_tool(
        "create_skill",
        {
            "name": "INVALID UPPER NAME",
            "title": "Bad",
            "description": "desc",
            "content": "valid content",
        },
    )
    data_name = (
        name_err_res.structured_content
        if name_err_res.structured_content
        else json.loads(name_err_res.content[0].text)
    )
    assert data_name.get("error") is True
    assert data_name.get("error_type") == "ValueError"
    assert data_name.get("reason") == "INVALID_ARGUMENTS"

    # 2. Недопустимая категория -> ValueError envelope
    category_err_res = await server.call_tool(
        "create_skill",
        {
            "name": "valid-name",
            "title": "Valid Title",
            "description": "desc",
            "content": "valid content",
            "category": "unknown-nonexistent-category",
        },
    )
    data_category = (
        category_err_res.structured_content
        if category_err_res.structured_content
        else json.loads(category_err_res.content[0].text)
    )
    assert data_category.get("error") is True
    assert data_category.get("error_type") == "ValueError"
    assert data_category.get("reason") == "INVALID_ARGUMENTS"

    # 3. Недопустимый слот -> ValueError envelope
    slot_err_res = await server.call_tool(
        "create_skill",
        {
            "name": "valid-name",
            "title": "Valid Title",
            "description": "desc",
            "content": "valid content",
            "available_slots": ["BAD/SLOT"],
        },
    )
    data_slot = (
        slot_err_res.structured_content
        if slot_err_res.structured_content
        else json.loads(slot_err_res.content[0].text)
    )
    assert data_slot.get("error") is True
    assert data_slot.get("error_type") == "ValueError"
    assert data_slot.get("reason") == "INVALID_ARGUMENTS"

    # 4. Проверка liveness: последующий валидный вызов create_skill и search_skills успешны
    ok_res = await server.call_tool(
        "create_skill",
        {
            "name": "liveness-skill",
            "title": "Liveness Skill",
            "description": "desc",
            "content": "# Live and kicking",
            "category": "backend-db",
        },
    )
    assert not ok_res.is_error
    ok_data = (
        ok_res.structured_content
        if ok_res.structured_content
        else json.loads(ok_res.content[0].text)
    )
    assert ok_data.get("status") == "created"

    search_res = await server.call_tool("search_skills", {"query": "liveness"})
    assert not search_res.is_error
    search_data = (
        search_res.structured_content
        if search_res.structured_content
        else json.loads(search_res.content[0].text)
    )
    assert search_data.get("count") == 1
    assert search_data["results"][0]["name"] == "liveness-skill"


def test_create_skill_spam_gate_catches_secret_in_metadata(isolate_skills_env: Path) -> None:
    """SpamGate валидирует весь Markdown: секреты в title и description отклоняются."""
    leaked_title = "Skill with AKIAIOSFODNN7EXAMPLE key"
    with pytest.raises(SpamGateRejectedError) as exc_info:
        create_skill(
            name="leak-title-skill",
            title=leaked_title,
            description="Clean description",
            content="# Normal content",
            skills_repo_dir=isolate_skills_env,
        )
    assert exc_info.value.issues

    leaked_desc = "Skill with ASIAIOSFODNN7EXAMPLE token"
    with pytest.raises(SpamGateRejectedError) as exc_info2:
        create_skill(
            name="leak-desc-skill",
            title="Clean Title",
            description=leaked_desc,
            content="# Normal content",
            skills_repo_dir=isolate_skills_env,
        )
    assert exc_info2.value.issues


def test_create_skill_spam_gate_expanded_patterns(isolate_skills_env: Path) -> None:
    """SpamGate обнаруживает AWS STS ASIA и GitHub fine-grained PAT."""
    # AWS STS
    with pytest.raises(SpamGateRejectedError):
        create_skill(
            name="sts-leak",
            title="STS Leak",
            description="desc",
            content="AWS_SESSION_TOKEN=ASIAIOSFODNN7EXAMPLE",
            skills_repo_dir=isolate_skills_env,
        )

    # GitHub fine-grained PAT (github_pat_ followed by 82 chars)
    gh_pat = "github_pat_" + "a" * 82
    with pytest.raises(SpamGateRejectedError):
        create_skill(
            name="gh-pat-leak",
            title="GH PAT Leak",
            description="desc",
            content=f"export GITHUB_TOKEN={gh_pat}",
            skills_repo_dir=isolate_skills_env,
        )


def test_create_skill_validation_stack(isolate_skills_env: Path) -> None:
    """Элементы списка stack валидируются на kebab-case без спецсимволов."""
    for bad_stack_item in ["UPPERCASE", "with space", "bad/slash", "кириллица", ""]:
        with pytest.raises(ValueError, match="Недопустимое имя технологии в стеке"):
            create_skill(
                name="stack-test-skill",
                title="Stack Test",
                description="desc",
                content="content",
                stack=[bad_stack_item],
                skills_repo_dir=isolate_skills_env,
            )


def test_create_skill_symlink_guard(tmp_path: Path, isolate_skills_env: Path) -> None:
    """Попытка создания скилла через символическую ссылку отклоняется."""
    outside_dir = tmp_path / "outside_secret_dir"
    outside_dir.mkdir()

    symlink_target = isolate_skills_env / "skills" / "symlink-skill"
    symlink_target.parent.mkdir(parents=True, exist_ok=True)
    symlink_target.symlink_to(outside_dir)

    with pytest.raises(ValueError, match="обнаружена символическая ссылка"):
        create_skill(
            name="symlink-skill",
            title="Symlink Skill",
            description="desc",
            content="Dangerous payload",
            skills_repo_dir=isolate_skills_env,
        )


def test_create_skill_with_aliases_and_search(isolate_skills_env: Path) -> None:
    """Алиасы валидируются, сохраняются в frontmatter и индексируются в FTS5."""
    from mcp_skills.storage import SkillsStorage
    from mcp_skills.synchronizer import SkillsSynchronizer

    db_path = isolate_skills_env / ".agents" / ".cache" / "skills_fts.db"
    storage = SkillsStorage(db_path)
    synchronizer = SkillsSynchronizer(storage, skills_dirs=[isolate_skills_env / "skills"])

    # 1. Создание с кириллическими алиасами и пробелами
    res = create_skill(
        name="db-speedup",
        title="Database Speedup",
        description="Тюнинг SQLite",
        content="# Инструкции по оптимизации SQLite",
        stack=["sqlite", "sql"],
        aliases=["ускорить базу данных", "оптимизация sql", "медленные запросы"],
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=synchronizer,
    )
    assert res["status"] == "created"

    # Добавим другие скиллы, чтобы в базе было >= 3 документов для положительного BM25 IDF
    for i in range(3):
        create_skill(
            name=f"other-skill-{i}",
            title=f"Other {i}",
            description="Other stuff",
            content="Nothing about databases here",
            skills_repo_dir=isolate_skills_env,
            storage=storage,
            synchronizer=synchronizer,
        )

    # 2. Проверяем файл на диске
    skill_file = isolate_skills_env / "skills" / "db-speedup" / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    assert "aliases:" in text
    assert "- ускорить базу данных" in text

    # 3. Поиск по алиасу находит скилл
    search_res = storage.search_skills("ускорить базу данных")
    assert len(search_res) > 0
    assert search_res[0].name == "db-speedup"
    assert search_res[0].score is not None and search_res[0].score > 2.0

    # 4. Валидация: запрещённые символы
    with pytest.raises(ValueError, match="Недопустимый алиас"):
        create_skill(
            name="bad-alias-skill",
            title="Bad",
            description="desc",
            content="content",
            aliases=["bad/slash/alias"],
            skills_repo_dir=isolate_skills_env,
        )

    # 5. Валидация: превышение длины
    with pytest.raises(ValueError, match="Недопустимый алиас"):
        create_skill(
            name="too-long-alias",
            title="Long",
            description="desc",
            content="content",
            aliases=["a" * 101],
            skills_repo_dir=isolate_skills_env,
        )


# ============================================================================
# Модульные блоки (blocks) и конвенции стека (stack_descriptions)
# ============================================================================


def test_create_skill_with_blocks_success(tmp_path: Path, isolate_skills_env: Path) -> None:
    """Запись blocks/<slot>/<value>.md с валидацией и отражением в ответе blocks_written."""
    res = create_skill(
        name="dual-mode-skill",
        title="Dual Mode Skill",
        description="Skill with mode blocks",
        content="# Dual Mode\n\n<!-- SLOT: mode -->\n\nBase text.",
        category="general",
        stack=["mcp", "offline"],
        blocks={
            "mode": {
                "default": "Зонд: проверь доступность MCP, выбери блок.",
                "mcp": "Протокол через MCP-вызовы.",
                "offline": "Файловый протокол без вызовов.",
            }
        },
        skills_repo_dir=isolate_skills_env,
    )

    assert res["status"] == "created"
    assert sorted(res["blocks_written"]) == [
        "blocks/mode/default.md",
        "blocks/mode/mcp.md",
        "blocks/mode/offline.md",
    ]
    for rel in res["blocks_written"]:
        assert (isolate_skills_env / "skills" / "dual-mode-skill" / rel).is_file()
    mcp_block = isolate_skills_env / "skills" / "dual-mode-skill" / "blocks" / "mode" / "mcp.md"
    assert "MCP-вызовы" in mcp_block.read_text(encoding="utf-8")


def test_create_skill_blocks_invalid_slot_name(isolate_skills_env: Path) -> None:
    """Недопустимое имя слота в blocks отклоняется до записи файлов."""
    with pytest.raises(ValueError, match="Недопустимое имя слота в blocks"):
        create_skill(
            name="bad-blocks-slot",
            title="Bad",
            description="desc",
            content="content",
            blocks={"../evil": {"default": "x"}},
            skills_repo_dir=isolate_skills_env,
        )


def test_create_skill_blocks_invalid_value_name(isolate_skills_env: Path) -> None:
    """Недопустимое значение блока (path traversal / верхний регистр) отклоняется."""
    with pytest.raises(ValueError, match="Недопустимое значение блока"):
        create_skill(
            name="bad-blocks-value",
            title="Bad",
            description="desc",
            content="content",
            blocks={"mode": {"../traversal": "x"}},
            skills_repo_dir=isolate_skills_env,
        )
    with pytest.raises(ValueError, match="Недопустимое значение блока"):
        create_skill(
            name="bad-blocks-value-2",
            title="Bad",
            description="desc",
            content="content",
            blocks={"mode": {"MCP": "x"}},  # не kebab-case
            skills_repo_dir=isolate_skills_env,
        )


def test_create_skill_blocks_invalid_structure(isolate_skills_env: Path) -> None:
    """blocks[...] должен быть словарём {value: content}, не строкой."""
    with pytest.raises(ValueError, match="ожидается словарь"):
        create_skill(
            name="bad-blocks-struct",
            title="Bad",
            description="desc",
            content="content",
            blocks={"mode": "not-a-dict"},
            skills_repo_dir=isolate_skills_env,
        )


def test_create_skill_blocks_empty_content_rejected(isolate_skills_env: Path) -> None:
    """Пустой контент блока отклоняется."""
    with pytest.raises(ValueError, match="контент обязателен"):
        create_skill(
            name="bad-blocks-empty",
            title="Bad",
            description="desc",
            content="content",
            blocks={"mode": {"default": "   "}},
            skills_repo_dir=isolate_skills_env,
        )


def test_create_skill_blocks_not_written_when_validation_fails(isolate_skills_env: Path) -> None:
    """При невалидном одном блоке валидные из той же пачки тоже не пишутся? Нет:
    SKILL.md уже записан, но блоки валидируются по мере записи — проверяем,
    что валидация имени срабатывает до записи файлов блока.
    """
    # Слот с невалидным именем — упадёт до записи любого блока
    with pytest.raises(ValueError, match="Недопустимое имя слота в blocks"):
        create_skill(
            name="partial-fail-skill",
            title="Partial",
            description="desc",
            content="content",
            blocks={
                "mode": {"default": "ok"},
                "bad slot!": {"default": "x"},
            },
            skills_repo_dir=isolate_skills_env,
        )
    blocks_dir = isolate_skills_env / "skills" / "partial-fail-skill" / "blocks"
    assert not blocks_dir.exists()


def test_create_skill_stack_descriptions_fill_conventions(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """stack_descriptions попадает в docs/stack-conventions.yaml при создании скилла."""
    res = create_skill(
        name="conv-skill",
        title="Conventions Skill",
        description="desc",
        content="content",
        stack=["python", "rust"],
        stack_descriptions={
            "python": "Язык проекта — использовать python-блоки",
            "rust": "Системный язык — использовать rust-блоки",
        },
        skills_repo_dir=isolate_skills_env,
    )
    assert res["status"] == "created"

    conv_path = isolate_skills_env / "docs" / "stack-conventions.yaml"
    assert conv_path.is_file()
    conv_text = conv_path.read_text(encoding="utf-8")
    assert "python:" in conv_text
    assert "Язык проекта — использовать python-блоки" in conv_text
    assert "rust:" in conv_text

    # Второй проход (update) не перетирает существующую семантику
    create_skill(
        name="conv-skill",
        title="Conventions Skill",
        description="desc",
        content="updated content",
        stack=["python"],
        stack_descriptions={"python": "ДРУГАЯ семантика, не должна перетереть"},
        overwrite=True,
        skills_repo_dir=isolate_skills_env,
    )
    conv_text_2 = conv_path.read_text(encoding="utf-8")
    assert "Язык проекта — использовать python-блоки" in conv_text_2
    assert "ДРУГАЯ семантика" not in conv_text_2


def test_create_skill_stack_conventions_null_without_description(isolate_skills_env: Path) -> None:
    """Новое значение стека без stack_descriptions пишется с null — до ручного описания."""
    create_skill(
        name="null-conv-skill",
        title="Null Conv",
        description="desc",
        content="content",
        stack=["zig"],
        skills_repo_dir=isolate_skills_env,
    )
    conv_path = isolate_skills_env / "docs" / "stack-conventions.yaml"
    conv_text = conv_path.read_text(encoding="utf-8")
    assert "zig:" in conv_text
    # null-описание — валидатор пропустит запись с warning
    assert "description: null" in conv_text or "description:" in conv_text


def test_create_skill_no_stack_leaves_conventions_untouched(isolate_skills_env: Path) -> None:
    """Без stack docs/stack-conventions.yaml не создаётся вовсе."""
    create_skill(
        name="no-stack-skill",
        title="No Stack",
        description="desc",
        content="content",
        skills_repo_dir=isolate_skills_env,
    )
    assert not (isolate_skills_env / "docs" / "stack-conventions.yaml").exists()


def test_create_skill_blocks_write_and_reassemble(tmp_path: Path, isolate_skills_env: Path) -> None:
    """Сквозной сценарий: blocks записаны → load_skill со stack подхватывает нужный блок."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])

    create_skill(
        name="assembly-skill",
        title="Assembly",
        description="desc",
        content="# Assembly\n\n<!-- SLOT: mode -->\n\nTail.",
        blocks={
            "mode": {
                "default": "DEFAULT-BLOCK",
                "mcp": "MCP-BLOCK",
                "offline": "OFFLINE-BLOCK",
            }
        },
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )

    loaded_mcp = load_skill("assembly-skill", stack=["mcp"], storage=storage, synchronizer=sync)
    assert "MCP-BLOCK" in loaded_mcp["content"]
    assert "DEFAULT-BLOCK" not in loaded_mcp["content"]

    loaded_default = load_skill(
        "assembly-skill", storage=storage, synchronizer=sync, project_root=tmp_path
    )
    assert "DEFAULT-BLOCK" in loaded_default["content"]

    loaded_offline = load_skill(
        "assembly-skill", stack=["offline"], storage=storage, synchronizer=sync
    )
    assert "OFFLINE-BLOCK" in loaded_offline["content"]


# ============================================================================
# Кэш stack_values (инвалидация по file_sync + mtime конвенций)
# ============================================================================


def test_stack_values_cache_invalidates_on_new_block(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """После create_skill с новым блоком stack_values отражает новое значение оси.

    Защита от протухшего кэша: file_sync обновляется синхронизатором, сигнатура
    меняется, ФС-скан выполняется заново.
    """
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])

    # 1. До создания скилла — пусто
    import mcp_skills.server as server_module

    server_module._STACK_VALUES_CACHE.pop(storage, None)
    empty = server_module._collect_stack_values(storage, skills_dir=isolate_skills_env)
    assert "cobol" not in empty

    # 2. Создаём скилл с новым значением оси
    create_skill(
        name="cache-check-skill",
        title="Cache Check",
        description="desc",
        content="content",
        blocks={"mode": {"cobol": "COBOL-protocol"}},
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )

    # 3. Кэш должен инвалидироваться: новое значение видно
    updated = server_module._collect_stack_values(storage, skills_dir=isolate_skills_env)
    assert "cobol" in updated
    assert updated["cobol"] == "локальная вариация блока для этого значения"


def test_stack_values_cache_hit_avoids_rescan(
    tmp_path: Path, isolate_skills_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторный вызов с неизменным индексом возвращает кэш без ФС-скана blocks/."""
    import mcp_skills.server as server_module

    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])
    create_skill(
        name="cache-hit-skill",
        title="Cache Hit",
        description="desc",
        content="content",
        blocks={"mode": {"mcp": "x"}},
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )

    first = server_module._collect_stack_values(storage, skills_dir=isolate_skills_env)
    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self, *args, **kwargs):
        if "blocks" in str(self):
            calls["n"] += 1
        return real_iterdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    second = server_module._collect_stack_values(storage, skills_dir=isolate_skills_env)
    assert second == first
    assert calls["n"] == 0, "Кэш не сработал: выполнен ФС-скан blocks/"


# ============================================================================
# A3/A4 (review-fixes-2026-10): overwrite по base_dir из индекса + merge frontmatter
# ============================================================================


def _write_manual_skill(repo: Path, rel_dir: str, name: str, extra_frontmatter: str = "") -> Path:
    """Создаёт скилл вручную на диске (вне create_skill) — имитирует репо с категорийными папками."""
    skill_dir = repo / "skills" / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    header = f"---\nname: {name}\ntitle: {name} original\ndescription: original\n"
    if extra_frontmatter:
        header += extra_frontmatter
    skill_file.write_text(header + "---\n\n# Original body\n", encoding="utf-8")
    return skill_file


def test_create_skill_overwrite_updates_existing_base_dir(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """A3: overwrite скилла, найденного в индексе вне skills/<name>, обновляет оригинал.

    Скилл 'demo' лежит в skills/backend-db/demo/ (категорийная папка, проиндексирована
    синхронизатором). create_skill(name='demo', overwrite=True) обязан писать в
    существующий base_dir из индекса, а не создавать дубликат в skills/demo/.
    """
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])

    original_file = _write_manual_skill(isolate_skills_env, "backend-db/demo", "demo")
    sync.sync()
    found, _ = storage.get_skill("demo")
    assert found is not None, "Скилл должен быть в индексе после sync"

    res = create_skill(
        name="demo",
        title="Demo v2",
        description="v2 desc",
        content="# Updated body",
        overwrite=True,
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )
    assert res["status"] == "updated"

    # Дубликата в skills/demo/ нет
    assert not (isolate_skills_env / "skills" / "demo" / "SKILL.md").exists()
    assert not (isolate_skills_env / "skills" / "demo").exists()

    # Оригинал в backend-db/demo/ обновлён
    text = original_file.read_text(encoding="utf-8")
    assert "# Updated body" in text
    assert "title: Demo v2" in text
    assert "# Original body" not in text

    # Индекс указывает на то же место, тело актуально
    found2, body2 = storage.get_skill("demo")
    assert Path(found2.base_dir) == original_file.parent
    assert "Updated body" in body2


def test_create_skill_new_skill_without_index_uses_default_path(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """A3 (guard): скилла нет в индексе — создание нового пишет в skills/<name> как раньше."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])

    res = create_skill(
        name="fresh-skill",
        title="Fresh",
        description="desc",
        content="# Fresh body",
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )
    assert res["status"] == "created"
    skill_file = isolate_skills_env / "skills" / "fresh-skill" / "SKILL.md"
    assert skill_file.is_file()
    assert "# Fresh body" in skill_file.read_text(encoding="utf-8")


def test_create_skill_overwrite_preserves_frontmatter_fields(
    tmp_path: Path, isolate_skills_env: Path
) -> None:
    """A4a: поля существующего frontmatter, не задаваемые вызовом, не теряются при overwrite.

    requires_stack/activation/kind были в файле; create_skill их не принимает —
    после overwrite=True они обязаны сохраниться в записанном SKILL.md и в индексе.
    """
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [isolate_skills_env / "skills"])

    original_file = _write_manual_skill(
        isolate_skills_env,
        "backend-db/preserved-skill",
        "preserved-skill",
        extra_frontmatter=(
            "requires_stack: [sqlite]\n"
            "activation: manual\n"
            "kind: extension\n"
            "extends: base-skill\n"
            "subagents: [research, coder]\n"
        ),
    )
    sync.sync()
    found, _ = storage.get_skill("preserved-skill")
    assert found is not None
    assert found.requires_stack == ["sqlite"]
    assert found.activation == "manual"

    res = create_skill(
        name="preserved-skill",
        title="Preserved v2",
        description="v2 desc",
        content="# Preserved updated body",
        overwrite=True,
        skills_repo_dir=isolate_skills_env,
        storage=storage,
        synchronizer=sync,
    )
    assert res["status"] == "updated"

    text = original_file.read_text(encoding="utf-8")
    assert "# Preserved updated body" in text
    assert "requires_stack:" in text
    assert "sqlite" in text
    assert "activation: manual" in text
    assert "kind: extension" in text
    assert "extends: base-skill" in text
    assert "subagents:" in text

    # Поля пережили цикл запись -> sync -> индекс
    found2, _ = storage.get_skill("preserved-skill")
    assert found2.requires_stack == ["sqlite"]
    assert found2.activation == "manual"
    assert found2.kind == "extension"
    assert found2.extends == "base-skill"
    assert found2.subagents == ["research", "coder"]


def test_create_skill_minimal_frontmatter_stays_compact(isolate_skills_env: Path) -> None:
    """A4a (guard): скилл без доп. полей — frontmatter не раздувается дефолтами."""
    create_skill(
        name="compact-skill",
        title="Compact",
        description="desc",
        content="# Compact body",
        skills_repo_dir=isolate_skills_env,
    )
    text = (isolate_skills_env / "skills" / "compact-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for absent_key in (
        "kind:",
        "extends:",
        "merge_strategy:",
        "subagents:",
        "scope:",
        "activation:",
        "requires_stack:",
    ):
        assert absent_key not in text


def test_create_skill_blocks_invalid_leaves_no_skill_md(isolate_skills_env: Path) -> None:
    """A4b: невалидный блок -> ValueError, и SKILL.md не остаётся на диске."""
    with pytest.raises(ValueError, match="Недопустимое имя слота в blocks"):
        create_skill(
            name="bad-block-skill",
            title="Bad",
            description="desc",
            content="content",
            blocks={"BAD SLOT!": {"default": "x"}},
            skills_repo_dir=isolate_skills_env,
        )
    skill_dir = isolate_skills_env / "skills" / "bad-block-skill"
    assert not (skill_dir / "SKILL.md").exists()
    assert not skill_dir.exists()
