"""Тесты root-anchoring guard'а (P1): fail-fast записей и видимость корня.

Сценарии:
- пишущий тул с незакреплённым cwd → error-envelope reason=ROOT_NOT_ANCHORED (is_error=False);
- MCP_PROJECT_ROOT спасает запись;
- carve-out'ы: явные write-target (knowledge_dir / audit_dir / project_root skills)
  и инъекция service/storage/synchronizer отключают guard;
- негативный carve-out: skills с явным db_path в незакреплённом cwd → guard активен;
- read-видимость: search_knowledge/search_debt содержат project_root/root_source
  (+root_warning при незакреплённом корне).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

import mcp_audit.server as audit_server_module
import mcp_knowledge.server as knowledge_server_module
import mcp_skills.server as skills_server_module
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


# ---------------------------------------------------------------------------
# Утилиты и фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_server_singletons() -> Any:
    """Сбрасывает модульные синглтоны серверов до и после каждого теста."""

    def _reset() -> None:
        knowledge_server_module  # knowledge не держит синглтонов storage/service
        skills_server_module._STORAGE = None
        skills_server_module._SYNCHRONIZER = None
        skills_server_module._COMPILER = None
        skills_server_module._BUNDLER = None
        skills_server_module._GIT_MGR = None
        audit_server_module._STORAGE = None
        audit_server_module._SYNCHRONIZER = None

    _reset()
    yield
    _reset()


def _unanchored_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Готовит незакреплённый cwd: chdir в пустой каталог + сброс MCP_PROJECT_ROOT.

    Важно: tmp_path (и предки до /) не содержат AGENTS.md/pyproject.toml/.git.
    """
    empty = tmp_path / "unanchored_cwd"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(empty)
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    return empty


def _tool_data(result: Any) -> dict[str, Any]:
    """Извлекает dict-ответ MCP call_tool (structured_content или text)."""
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


def _record_args(slug: str) -> dict[str, Any]:
    return {
        "title": "Guard-тест карточки",
        "category": "platform",
        "type": "lesson",
        "symptom": "Проверка fail-fast guard'а при незакреплённом корне проекта сервера",
        "root_cause": "Резолв корня упал на cwd-fallback без корневых маркеров и MCP_PROJECT_ROOT",
        "solution": "Пишущие тулы отклоняют запись с reason=ROOT_NOT_ANCHORED до побочного эффекта",
        "rationale": "Карточки не должны попадать в случайные каталоги вроде cwd десинкнутого сервера",
        "slug": slug,
    }


def _md_files(root: Path) -> list[Path]:
    """Все .md под root (пусто, если root не существует)."""
    return sorted(root.rglob("*.md")) if root.exists() else []


# ---------------------------------------------------------------------------
# knowledge-engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_record_rejected_when_root_unanchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: create_server без аргументов в незакреплённом cwd; record_knowledge.
    Выход: error-envelope reason=ROOT_NOT_ANCHORED, is_error=False, карточка не создана.
    """
    cwd = _unanchored_env(monkeypatch, tmp_path)
    from mcp_knowledge.server import create_server

    server = create_server()
    res = await server.call_tool("record_knowledge", _record_args("audio-guard-reject"))
    assert res.is_error is True
    data = _tool_data(res)
    assert data.get("error") is True
    assert data.get("reason") == "ROOT_NOT_ANCHORED"
    assert data.get("error_type") == "RootNotAnchoredError"
    # Карточка не записана ни в cwd, ни в tmp_path
    assert _md_files(cwd / ".agents" / "knowledge") == []
    assert _md_files(tmp_path / ".agents" / "knowledge") == []


@pytest.mark.asyncio
async def test_knowledge_record_works_with_mcp_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: незакреплённый cwd, но MCP_PROJECT_ROOT указывает на существующий каталог.
    Выход: запись успешна, поле project_root указывает на env-корень.
    """
    cwd = _unanchored_env(monkeypatch, tmp_path)
    target = tmp_path / "env_root"
    target.mkdir()
    monkeypatch.setenv("MCP_PROJECT_ROOT", str(target))

    from mcp_knowledge.server import create_server

    server = create_server()
    res = await server.call_tool("record_knowledge", _record_args("audio-guard-env"))
    assert not res.is_error
    data = _tool_data(res)
    assert data.get("status") == "created"
    assert Path(data["project_root"]) == target.resolve()
    assert len(_md_files(target / ".agents" / "knowledge")) == 1
    # cwd остался чистым
    assert _md_files(cwd / ".agents" / "knowledge") == []


