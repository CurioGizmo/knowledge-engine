"""Тесты многопроектного режима (multi-tenant) и динамического резолва project_path."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from mcp.server.mcpserver.exceptions import ToolError
from mcp_audit.server import create_server as create_audit_server
from mcp_core.paths import (
    BLOCKLIST_ROOTS,
    InvalidProjectPathError,
    SessionProjectMismatchError,
    resolve_dynamic_project_root,
    sanitize_project_path,
)
from mcp_core.service_pool import ProjectServicePool
from mcp_knowledge.server import create_server as create_knowledge_server
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import FlowSession, FlowStatus
from mcp_skills.server import create_server as create_skills_server


# ---------------------------------------------------------------------------
# 1. sanitize_project_path
# ---------------------------------------------------------------------------


def test_sanitize_project_path_quotes_and_whitespace(tmp_path: Path) -> None:
    """Очистка кавычек, пробелов и URI-схем."""
    target = tmp_path / "my_project"
    target.mkdir()

    # Двойные кавычки
    assert sanitize_project_path(f'"{target}"') == target.resolve()
    # Одинарные кавычки
    assert sanitize_project_path(f"'{target}'") == target.resolve()
    # Пробелы
    assert sanitize_project_path(f"  {target}  ") == target.resolve()
    # file:// схема
    assert sanitize_project_path(f"file://{target}") == target.resolve()
    # workspace:// схема
    assert sanitize_project_path(f"workspace://{target}") == target.resolve()


def test_sanitize_project_path_literal_percent(tmp_path: Path) -> None:
    """Обычный путь с литеральными символами %20 не декодируется в пробел."""
    dir_literal = tmp_path / "literal%20name"
    dir_literal.mkdir()
    dir_space = tmp_path / "literal name"
    dir_space.mkdir()

    res = sanitize_project_path(str(dir_literal))
    assert res == dir_literal.resolve()
    assert res != dir_space.resolve()


def test_sanitize_project_path_file_uri_host_validation(tmp_path: Path) -> None:
    """file:// URI с внешним хостом отклоняются, а localhost разрешён."""
    target = tmp_path / "my_project"
    target.mkdir()

    # localhost разрешён
    assert sanitize_project_path(f"file://localhost{target}") == target.resolve()

    # внешний хост отклонён
    with pytest.raises(InvalidProjectPathError, match="внешним хостом не поддерживается"):
        sanitize_project_path(f"file://foreign-host{target}")

    # неподдерживаемая схема отклонёна
    with pytest.raises(InvalidProjectPathError, match="Неподдерживаемая схема URI"):
        sanitize_project_path(f"http://example.com{target}")


def test_sanitize_project_path_invalid_inputs(tmp_path: Path) -> None:
    """Невалидные входные данные вызывают InvalidProjectPathError."""
    # Относительный путь
    with pytest.raises(InvalidProjectPathError, match="абсолютным"):
        sanitize_project_path("relative/path/to/project")

    # Несуществующий путь
    non_existent = tmp_path / "does_not_exist_xyz_123"
    with pytest.raises(InvalidProjectPathError, match="не существует"):
        sanitize_project_path(str(non_existent))

    # Пустой ввод
    with pytest.raises(InvalidProjectPathError, match="пустым"):
        sanitize_project_path("   ")


def test_sanitize_project_path_blocklist() -> None:
    """Пути из блок-листа системных директорий отклоняются."""
    for blocked in (Path("/"), Path("/home"), Path("/tmp"), Path.home()):  # noqa: S108
        if blocked.exists():
            with pytest.raises(InvalidProjectPathError, match="системную директорию"):
                sanitize_project_path(str(blocked))


# ---------------------------------------------------------------------------
# 2. resolve_dynamic_project_root & walk-up
# ---------------------------------------------------------------------------


