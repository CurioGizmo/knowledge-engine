"""Unit-тесты для модуля mcp_core.paths: разрешение корня проекта, MCP_PROJECT_ROOT, fallback."""

import logging
from pathlib import Path
import pytest

from mcp_core.paths import (
    ResolvedRoot,
    RootNotAnchoredError,
    _find_default_project_root,
    ensure_root_anchored,
    get_cache_dir,
    get_skills_cache_dir,
    get_skills_dir,
    is_anchored,
    is_stale_tmp_root,
    log_root_banner,
    resolve_factory_root,
    resolve_project_root,
)


def test_find_project_root_via_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Проверяет приоритет переменной окружения MCP_PROJECT_ROOT."""
    custom_root = tmp_path / "custom_project"
    custom_root.mkdir()
    monkeypatch.setenv("MCP_PROJECT_ROOT", str(custom_root))

    resolved = _find_default_project_root()
    assert resolved == custom_root.resolve()


def test_find_project_root_invalid_env_var_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Проверяет, что невалидный MCP_PROJECT_ROOT логирует warning и откатывается к walk-up."""
    invalid_path = tmp_path / "non_existent_dir_12345"
    monkeypatch.setenv("MCP_PROJECT_ROOT", str(invalid_path))

    # Создаём проект с .git в tmp_path
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    (git_dir / ".git").mkdir()
    nested_dir = git_dir / "src" / "pkg"
    nested_dir.mkdir(parents=True)

    with caplog.at_level(logging.WARNING):
        resolved = _find_default_project_root(start_path=nested_dir)

    assert resolved == git_dir
    assert "MCP_PROJECT_ROOT=" in caplog.text
    assert "does not exist or is not a directory" in caplog.text


def test_find_project_root_walk_up_anchors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяет обнаружение корня по корневого маркерам .git, pyproject.toml, AGENTS.md."""
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)

    for anchor in [".git", "pyproject.toml", "AGENTS.md"]:
        clean_name = anchor.replace(".", "_")
        proj_dir = tmp_path / f"proj_{clean_name}"
        proj_dir.mkdir()
        if anchor == ".git":
            (proj_dir / anchor).mkdir()
        else:
            (proj_dir / anchor).write_text("test")

        sub_dir = proj_dir / "a" / "b" / "c"
        sub_dir.mkdir(parents=True)

        assert _find_default_project_root(start_path=sub_dir) == proj_dir


def test_find_project_root_fallback_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяет fallback к cwd при отсутствии корневых маркеров."""
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()

    resolved = _find_default_project_root(start_path=empty_dir)
    assert resolved == empty_dir


def test_resolve_project_root_source_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяет source='env' при валидной MCP_PROJECT_ROOT."""
    monkeypatch.setenv("MCP_PROJECT_ROOT", str(tmp_path))
    res = resolve_project_root(start_path=tmp_path)
    assert res.root == tmp_path.resolve()
    assert res.source == "env"
    assert res.anchored is True


def test_resolve_project_root_source_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверяет source='anchor:<путь>' при walk-up до корневого маркера."""
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / ".git").mkdir()
    nested = proj / "a" / "b"
    nested.mkdir(parents=True)
    res = resolve_project_root(start_path=nested)
    assert res.root == proj
    assert res.source.startswith("anchor:")
    assert res.anchored is True


def test_resolve_project_root_cwd_fallback_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Проверяет source='cwd-fallback' и warning-лог без корневых маркеров (нестрогий режим не падает)."""
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    empty = tmp_path / "no_anchor"
    empty.mkdir()
    with caplog.at_level(logging.WARNING):
        res = resolve_project_root(start_path=empty)
    assert res.root == empty
    assert res.source.startswith("cwd-fallback:")
    assert res.anchored is False
    assert "корень проекта не закреплён" in caplog.text.lower()


def test_resolve_project_root_strict_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверяет, что strict=True поднимает RootNotAnchoredError при cwd-fallback."""
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    empty = tmp_path / "no_anchor"
    empty.mkdir()
    with pytest.raises(RootNotAnchoredError):
        resolve_project_root(strict=True, start_path=empty)
    # env спасает
    monkeypatch.setenv("MCP_PROJECT_ROOT", str(empty))
    assert resolve_project_root(strict=True, start_path=empty).root == empty