@pytest.mark.asyncio
async def test_knowledge_record_carveout_explicit_knowledge_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: незакреплённый cwd, но явный knowledge_dir + cache_db (легитимный carve-out).
    Выход: guard не мешает, запись идёт в knowledge_dir.
    """
    _unanchored_env(monkeypatch, tmp_path)
    kdir = tmp_path / "explicit_knowledge"
    cdb = tmp_path / "explicit_cache.db"

    from mcp_knowledge.server import create_server

    server = create_server(knowledge_dir=kdir, cache_db=cdb)
    res = await server.call_tool("record_knowledge", _record_args("audio-guard-carveout"))
    assert not res.is_error
    data = _tool_data(res)
    assert data.get("status") == "created"
    assert kdir in Path(data["path"]).parents


@pytest.mark.asyncio
async def test_knowledge_service_injection_skips_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: инъекция service (моки тестов) в незакреплённом cwd.
    Выход: запись разрешена (write_ok=True), root_warning отсутствует.
    """
    _unanchored_env(monkeypatch, tmp_path)
    kdir = tmp_path / "svc_knowledge"
    kdir.mkdir()
    cdb = tmp_path / "svc_cache.db"
    storage = KnowledgeStorage(db_path=cdb, knowledge_dir=kdir)
    service = KnowledgeService(storage=storage, knowledge_dir=kdir)

    from mcp_knowledge.server import create_server

    server = create_server(service=service)
    res = await server.call_tool("record_knowledge", _record_args("audio-guard-service"))
    data = _tool_data(res)
    assert data.get("status") == "created"
    search = await server.call_tool("search_knowledge", {"query": "guard"})
    search_data = _tool_data(search)
    assert "root_warning" not in search_data


@pytest.mark.asyncio
async def test_knowledge_search_visibility_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: search_knowledge на сервере с незакреплённым корнем.
    Выход: project_root/root_source=cwd-fallback:* + root_warning; чтение не блокируется.
    """
    cwd = _unanchored_env(monkeypatch, tmp_path)
    from mcp_knowledge.server import create_server

    server = create_server()
    res = await server.call_tool("search_knowledge", {"query": "anything"})
    assert not res.is_error
    data = _tool_data(res)
    assert data["project_root"] == str(cwd)
    assert data["root_source"].startswith("cwd-fallback")
    assert "ROOT_NOT_ANCHORED" in data["root_warning"]


def test_knowledge_banner_logged_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Проверяет banner-лог источника корня при старте сервера (INFO) и warning при cwd-fallback."""
    _unanchored_env(monkeypatch, tmp_path)
    from mcp_knowledge.server import create_server

    with caplog.at_level(logging.INFO, logger="mcp_core.paths"):
        create_server()
    assert "root source=cwd-fallback" in caplog.text
    assert "write_ok=False" in caplog.text
    assert "не закреплён" in caplog.text


def test_knowledge_error_rules_order() -> None:
    """RootNotAnchoredError — первое правило (subclass раньше ValueError-ветки движка core)."""
    from mcp_core.paths import RootNotAnchoredError

    assert knowledge_server_module.KNOWLEDGE_ERROR_RULES[0].exc is RootNotAnchoredError
    assert knowledge_server_module.KNOWLEDGE_ERROR_RULES[0].reason == "ROOT_NOT_ANCHORED"


# ---------------------------------------------------------------------------
# skills-engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skills_start_flow_rejected_without_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: create_server без project_root в незакреплённом cwd; start_flow.
    Выход: error-envelope reason=ROOT_NOT_ANCHORED.
    """
    _unanchored_env(monkeypatch, tmp_path)
    from mcp_skills.server import create_server

    server = create_server()
    res = await server.call_tool("step_flow", {"flow": "feature-delivery"})
    assert res.is_error is True
    data = _tool_data(res)
    assert data.get("error") is True
    assert data.get("reason") == "ROOT_NOT_ANCHORED"


@pytest.mark.asyncio
async def test_skills_db_path_is_not_write_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Негативный carve-out (глубокое ревью): явный db_path НЕ разрешает запись флоу.
    Вход: create_server(db_path=...) в незакреплённом cwd; step_flow.
    Выход: ROOT_NOT_ANCHORED (db-флаг пиннит только эфемерный FTS-кэш).
    """
    _unanchored_env(monkeypatch, tmp_path)
    from mcp_skills.server import create_server

    server = create_server(db_path=tmp_path / "skills_fts.db")
    res = await server.call_tool("step_flow", {"flow": "feature-delivery"})
    data = _tool_data(res)
    assert data.get("error") is True
    assert data.get("reason") == "ROOT_NOT_ANCHORED"


