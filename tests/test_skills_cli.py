"""Тесты для CLI утилиты mcp_skills."""

from pathlib import Path
import pytest

from mcp_skills.cli import main


def test_cli_categories_command(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["categories"])
    assert code == 0
    captured = capsys.readouterr()
    assert "planning" in captured.out
    assert "audio-ml" in captured.out


def test_cli_search_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mcp_skills.server as server_module

    server_module._STORAGE = None
    server_module._SYNCHRONIZER = None

    repo_dir = tmp_path / "skills-repo"
    skills_dir = repo_dir / "skills" / "writing-plans"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        """---
name: writing-plans
title: Writing Plans
category: planning
---
# Content
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("MCP_SKILLS_PATH", str(repo_dir))
    monkeypatch.chdir(tmp_path)

    code = main(["search", "writing-plans"])
    assert code == 0
    captured = capsys.readouterr()
    assert "writing-plans" in captured.out

    # Zero-query (Обзор) не должен падать с KeyError('count')
    code_overview = main(["search"])
    assert code_overview == 0
    captured_overview = capsys.readouterr()
    assert "Обзор" in captured_overview.out

    # Поиск с явным типом flow
    code_flow = main(["search", "--type", "flow"])
    assert code_flow == 0
    captured_flow = capsys.readouterr()
    assert "Найдено сценариев" in captured_flow.out


def test_cli_agent_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mcp_skills.server as server_module

    server_module._STORAGE = None
    server_module._SYNCHRONIZER = None

    repo_dir = tmp_path / "skills-repo"
    skills_dir = repo_dir / "skills" / "writing-plans"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        """---
name: writing-plans
title: Writing Plans
category: planning
---
# Writing Plans
Rules.
""",
        encoding="utf-8",
    )

    # R2 ревизия 4: read-only профиль из скилла (без AGENT.yaml) отклоняется
    # при экспорте в OpenCode — создаём профиль с подтверждением.
    agents_dir = repo_dir / "agents" / "writing-plans"
    agents_dir.mkdir(parents=True)
    (agents_dir / "AGENT.yaml").write_text(
        """name: writing-plans
role: Writing Plans Specialist
description: Plans agent
permissions:
  write: false
  mcp: true
  subagents: false
readonly_acknowledged: true
skills: [writing-plans]
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("MCP_SKILLS_PATH", str(repo_dir))
    monkeypatch.chdir(tmp_path)

    # agent load
    code_agent = main(["agent", "writing-plans", "--target", "prompt"])
    assert code_agent == 0
    captured_agent = capsys.readouterr()
    assert "Persona: writing-plans" in captured_agent.out

    # agent install
    code_install = main(["agent", "writing-plans", "--target", "opencode", "--install"])
    assert code_install == 0
    captured_install = capsys.readouterr()
    assert "установлен" in captured_install.out


def test_cli_load_and_search_new_terminology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mcp_skills.server as server_module

    server_module._STORAGE = None
    server_module._SYNCHRONIZER = None

    repo_dir = tmp_path / "skills-repo"
    skills_dir = repo_dir / "skills" / "writing-plans"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        """---
name: writing-plans
title: Writing Plans
category: planning
available_slots: [backend]
---
# Writing Plans
<!-- SLOT: backend -->
SlotContent
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("MCP_SKILLS_PATH", str(repo_dir))
    monkeypatch.chdir(tmp_path)

    # 1. Search with --category
    code_cat = main(["search", "writing", "--category", "planning"])
    assert code_cat == 0
    captured_cat = capsys.readouterr()
    assert "writing-plans" in captured_cat.out

    # 2. Load with --stack
    code_load = main(["load", "writing-plans", "--stack", "backend"])
    assert code_load == 0
    captured_load = capsys.readouterr()
    assert "Writing Plans" in captured_load.out