def test_resolve_dynamic_project_root_walk_up(tmp_path: Path) -> None:
    """Walk-up от вложенной папки сохраняет точный корень, помечая его anchored."""
    repo_root = tmp_path / "my_repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    subfolder = repo_root / "src" / "package" / "sub"
    subfolder.mkdir(parents=True)

    resolved = resolve_dynamic_project_root(project_path=str(subfolder))
    assert resolved.root == subfolder.resolve()
    assert resolved.source == f"llm:anchor:{subfolder.resolve()}"
    assert resolved.anchored is True


def test_resolve_dynamic_project_root_post_walkup_blocklist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если при walk-up найден маркер, но корень попал в блок-лист, поднимается InvalidProjectPathError."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    (fake_home / ".git").mkdir()

    subfolder = fake_home / "some" / "dir"
    subfolder.mkdir(parents=True)

    monkeypatch.setattr("mcp_core.paths.BLOCKLIST_ROOTS", BLOCKLIST_ROOTS | {fake_home.resolve()})

    # 1. Сам корень из блок-листа отсекается с InvalidProjectPathError
    with pytest.raises(InvalidProjectPathError, match="системную директорию или домашнюю папку"):
        resolve_dynamic_project_root(project_path=str(fake_home))

    # 2. Подпапка не падает, а пропускает блок-лист при walk-up и становится unanchored
    res = resolve_dynamic_project_root(project_path=str(subfolder))
    assert res.anchored is False
    assert res.root == subfolder.resolve()


def test_resolve_dynamic_project_root_multi_tenant_fail_fast() -> None:
    """В multi_tenant=True отсутствие project_path вызывает RootNotAnchoredError с инструкцией без кавычек."""
    from mcp_core.paths import RootNotAnchoredError

    with pytest.raises(RootNotAnchoredError) as exc_info:
        resolve_dynamic_project_root(project_path=None, multi_tenant=True)

    msg = str(exc_info.value)
    assert "project_path" in msg
    # Проверяем, что в примере пути нет окружающих кавычек
    assert '"/path/to/project"' not in msg
    assert "/path/to/project" in msg


# ---------------------------------------------------------------------------
# 3. ProjectServicePool
# ---------------------------------------------------------------------------


class MockBundle:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_project_service_pool_thread_safety(tmp_path: Path) -> None:
    """Параллельные запросы к одному проекту инициализируют бандл ровно 1 раз."""
    call_count = 0
    proj = tmp_path / "repo"
    proj.mkdir()

    def factory(p: Path) -> MockBundle:
        nonlocal call_count
        call_count += 1
        return MockBundle(p)

    pool: ProjectServicePool[MockBundle] = ProjectServicePool(factory, max_size=16)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(pool.get, proj) for _ in range(20)]
        results = [f.result() for f in futures]

    assert call_count == 1
    assert all(r is results[0] for r in results)


def test_project_service_pool_lru_eviction(tmp_path: Path) -> None:
    """LRU вытеснение вызывает bundle.close()."""
    created_bundles: list[MockBundle] = []

    def factory(p: Path) -> MockBundle:
        b = MockBundle(p)
        created_bundles.append(b)
        return b

    pool: ProjectServicePool[MockBundle] = ProjectServicePool(factory, max_size=3)

    dirs = [tmp_path / f"proj_{i}" for i in range(5)]
    for d in dirs:
        d.mkdir()

    # Заполняем пул проектами 0, 1, 2
    b0 = pool.get(dirs[0])
    pool.get(dirs[1])
    pool.get(dirs[2])
    assert not b0.closed

    # Добавляем 3 -> b0 должен быть вытеснен и закрыт
    pool.get(dirs[3])
    assert b0.closed is True

    # Пул close_all закрывает оставшиеся
    pool.close_all()
    assert all(b.closed for b in created_bundles)