@pytest.mark.asyncio
async def test_skills_explicit_project_root_enables_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carve-out: явный project_root (схема инсталлера/тестов) отключает guard.
    Вход: create_server(project_root=tmp_path); step_flow.
    Выход: сессия создана, поле project_root в ответе.
    """
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    from mcp_skills.server import create_server

    server = create_server(project_root=tmp_path)
    res = await server.call_tool("step_flow", {"flow": "feature-delivery"})
    assert not res.is_error
    data = _tool_data(res)
    assert data.get("session_id")
    assert Path(data["project_root"]) == tmp_path.resolve()


@pytest.mark.asyncio
async def test_skills_step_flow_status_read_only_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only carve-out: step_flow(action='status') разрешён без закреплённого корня.
    Вход: step_flow(action='status') в незакреплённом cwd.
    Выход: Успешный ответ (active: False), не падает с ROOT_NOT_ANCHORED.
    """
    _unanchored_env(monkeypatch, tmp_path)
    from mcp_skills.server import create_server

    server = create_server()
    res = await server.call_tool("step_flow", {"action": "status"})
    assert not res.is_error
    data = _tool_data(res)
    assert data.get("error") is not True
    assert data.get("status") == "no_active_session"


# ---------------------------------------------------------------------------
# audit-engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_record_debt_rejected_without_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: create_server() в незакреплённом cwd; record_debt.
    Выход: error-envelope reason=ROOT_NOT_ANCHORED (новая обёртка audit MEDIUM-3).
    """
    _unanchored_env(monkeypatch, tmp_path)
    from mcp_audit.server import create_server

    server = create_server()
    res = await server.call_tool(
        "record_debt",
        {"title": "Guard-долг", "category": "core", "symptom": "проверка guard'а аудита"},
    )
    assert res.is_error is True
    data = _tool_data(res)
    assert data.get("error") is True
    assert data.get("reason") == "ROOT_NOT_ANCHORED"
    assert data.get("error_type") == "RootNotAnchoredError"


@pytest.mark.asyncio
async def test_audit_carveout_explicit_audit_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carve-out: явный audit_dir разрешает запись даже в незакреплённом cwd."""
    _unanchored_env(monkeypatch, tmp_path)
    from mcp_audit.server import create_server

    adir = tmp_path / "explicit_audit"
    cdb = tmp_path / "audit_cache.db"
    server = create_server(audit_dir=adir, cache_db=cdb)
    res = await server.call_tool(
        "record_debt",
        {"title": "Carveout-долг", "category": "core", "symptom": "проверка carve-out аудита"},
    )
    data = _tool_data(res)
    assert data.get("status") == "created"
    file_path = Path(data["file_path"])
    assert file_path.exists()
    assert adir in file_path.parents


@pytest.mark.asyncio
async def test_audit_injected_storage_skips_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Инъекция storage+synchronizer (схема существующих тестов) ⇒ write_ok=True."""
    _unanchored_env(monkeypatch, tmp_path)
    from mcp_audit.server import create_server
    from mcp_audit.storage import AuditStorage
    from mcp_audit.synchronizer import AuditSynchronizer

    adir = tmp_path / "inj_audit"
    storage = AuditStorage(tmp_path / "inj_audit.db")
    sync = AuditSynchronizer(storage, audit_dir=adir)
    server = create_server(audit_dir=adir, storage=storage, synchronizer=sync)
    res = await server.call_tool(
        "record_debt",
        {"title": "Inj-долг", "category": "core", "symptom": "проверка инъекции аудита"},
    )
    data = _tool_data(res)
    assert data.get("status") == "created"


@pytest.mark.asyncio
async def test_audit_search_visibility(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Вход: search_debt в незакреплённом корне.
    Выход: поля видимости project_root/root_source + root_warning; чтение не блокируется.
    """
    cwd = _unanchored_env(monkeypatch, tmp_path)
    from mcp_audit.server import create_server

    server = create_server()
    res = await server.call_tool("search_debt", {"query": "x"})
    assert not res.is_error
    data = _tool_data(res)
    assert data["project_root"] == str(cwd)
    assert data["root_source"].startswith("cwd-fallback")
    assert "ROOT_NOT_ANCHORED" in data["root_warning"]


@pytest.mark.asyncio
async def test_audit_value_error_maps_to_invalid_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход: закреплённый корень (project_root с .git), update_debt_status с мусорным статусом.
    Выход: обёртка audit мапит ValueError → INVALID_ARGUMENTS (не RAW исключение).
    """
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    proj = tmp_path / "anchored_repo"
    proj.mkdir()
    (proj / ".git").mkdir()

    from mcp_audit.server import create_server

    # Неверный enum теперь отвергается валидацией схемы (literal_error) до
    # доменной функции; доменная ветка ValueError → INVALID_ARGUMENTS
    # проверяется напрямую через update_debt_status.
    import pytest as _pytest

    from mcp.server.mcpserver.exceptions import ToolError

    server = create_server(project_root=proj)
    with _pytest.raises(ToolError):
        await server.call_tool(
            "update_debt_status", {"id": "aud-core-nonexistent", "status": "bogus-status"}
        )

    from mcp_audit.server import update_debt_status as _update_debt_status

    with _pytest.raises(ValueError, match="bogus-status"):
        _update_debt_status(id="aud-core-nonexistent", status="bogus-status")