def test_ensure_root_anchored_pass_and_raise() -> None:
    """Проверяет guard: write_ok=True проходит, иначе raise RootNotAnchoredError."""
    res = ResolvedRoot(root=Path("/tmp"), source="cwd-fallback:/tmp")  # noqa: S108 — тест эвристики /tmp-корня
    ensure_root_anchored(True, res)  # не должен падать
    with pytest.raises(RootNotAnchoredError):
        ensure_root_anchored(False, res)
    # без resolved тоже raise
    with pytest.raises(RootNotAnchoredError):
        ensure_root_anchored(False)


def test_is_anchored(tmp_path: Path) -> None:
    """Проверяет is_anchored: корневой маркер в самом каталоге или в предках."""
    empty = tmp_path / "empty"
    empty.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    nested = proj / "x" / "y"
    nested.mkdir(parents=True)
    assert is_anchored(empty) is False
    assert is_anchored(proj) is True
    assert is_anchored(nested) is True


def test_is_stale_tmp_root() -> None:
    """Проверяет эвристику /tmp-корня (tmp_path сам под /tmp, поэтому негатив — корень репо)."""
    repo_root = Path(__file__).resolve().parents[1]
    assert is_stale_tmp_root(Path("/tmp")) is True  # noqa: S108
    assert is_stale_tmp_root(Path("/tmp/pytest-of-user/pytest-1/x")) is True  # noqa: S108
    assert is_stale_tmp_root(Path("/opt/my-project")) is False
    if Path("/tmp") not in repo_root.parents and repo_root != Path("/tmp"):  # noqa: S108
        assert is_stale_tmp_root(repo_root) is False


def test_resolve_factory_root_explicit(tmp_path: Path) -> None:
    """Проверяет resolve_factory_root: явный закреплённый vs незакреплённый root."""
    anchored = tmp_path / "repo"
    anchored.mkdir()
    (anchored / "AGENTS.md").write_text("x")
    res = resolve_factory_root(anchored)
    assert res.root == anchored.resolve()
    assert res.source.startswith("explicit:")
    assert res.anchored is True

    unanchored = tmp_path / "plain"
    unanchored.mkdir()
    res2 = resolve_factory_root(unanchored)
    assert res2.root == unanchored.resolve()
    assert res2.source.startswith("explicit-unanchored:")
    assert res2.anchored is False


def test_log_root_banner_logs_source(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    """Проверяет, что log_root_banner пишет INFO banner с source и write_ok."""
    res = ResolvedRoot(root=tmp_path, source="env")
    with caplog.at_level(logging.INFO):
        log_root_banner("knowledge-engine", res, True)
    assert "knowledge-engine" in caplog.text
    assert "root source=env" in caplog.text
    assert "write_ok=True" in caplog.text


def test_get_cache_and_skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяет get_cache_dir и get_skills_dir с явным project_root и по умолчанию."""
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)
    # Явный аргумент
    assert get_cache_dir(tmp_path) == tmp_path / ".agents" / ".cache"
    assert get_skills_dir(tmp_path) == tmp_path / ".agents" / "skills"

    # Через env
    monkeypatch.setenv("MCP_PROJECT_ROOT", str(tmp_path))
    assert get_cache_dir() == tmp_path.resolve() / ".agents" / ".cache"
    assert get_skills_dir() == tmp_path.resolve() / ".agents" / "skills"


def test_server_resolves_project_root_when_cwd_is_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверяет, что сервер, запущенный с cwd=$HOME, находит корень проекта через MCP_PROJECT_ROOT или явный project_root."""
    home_dir = tmp_path / "home_user"
    home_dir.mkdir()
    project_dir = tmp_path / "work" / "my_project"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    # Симулируем запуск из $HOME
    monkeypatch.chdir(home_dir)
    monkeypatch.delenv("MCP_PROJECT_ROOT", raising=False)

    # Без env и без корневых маркеров в $HOME корень падает в $HOME
    assert _find_default_project_root() == home_dir

    # С MCP_PROJECT_ROOT корень указывает на проект
    monkeypatch.setenv("MCP_PROJECT_ROOT", str(project_dir))
    assert _find_default_project_root() == project_dir.resolve()
    assert get_cache_dir() == project_dir.resolve() / ".agents" / ".cache"