def test_project_service_pool_leases_prevent_eviction(tmp_path: Path) -> None:
    """Активная аренда (lease) защищает бандл от вытеснения и закрытия."""
    created_bundles: list[MockBundle] = []

    def factory(p: Path) -> MockBundle:
        b = MockBundle(p)
        created_bundles.append(b)
        return b

    pool: ProjectServicePool[MockBundle] = ProjectServicePool(factory, max_size=2)
    d0 = tmp_path / "p0"
    d1 = tmp_path / "p1"
    d2 = tmp_path / "p2"
    for d in (d0, d1, d2):
        d.mkdir()

    # Берем p0 в аренду
    with pool.lease(d0) as b0:
        # Заполняем пул до предела
        b1 = pool.acquire(d1)
        assert len(pool._pool) == 2

        # Добавляем p2 при превышении max_size в аренду
        # Поскольку все бандлы в аренде, пул делает burst сверх max_size (размер 3)
        b2 = pool.acquire(d2)
        assert len(pool._pool) == 3
        assert not b0.closed
        assert not b1.closed
        assert not b2.closed

        # Освобождаем p1
        pool.release(d1)
        # p1 был unleased, теперь размер пула вернулся к 2, p1 вытеснен и закрыт
        assert b1.closed is True
        assert not b0.closed
        assert not b2.closed


# ---------------------------------------------------------------------------
# 4. Multi-tenant Server Fail-Fast across 3 Servers
# ---------------------------------------------------------------------------


async def _extract_content(tool_res: Any) -> dict[str, Any]:
    if getattr(tool_res, "structured_content", None):
        return tool_res.structured_content
    return json.loads(tool_res.content[0].text)


@pytest.mark.asyncio
async def test_knowledge_server_multi_tenant(tmp_path: Path) -> None:
    """Тестирование knowledge-engine в режиме multi_tenant=True."""
    server = create_knowledge_server(multi_tenant=True)

    # 1. tools/list объявляет project_path обязательным
    tools = {t.name: t for t in await server.list_tools()}
    assert "project_path" in tools["search_knowledge"].input_schema.get("required", [])

    # Без project_path -> SDK отклоняет валидацией входной схемы (ToolError)
    with pytest.raises(ToolError, match="project_path"):
        await server.call_tool("search_knowledge", {"query": "test"})

    # 2. Невалидный project_path -> INVALID_PROJECT_PATH
    res = await server.call_tool("search_knowledge", {"query": "test", "project_path": "rel/path"})
    data = await _extract_content(res)
    assert data.get("error") is True
    assert data.get("reason") == "INVALID_PROJECT_PATH"

    # 3. Валидный закреплённый project_path -> Успех
    proj = tmp_path / "knowledge_repo"
    proj.mkdir()
    (proj / ".git").mkdir()

    res = await server.call_tool(
        "record_knowledge",
        {
            "title": "Test Multi-tenant Card",
            "category": "backend",
            "type": "hint",
            "context": "Context for the test multi-tenant card explaining problem in detail",
            "solution": "Working solution with sufficient length to pass the anti spam gate validation",
            "rationale": "Clear rationale explaining why this solution is technically sound",
            "project_path": str(proj),
        },
    )
    data = await _extract_content(res)
    assert data.get("status") == "created" or data.get("id") is not None
    assert (proj / ".agents" / "knowledge").is_dir()


@pytest.mark.asyncio
async def test_audit_server_multi_tenant(tmp_path: Path) -> None:
    """Тестирование audit-engine в режиме multi_tenant=True."""
    server = create_audit_server(multi_tenant=True)

    # 1. tools/list объявляет project_path обязательным
    tools = {t.name: t for t in await server.list_tools()}
    assert "project_path" in tools["search_debt"].input_schema.get("required", [])

    # Без project_path -> SDK отклоняет валидацией входной схемы (ToolError)
    with pytest.raises(ToolError, match="project_path"):
        await server.call_tool("search_debt", {"query": "test"})

    # 2. Валидный закреплённый project_path -> Успех
    proj = tmp_path / "audit_repo"
    proj.mkdir()
    (proj / ".git").mkdir()

    res = await server.call_tool(
        "record_debt",
        {
            "title": "Debt in project",
            "category": "backend",
            "severity": "low",
            "description": "Some debt",
            "project_path": str(proj),
        },
    )
    data = await _extract_content(res)
    assert data.get("status") == "created" or data.get("id") is not None
    assert (proj / ".agents" / "audit").is_dir()


