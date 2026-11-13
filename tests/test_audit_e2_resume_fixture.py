from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _builder() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "docs"
        / "testing"
        / "audit-pilot"
        / "audit-simulation-orchestrator"
        / "tools"
        / "build_e2_resume.py"
    )
    if not path.is_file():
        pytest.skip("audit simulation e2 resume builder tool not present")
    spec = importlib.util.spec_from_file_location("audit_e2_resume_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(fixture: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, *args],
        cwd=fixture,
        check=True,
        capture_output=True,
        text=True,
    )


def _db_state(fixture: Path) -> dict[str, object]:
    result = _run(fixture, "scripts/inspect_db.py", "data/app.db")
    return json.loads(result.stdout)


def test_e2_resume_fixture_has_truthful_pre_and_postconditions(tmp_path: Path) -> None:
    fixture = tmp_path / "e2-resume"
    manifest = _builder().build_fixture(fixture)

    assert manifest["precondition"] == {
        "user_version": 1,
        "indexes": [],
        "item_count": 1,
    }
    assert _db_state(fixture) == manifest["precondition"]

    _run(fixture, "-m", "unittest", "discover", "-s", "tests", "-q")
    _run(fixture, "-m", "migrations.upgrade", "data/app.db")
    first_post = _db_state(fixture)
    _run(fixture, "-m", "migrations.upgrade", "data/app.db")
    second_post = _db_state(fixture)

    assert first_post == {
        "user_version": 2,
        "indexes": ["idx_items_name"],
        "item_count": 1,
    }
    assert second_post == first_post


def test_e2_resume_builder_refuses_nonempty_target(tmp_path: Path) -> None:
    fixture = tmp_path / "e2-resume"
    fixture.mkdir()
    (fixture / "owned-by-user.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _builder().build_fixture(fixture)

    assert (fixture / "owned-by-user.txt").read_text(encoding="utf-8") == "keep"


def test_e2_resume_manifest_matches_file_hashes(tmp_path: Path) -> None:
    fixture = tmp_path / "e2-resume"
    manifest = _builder().build_fixture(fixture)
    saved = json.loads((fixture / "fixture-manifest.json").read_text(encoding="utf-8"))

    assert saved == manifest
    connection = sqlite3.connect(fixture / "data" / "app.db")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        connection.close()
