"""Тесты для CardRepository (слой доступа к Markdown-файлам)."""

from pathlib import Path

import pytest
from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    GeneratedBy,
    GeneratedMetadata,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import serialize_card_markdown
from mcp_knowledge.repository import CardNotFoundError, CardRepository, _atomic_write_text


def test_card_repository_path_traversal_protection(tmp_path: Path):
    """Проверка защиты от Path Traversal в CardRepository.find_card_file."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir(parents=True)
    repo = CardRepository(knowledge_dir=k_dir)

    outside_file = tmp_path / "secret.md"
    outside_file.write_text("secret content", encoding="utf-8")

    assert repo.find_card_file("..-secret") is None
    assert repo.find_card_file("../secret") is None
    assert repo.find_card_file("../../secret") is None
    assert repo.find_card_file("/etc/passwd") is None
    assert repo.find_card_file("invalid/slug") is None
    assert repo.find_card_file("") is None


def test_card_repository_read_write_delete(tmp_path: Path):
    """Проверка создания, чтения, удаления и листинга карточек через CardRepository."""
    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    fm = CardFrontmatter(
        id="backend-auth-jwt",
        title="Auth JWT",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.HUMAN, created_at="2026-09-29"),
    )
    sections = {
        "Контекст (Context)": "Контекст задачи",
        "Решение (Solution)": "Использовать JWT токены",
        "Обоснование (Rationale)": "Быстро и удобно",
    }

    # Запись
    written_path = repo.write_card("backend", "backend-auth-jwt", fm, sections)
    assert written_path.exists()
    assert repo.get_existing_ids() == {"backend-auth-jwt"}

    # Чтение
    read_fm, read_sections = repo.read_card("backend-auth-jwt")
    assert read_fm.id == fm.id
    assert read_sections["Решение (Solution)"] == "Использовать JWT токены"

    # Stat
    stats = repo.stat_cards()
    assert len(stats) == 1
    assert stats[0][0].resolve() == written_path.resolve()

    # Удаление
    assert repo.delete_card("backend-auth-jwt") is True
    assert repo.delete_card("backend-auth-jwt") is False
    assert repo.get_existing_ids() == set()

    with pytest.raises(CardNotFoundError):
        repo.read_card("backend-auth-jwt")


def test_card_repository_index_lookup_callback(tmp_path: Path):
    """Проверка работы колбэка index_lookup (стадия 2 поиска)."""
    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    deep_path = k_dir / "platform" / "nested" / "deep" / "platform-deep-id.md"
    req = CardRecordRequest(
        title="Deep Card",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.LESSON,
        symptom="Тестовый симптом глубокой карточки",
        root_cause="Первопричина глубокой карточки длиной более 40 символов",
        solution="Решение проблемы глубокой карточки длиной более 50 символов",
        rationale="Обоснование решения глубокой карточки более 30 символов",
        slug="platform-deep-id",
    )
    _atomic_write_text(deep_path, serialize_card_markdown("platform-deep-id", req))

    # С установленным колбэком
    def mock_lookup(card_id: str) -> Path | None:
        if card_id == "platform-deep-id":
            return deep_path
        return None

    repo.index_lookup = mock_lookup
    found = repo.find_card_file("platform-deep-id")
    assert found is not None
    assert found.resolve() == deep_path.resolve()


def test_read_card_file_parses_by_exact_path(tmp_path: Path):
    """read_card_file читает и парсит карточку по точному пути (глубокая вложенность)."""
    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    fm = CardFrontmatter(
        id="backend-deep-card",
        title="Deep Card",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.HUMAN, created_at="2026-09-29"),
    )
    sections = {"Решение (Solution)": "Точное чтение по пути"}
    written = repo.write_card("backend", "backend-deep-card", fm, sections)

    deep_file = k_dir / "backend" / "nested" / "deep" / "backend-deep-card.md"
    deep_file.parent.mkdir(parents=True, exist_ok=True)
    deep_file.write_text(written.read_text(encoding="utf-8"), encoding="utf-8")
    written.unlink()

    read_fm, read_sections = repo.read_card_file(deep_file)
    assert read_fm.id == "backend-deep-card"
    assert read_sections["Решение (Solution)"] == "Точное чтение по пути"


def test_read_card_file_oserror_to_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """OSError при чтении файла маппится в StorageUnavailableError."""
    from mcp_knowledge.errors import StorageUnavailableError

    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)
    card_file = k_dir / "backend" / "missing-card.md"

    def _fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "read_text", _fail_read_text)

    with pytest.raises(StorageUnavailableError):
        repo.read_card_file(card_file)


def test_read_card_delegates_to_read_card_file(tmp_path: Path):
    """read_card по ID эквивалентен read_card_file по найденному пути."""
    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    fm = CardFrontmatter(
        id="backend-delegate",
        title="Delegate Card",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.HUMAN, created_at="2026-09-29"),
    )
    sections = {"Решение (Solution)": "Делегирование чтения"}
    repo.write_card("backend", "backend-delegate", fm, sections)

    via_id = repo.read_card("backend-delegate")
    path = repo.find_card_file("backend-delegate")
    assert path is not None
    via_path = repo.read_card_file(path)
    assert via_id[0].id == via_path[0].id == "backend-delegate"
    assert via_id[1] == via_path[1]


def test_save_card_writes_exact_bytes(tmp_path: Path):
    """save_card перезаписывает файл in-place байт-в-байт как serialize_card_from_parts."""
    from mcp_knowledge.parser import serialize_card_from_parts

    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    fm = CardFrontmatter(
        id="backend-save-card",
        title="Save Card",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.HUMAN, created_at="2026-09-29"),
    )
    sections = {"Решение (Solution)": "Перезапись по точному пути"}
    written = repo.write_card("backend", "backend-save-card", fm, sections)

    fm.title = "Save Card v2"
    sections["Решение (Solution)"] = "Обновлённое решение по точному пути"
    result = repo.save_card(written, fm, sections)
    assert result == written

    expected = serialize_card_from_parts(fm, sections)
    assert written.read_text(encoding="utf-8") == expected


def test_save_card_oserror_to_storage_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """OSError при записи маппится в StorageUnavailableError (D5)."""
    import mcp_knowledge.repository as repo_module
    from mcp_knowledge.errors import StorageUnavailableError

    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    fm = CardFrontmatter(
        id="backend-save-fail",
        title="Save Fail",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.HUMAN, created_at="2026-09-29"),
    )
    card_file = k_dir / "backend" / "backend-save-fail.md"

    def _fail_write(path: Path, content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repo_module, "_atomic_write_text", _fail_write)

    with pytest.raises(StorageUnavailableError):
        repo.save_card(card_file, fm, {"Решение (Solution)": "x"})


def test_write_new_card_canonical_path_and_content(tmp_path: Path):
    """write_new_card пишет контент дословно по каноническому пути категории; каталог создаётся записью."""
    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    content = "---\nid: general-new-card\n---\n\n### Секция\n\nТело карточки"
    result = repo.write_new_card("general", "general-new-card", content)
    assert result == k_dir / "general" / "general-new-card.md"
    assert result.exists()
    assert result.read_text(encoding="utf-8") == content


def test_write_new_card_oserror_to_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """OSError при записи новой карточки маппится в StorageUnavailableError (D5)."""
    import mcp_knowledge.repository as repo_module
    from mcp_knowledge.errors import StorageUnavailableError

    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    def _fail_write(path: Path, content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repo_module, "_atomic_write_text", _fail_write)

    with pytest.raises(StorageUnavailableError):
        repo.write_new_card("general", "general-fail-card", "content")


def test_card_lock_serializes_concurrent_rmw(tmp_path: Path) -> None:
    """K4: card_lock сериализует конкурентные RMW-циклы (контроль на POSIX)."""
    import threading

    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)
    marker = k_dir / "counter.txt"
    marker.write_text("0")

    def _rmw_increment() -> None:
        for _ in range(20):
            with repo.card_lock("counter-card"):
                cur = int(marker.read_text()) if marker.exists() else 0
                marker.write_text(str(cur + 1))

    threads = [threading.Thread(target=_rmw_increment) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert marker.read_text() == "160", "card_lock не сериализовал конкурентные RMW"


def test_card_lock_degrades_to_noop_without_fcntl_and_msvcrt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью r2: card_lock без fcntl деградирует в msvcrt-лок, а не в no-op.

    На POSIX msvcrt недоступен — оба резервных пути проверяются так:
    (a) msvcrt-заглушка: лок реально захватывается (вызовы locking видны);
    (b) ни fcntl, ни msvcrt — задокументированный no-op без исключений.
    """
    import threading
    from mcp_knowledge import repository as repo_mod

    k_dir = tmp_path / "knowledge"
    repo = CardRepository(knowledge_dir=k_dir)

    # (a) Заглушка msvcrt: track-вызовы locking/unlocking
    calls: list[tuple[str, int]] = []
    lock_state = threading.Lock()

    class _FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def locking(self, fd, mode, nbytes):  # noqa: ANN001
            calls.append(("lock" if mode == 1 else "unlock", fd))
            if mode == 1:
                lock_state.acquire()
            else:
                lock_state.release()

    monkeypatch.setattr(repo_mod, "fcntl", None)
    monkeypatch.setattr(repo_mod, "_import_msvcrt", lambda: _FakeMsvcrt())

    marker = k_dir / "counter.txt"
    marker.write_text("0")

    def _rmw_increment() -> None:
        for _ in range(20):
            with repo.card_lock("counter-card"):
                cur = int(marker.read_text()) if marker.exists() else 0
                marker.write_text(str(cur + 1))

    threads = [threading.Thread(target=_rmw_increment) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert marker.read_text() == "160", "msvcrt-ветка card_lock не сериализовала RMW"
    assert calls, "msvcrt.locking не вызывался: card_lock остался no-op"
    # Все локи корректно сняты
    assert lock_state.acquire(blocking=False), "после выхода локов остался захваченным"
    lock_state.release()

    # (b) Ни fcntl, ни msvcrt — no-op без исключений
    monkeypatch.setattr(repo_mod, "_import_msvcrt", lambda: None)
    with repo.card_lock("any-card"):
        pass