@pytest.mark.asyncio
async def test_skills_server_multi_tenant(tmp_path: Path) -> None:
    """Тестирование skills-engine в режиме multi_tenant=True."""
    server = create_skills_server(multi_tenant=True)

    # 1. tools/list объявляет project_path обязательным
    tools = {t.name: t for t in await server.list_tools()}
    assert "project_path" in tools["search_skills"].input_schema.get("required", [])

    # Без project_path -> SDK отклоняет валидацией входной схемы (ToolError)
    with pytest.raises(ToolError, match="project_path"):
        await server.call_tool("search_skills", {"query": "test"})

    # 2. Валидный закреплённый project_path -> Успех
    proj = tmp_path / "skills_repo"
    proj.mkdir()
    (proj / ".git").mkdir()

    res = await server.call_tool("search_skills", {"query": "", "project_path": str(proj)})
    data = await _extract_content(res)
    assert data.get("mode") == "overview"
    assert "categories" in data


# ---------------------------------------------------------------------------
# 5. Unanchored explicit project_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unanchored_explicit_project_path(tmp_path: Path) -> None:
    """Передача незакреплённого существующего каталога: чтение с warning, мутации отклоняются."""
    unanchored = tmp_path / "unanchored_dir"
    unanchored.mkdir()

    # Knowledge
    k_server = create_knowledge_server(multi_tenant=True)
    res_read = await k_server.call_tool(
        "search_knowledge", {"query": "", "project_path": str(unanchored)}
    )
    data_read = await _extract_content(res_read)
    assert "root_warning" in data_read

    res_write = await k_server.call_tool(
        "record_knowledge",
        {
            "title": "Reject Unanchored",
            "category": "backend",
            "type": "hint",
            "context": "Context for the test multi-tenant card explaining problem in detail",
            "solution": "Working solution with sufficient length to pass the anti spam gate validation",
            "rationale": "Clear rationale explaining why this solution is technically sound",
            "project_path": str(unanchored),
        },
    )
    data_write = await _extract_content(res_write)
    assert data_write.get("reason") == "ROOT_NOT_ANCHORED"

    # Skills step_flow mutate
    s_server = create_skills_server(multi_tenant=True)
    res_flow = await s_server.call_tool(
        "step_flow",
        {
            "action": "advance",
            "flow": "feature-delivery",
            "project_path": str(unanchored),
        },
    )
    data_flow = await _extract_content(res_flow)
    assert data_flow.get("reason") == "ROOT_NOT_ANCHORED"


# ---------------------------------------------------------------------------
# 6. step_flow session project mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_flow_session_project_mismatch(tmp_path: Path) -> None:
    """Вызов step_flow с session_id от другого проекта отклоняется SESSION_PROJECT_MISMATCH."""
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    (proj_a / ".git").mkdir()

    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / ".git").mkdir()

    # Создаём сессию в proj_a через storage proj_a
    flow_storage_a = FlowStorage(project_root=proj_a)
    session = FlowSession(
        id="session-proj-a-123",
        project_root=str(proj_a),
        git_branch="main",
        flow_name="feature-delivery",
        current_stage_id="planning",
        status=FlowStatus.ACTIVE,
        created_at=1000.0,
        updated_at=1000.0,
    )
    flow_storage_a.save_session(session)

    # Записываем ту же сессию в БД proj_b (например, коллизия или перенос)
    flow_storage_b = FlowStorage(project_root=proj_b)
    flow_storage_b.save_session(session)

    s_server = create_skills_server(multi_tenant=True)
    res = await s_server.call_tool(
        "step_flow",
        {
            "action": "status",
            "session_id": "session-proj-a-123",
            "project_path": str(proj_b),
        },
    )
    data = await _extract_content(res)
    assert data.get("error") is True
    assert data.get("reason") == "SESSION_PROJECT_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "advance", "retry", "abort", "escalation_resolve"])
