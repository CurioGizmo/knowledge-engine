"""Тесты для расширений путей mcp_core.paths для модуля skills."""

from pathlib import Path
import pytest

from mcp_core.paths import (
    get_agents_cache_dir,
    get_agents_dir,
    get_skills_cache_dir,
    get_skills_dir,
)


def test_get_skills_dir_default(tmp_path: Path) -> None:
    skills_dir = get_skills_dir(tmp_path)
    assert skills_dir == tmp_path / ".agents" / "skills"


def test_get_agents_dir_default(tmp_path: Path) -> None:
    agents_dir = get_agents_dir(tmp_path)
    assert agents_dir == tmp_path / ".agents" / "agents"


def test_get_skills_cache_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_SKILLS_CACHE_DIR", raising=False)
    expected = Path.home() / ".cache" / "mcp-skills" / "repo"
    assert get_skills_cache_dir() == expected
    assert get_agents_cache_dir() == expected / "agents"


def test_get_skills_cache_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom_dir = tmp_path / "custom_skills_cache"
    monkeypatch.setenv("MCP_SKILLS_CACHE_DIR", str(custom_dir))
    assert get_skills_cache_dir() == custom_dir
    assert get_agents_cache_dir() == custom_dir / "agents"


def test_resolve_skills_repo_dir_priorities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import mcp_core.paths as paths_mod
    from mcp_core.paths import resolve_skills_repo_dir

    paths_mod._WARNED_DEPRECATED_CACHE_ENV = False

    cli_dir = tmp_path / "from_cli"
    env_path = tmp_path / "from_path"
    env_cache = tmp_path / "from_cache"

    # 1. CLI wins over env
    monkeypatch.setenv("MCP_SKILLS_PATH", str(env_path))
    monkeypatch.setenv("MCP_SKILLS_CACHE_DIR", str(env_cache))
    assert resolve_skills_repo_dir(cli_dir) == cli_dir

    # 2. MCP_SKILLS_PATH wins over MCP_SKILLS_CACHE_DIR
    assert resolve_skills_repo_dir(None) == env_path

    # 3. MCP_SKILLS_CACHE_DIR warns once
    monkeypatch.delenv("MCP_SKILLS_PATH")
    with caplog.at_level("WARNING"):
        resolved = resolve_skills_repo_dir(None)
    assert resolved == env_cache
    assert "MCP_SKILLS_CACHE_DIR устарела" in caplog.text
