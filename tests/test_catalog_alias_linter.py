"""Hidden compatibility flows do not compete for public search aliases."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.conftest import get_companion_skills_dir


def _linter():
    catalog = get_companion_skills_dir()
    if catalog is None:
        pytest.skip("companion mcp_skills catalog is not checked out")
    spec = importlib.util.spec_from_file_location(
        "catalog_alias_linter", catalog / "scripts/check_aliases.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_flow(path: Path, aliases: list[str], *, hidden_metadata: str = "") -> None:
    alias_lines = "\n".join(f"- {alias}" for alias in aliases)
    path.write_text(
        "---\n"
        + hidden_metadata
        + "description: Этот сценарий проверяет корректную маршрутизацию инженерной задачи "
        "через публичный каталог и предотвращает неоднозначность выбора для пользователя в проекте.\n"
        + f"aliases:\n{alias_lines}\n---\n# Flow\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "hidden_metadata",
    ["visibility: internal\n", "deprecated: true\n", "entrypoint: false\n"],
)
def test_hidden_flow_does_not_duplicate_public_aliases(
    tmp_path: Path, hidden_metadata: str
) -> None:
    linter = _linter()
    flows = tmp_path / "flows"
    flows.mkdir()
    _write_flow(
        flows / "public-flow.md", ["shared phrase", "alpha beta", "gamma delta", "epsilon zeta"]
    )
    legacy = flows / "legacy-flow.md"
    _write_flow(
        legacy,
        ["shared phrase", "one two", "three four", "five six"],
        hidden_metadata=hidden_metadata,
    )

    assert linter.run(tmp_path) == 0
    _write_flow(legacy, ["shared phrase", "one two", "three four", "five six"])
    assert linter.run(tmp_path) == 1