async def test_step_flow_all_actions_session_project_mismatch(tmp_path: Path, action: str) -> None:
    """Все действия step_flow отклоняют чужую сессию с причиной SESSION_PROJECT_MISMATCH."""
    proj_a = tmp_path / "proj_a_param"
    proj_a.mkdir()
    (proj_a / ".git").mkdir()

    proj_b = tmp_path / "proj_b_param"
    proj_b.mkdir()
    (proj_b / ".git").mkdir()

    session = FlowSession(
        id="session-proj-a-999",
        project_root=str(proj_a),
        git_branch="main",
        flow_name="feature-delivery",
        current_stage_id="planning",
        status=FlowStatus.ACTIVE,
        created_at=1000.0,
        updated_at=1000.0,
    )
    FlowStorage(project_root=proj_b).save_session(session)

    server = create_skills_server(multi_tenant=True)
    kwargs: dict[str, Any] = {
        "action": action,
        "session_id": "session-proj-a-999",
        "project_path": str(proj_b),
    }
    if action == "escalation_resolve":
        kwargs["resolution"] = "abort"

    res = await server.call_tool("step_flow", kwargs)
    data = await _extract_content(res)
    assert data.get("error") is True
    assert data.get("reason") == "SESSION_PROJECT_MISMATCH"
    assert data.get("status") == "rejected"


# ---------------------------------------------------------------------------
# 7. Edge Cases: sanitize_project_path
# ---------------------------------------------------------------------------


def test_sanitize_project_path_advanced_edge_cases(tmp_path: Path) -> None:
    """Проверка краевых случаев нормализации путей: пробелы, percent-encoding, null byte."""
    proj_with_spaces = tmp_path / "project with spaces"
    proj_with_spaces.mkdir()

    # Percent-encoded пробел file://
    encoded_uri = f"file://{proj_with_spaces.as_posix()}".replace(" ", "%20")
    assert sanitize_project_path(encoded_uri) == proj_with_spaces.resolve()

    # Табуляции и переводы строк
    assert sanitize_project_path(f"\t\n{proj_with_spaces}\n\t") == proj_with_spaces.resolve()

    # Обратные кавычки Markdown
    assert sanitize_project_path(f"`{proj_with_spaces}`") == proj_with_spaces.resolve()

    # Относительный путь с ../
    with pytest.raises(InvalidProjectPathError, match="абсолютным"):
        sanitize_project_path("../relative/path")

    # Embedded null byte
    with pytest.raises(InvalidProjectPathError):
        sanitize_project_path(f"{proj_with_spaces}\x00evil")

    # Путь к файлу вместо директории
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("hello", encoding="utf-8")
    with pytest.raises(InvalidProjectPathError, match="не является каталогом"):
        sanitize_project_path(str(regular_file))

    # Нестроковый тип
    for invalid_val in (None, 12345, ["/path"]):
        with pytest.raises(InvalidProjectPathError, match="не может быть пустым"):
            sanitize_project_path(invalid_val)  # type: ignore[arg-type]

    # Path traversal в блок-лист
    if Path("/etc").exists():
        with pytest.raises(InvalidProjectPathError, match="системную директорию"):
            sanitize_project_path(f"{tmp_path}/../../../../etc")


# ---------------------------------------------------------------------------
# 8. Edge Cases: Walk-up (Git Submodule, Symlinks, Monorepo, Dotfiles in HOME)
# ---------------------------------------------------------------------------


def test_resolve_dynamic_project_root_git_submodule(tmp_path: Path) -> None:
    """В git submodule .git является текстовым файлом (gitdir), а не директорией."""
    submodule = tmp_path / "submodule_repo"
    submodule.mkdir()
    (submodule / ".git").write_text("gitdir: ../.git/modules/submodule\n", encoding="utf-8")

    code_dir = submodule / "src" / "pkg"
    code_dir.mkdir(parents=True)

    resolved = resolve_dynamic_project_root(project_path=str(code_dir))
    assert resolved.root == code_dir.resolve()
    assert resolved.anchored is True


