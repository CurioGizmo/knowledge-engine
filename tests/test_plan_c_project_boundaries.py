"""Пункт C: тесты project_path — самостоятельный подпроект, подпапка без маркера, worktree, symlink."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_core.paths import (
    InvalidProjectPathError,
    resolve_dynamic_project_root,
)


def _repo(root: Path, marker: str = ".git") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / marker).mkdir() if marker == ".git" else (root / marker).write_text(
        "m", encoding="utf-8"
    )
    return root


def test_self_anchored_subproject_accepted(tmp_path: Path) -> None:
    """Самостоятельный подпроект со своим маркером корня (pyproject.toml в подпапке) принимается."""
    monorepo = _repo(tmp_path / "mono")
    sub = monorepo / "packages" / "service"
    sub.mkdir(parents=True)
    (sub / "pyproject.toml").write_text("[project]\nname='svc'\n", encoding="utf-8")

    res = resolve_dynamic_project_root(project_path=str(sub))
    assert res.root == sub.resolve()
    assert res.anchored is True
    assert res.source == f"llm:anchor:{sub.resolve()}"


def test_self_anchored_subproject_dot_agents_marker(tmp_path: Path) -> None:
    """Подпроект с собственным маркером .agents принимается как отдельный корень."""
    monorepo = _repo(tmp_path / "mono2")
    sub = monorepo / "tools" / "cli"
    sub.mkdir(parents=True)
    (sub / ".agents").mkdir()

    res = resolve_dynamic_project_root(project_path=str(sub))
    assert res.root == sub.resolve()
    assert res.anchored is True


def test_plain_subfolder_without_own_marker(tmp_path: Path) -> None:
    """Обычная подпапка закреплённого репо: точный root сохраняется, статус anchored от предка.

    Контракт (mcp_core.paths): walk-up не подменяет root на предка; подкаталог
    без собственного маркера не отклоняется на уровне резолва — rejection
    неоднозначного подкаталога выполняется guard'ом фиксированного режима
    (см. test_fixed_mode_rejects_foreign_root).
    """
    repo = _repo(tmp_path / "plain_repo")
    src_sub = repo / "src" / "deep"
    src_sub.mkdir(parents=True)

    res = resolve_dynamic_project_root(project_path=str(src_sub))
    # Точный путь сохраняется (контракт: walk-up не подменяет root на предка)
    assert res.root == src_sub.resolve()
    # Предок имеет маркер -> путь считается закреплённым (запись разрешена)
    assert res.anchored is True
    assert res.source.startswith("llm:anchor:")


def test_plain_subfolder_write_guard_rejects(tmp_path: Path) -> None:
    """Пишущий guard отклоняет запись в неоднозначную подпапку без маркера с подсказкой ожидаемого корня."""
    from mcp_core.paths import ResolvedRoot, ensure_root_anchored

    repo = _repo(tmp_path / "guard_repo")
    src_sub = repo / "src" / "deep"
    src_sub.mkdir(parents=True)

    res = resolve_dynamic_project_root(project_path=str(src_sub))
    with pytest.raises(Exception) as exc_info:
        ensure_root_anchored(False, res)
    # Подсказка указывает на ожидаемый закреплённый корень
    assert "MCP_PROJECT_ROOT" in str(exc_info.value) or str(repo.resolve()) in str(exc_info.value)


def test_worktree_path_anchored(tmp_path: Path) -> None:
    """Git worktree: .git — файл-указатель на gitdir; путь распознаётся как закреплённый корень."""
    main_repo = _repo(tmp_path / "main_repo")
    gitdir = tmp_path / "main_repo" / ".git"
    (gitdir / "modules").mkdir()
    worktree = tmp_path / "wt_feature"
    worktree.mkdir()
    # .git в worktree — файл со ссылкой на основной gitdir
    (worktree / ".git").write_text(f"gitdir: {gitdir.resolve()}\n", encoding="utf-8")

    res = resolve_dynamic_project_root(project_path=str(worktree))
    assert res.root == worktree.resolve()
    assert res.anchored is True
    _ = main_repo


def test_symlink_to_project_anchored(tmp_path: Path) -> None:
    """Symlink на проект разыменовывается и резолвится в реальный корень."""
    real = _repo(tmp_path / "real_repo")
    link = tmp_path / "link_repo"
    link.symlink_to(real, target_is_directory=True)

    res = resolve_dynamic_project_root(project_path=str(link))
    assert res.root == real.resolve()
    assert res.anchored is True


def test_symlink_subpath_of_project_anchored(tmp_path: Path) -> None:
    """Symlink в подпапку проекта: точный корень сохраняется, статус anchored от реального предка."""
    real = _repo(tmp_path / "real_repo2")
    sub = real / "src"
    sub.mkdir()
    link = tmp_path / "link_src"
    link.symlink_to(sub, target_is_directory=True)

    res = resolve_dynamic_project_root(project_path=str(link))
    assert res.root == sub.resolve()
    assert res.anchored is True


def test_fixed_mode_rejects_foreign_root(tmp_path: Path) -> None:
    """Фиксированный режим отклоняет переключение на чужой проект (не подкаталог того же корня)."""
    from mcp_core.paths import ResolvedRoot

    project = _repo(tmp_path / "fixed_proj")
    other = _repo(tmp_path / "other_proj")

    default_root = ResolvedRoot(root=project.resolve(), source=f"explicit:{project.resolve()}")

    with pytest.raises(InvalidProjectPathError, match="фиксированном режиме"):
        resolve_dynamic_project_root(
            project_path=str(other),
            multi_tenant=False,
            default_root=default_root,
        )