def test_resolve_dynamic_project_root_symlink(tmp_path: Path) -> None:
    """Символическая ссылка на репозиторий корректно разыменовывается."""
    real_repo = tmp_path / "real_repo"
    real_repo.mkdir()
    (real_repo / ".git").mkdir()

    symlink_repo = tmp_path / "symlink_repo"
    symlink_repo.symlink_to(real_repo)

    sub = real_repo / "src"
    sub.mkdir()

    resolved = resolve_dynamic_project_root(project_path=str(symlink_repo / "src"))
    assert resolved.root == (real_repo / "src").resolve()
    assert resolved.anchored is True


def test_resolve_dynamic_project_root_monorepo_closest_marker(tmp_path: Path) -> None:
    """В monorepo точный корень сохраняется, а проект признаётся anchored."""
    monorepo = tmp_path / "monorepo"
    monorepo.mkdir()
    (monorepo / ".git").mkdir()

    pkg = monorepo / "packages" / "pkg_a"
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("[project]\nname='pkg_a'\n", encoding="utf-8")

    src = pkg / "src"
    src.mkdir()

    resolved = resolve_dynamic_project_root(project_path=str(src))
    assert resolved.root == src.resolve()
    assert resolved.anchored is True


def test_resolve_dynamic_project_root_dotfiles_in_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Наличие .git в BLOCKLIST_ROOTS (например ~/.git) не приводит к падению unanchored проектов."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    (fake_home / ".git").mkdir()  # Имитация dotfile-репозитория в $HOME

    unanchored_sub = fake_home / "work" / "scratch"
    unanchored_sub.mkdir(parents=True)

    monkeypatch.setattr("mcp_core.paths.BLOCKLIST_ROOTS", {fake_home.resolve(), *BLOCKLIST_ROOTS})

    resolved = resolve_dynamic_project_root(project_path=str(unanchored_sub))
    assert resolved.root == unanchored_sub.resolve()
    assert resolved.anchored is False
    assert resolved.source.startswith("llm:explicit-unanchored:")


# ---------------------------------------------------------------------------
# 9. Concurrency & Stress tests for ProjectServicePool
# ---------------------------------------------------------------------------


class MockStressBundle:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_project_service_pool_concurrent_multi_key_stress(tmp_path: Path) -> None:
    """50 параллельных потоков обращаются к 12 разным проектам при max_size=3."""
    dirs = [tmp_path / f"stress_proj_{i}" for i in range(12)]
    for d in dirs:
        d.mkdir()

    pool: ProjectServicePool[MockStressBundle] = ProjectServicePool(MockStressBundle, max_size=3)

    def worker(i: int) -> None:
        d = dirs[i % len(dirs)]
        bundle = pool.get(d)
        assert isinstance(bundle, MockStressBundle)
        with pool._pool_lock:
            assert len(pool._pool) <= 3

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(worker, i) for i in range(50)]
        for f in futures:
            f.result()

    pool.close_all()
    assert len(pool._pool) == 0


def test_project_service_pool_factory_error_lock_cleanup(tmp_path: Path) -> None:
    """Исключение в factory не приводит к утечке ключей в _key_locks."""

    def failing_factory(p: Path) -> MockStressBundle:
        raise RuntimeError("Simulated failure in bundle factory")

    pool: ProjectServicePool[MockStressBundle] = ProjectServicePool(failing_factory, max_size=4)
    proj = tmp_path / "failing_proj"
    proj.mkdir()

    with pytest.raises(RuntimeError, match="Simulated failure"):
        pool.get(proj)

    assert proj.resolve() not in pool._key_locks


# ---------------------------------------------------------------------------
# 10. multi_tenant=False с явным переключением project_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_tenant_false_with_explicit_project_path(tmp_path: Path) -> None:
    """Сервер с multi_tenant=False запрещает переключение на чужой project_path."""
    def_proj = tmp_path / "default_repo"
    def_proj.mkdir()
    (def_proj / ".git").mkdir()

    other_proj = tmp_path / "other_repo"
    other_proj.mkdir()
    (other_proj / ".git").mkdir()

    server = create_knowledge_server(project_root=def_proj, multi_tenant=False)

    # Без параметра -> default_repo
    res1 = await server.call_tool("search_knowledge", {"query": ""})
    data1 = await _extract_content(res1)
    assert data1["project_root"] == str(def_proj.resolve())
    assert "root_warning" not in data1

    # Совпадающий параметр -> default_repo успешно
    res_same = await server.call_tool(
        "search_knowledge", {"query": "", "project_path": str(def_proj)}
    )
    data_same = await _extract_content(res_same)
    assert data_same["project_root"] == str(def_proj.resolve())
    assert "root_warning" not in data_same

    # Чужой параметр -> INVALID_PROJECT_PATH (переключение запрещено)
    res2 = await server.call_tool(
        "search_knowledge", {"query": "", "project_path": str(other_proj)}
    )
    data2 = await _extract_content(res2)
    assert data2["error"] is True
    assert data2["reason"] == "INVALID_PROJECT_PATH"
    assert "Переключение" in data2["message"]


# ---------------------------------------------------------------------------
# 11. Read-only unanchored guarantees: .agents is NOT created on disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unanchored_read_does_not_modify_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Чтение незакреплённого каталога во всех 3 серверах не создаёт .agents/ на диске."""
    unanchored_k = tmp_path / "unanchored_knowledge"
    unanchored_k.mkdir()
    k_server = create_knowledge_server(multi_tenant=True)
    res_k = await k_server.call_tool(
        "search_knowledge", {"query": "", "project_path": str(unanchored_k)}
    )
    data_k = await _extract_content(res_k)
    assert "root_warning" in data_k
    assert not (unanchored_k / ".agents").exists()

    unanchored_a = tmp_path / "unanchored_audit"
    unanchored_a.mkdir()
    a_server = create_audit_server(multi_tenant=True)
    res_a = await a_server.call_tool(
        "search_debt", {"query": "", "project_path": str(unanchored_a)}
    )
    data_a = await _extract_content(res_a)
    assert "root_warning" in data_a
    assert not (unanchored_a / ".agents").exists()

    unanchored_s = tmp_path / "unanchored_skills"
    unanchored_s.mkdir()
    s_server = create_skills_server(multi_tenant=True)
    res_s = await s_server.call_tool(
        "search_skills", {"query": "", "project_path": str(unanchored_s)}
    )
    data_s = await _extract_content(res_s)
    assert "root_warning" in data_s
    assert not (unanchored_s / ".agents").exists()

    # step_flow status на пустой папке возвращает no_active_session и НЕ создаёт .agents/flows.db
    unanchored_flow = tmp_path / "unanchored_flow"
    unanchored_flow.mkdir()
    res_flow = await s_server.call_tool(
        "step_flow", {"action": "status", "project_path": str(unanchored_flow)}
    )
    data_flow = await _extract_content(res_flow)
    assert data_flow["status"] == "no_active_session"
    assert not (unanchored_flow / ".agents").exists()

    # create_skill с невалидным именем отклоняется до создания файлов и НЕ мусорит в cwd
    scratch_cwd = tmp_path / "scratch_cwd"
    scratch_cwd.mkdir()
    monkeypatch.chdir(scratch_cwd)

    res_bad_skill = await s_server.call_tool(
        "create_skill",
        {
            "name": "Invalid_Skill_Name!",
            "title": "Bad",
            "description": "Desc",
            "content": "Content",
        },
    )
    data_bad = await _extract_content(res_bad_skill)
    assert data_bad["error"] is True
    assert not (scratch_cwd / ".agents").exists()
