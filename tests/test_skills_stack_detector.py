"""Тесты для tomllib-парсинга pyproject.toml в детекторе стека."""

from pathlib import Path

import pytest

from mcp_skills.stack_detector import (
    _extract_toml_dependencies,
    _normalize_dep_name,
    detect_mode,
    detect_project_stack,
)


def test_normalize_dep_name_pep508() -> None:
    assert _normalize_dep_name("torch>=2.0.0") == "torch"
    assert _normalize_dep_name("Pydantic") == "pydantic"
    assert _normalize_dep_name("fastapi[all]") == "fastapi"
    assert _normalize_dep_name("sqlalchemy~=2.0") == "sqlalchemy"
    assert _normalize_dep_name("pytest") == "pytest"
    assert _normalize_dep_name("django != 5.0") == "django"


def test_extract_toml_dependencies_pep621() -> None:
    data = {
        "project": {
            "dependencies": ["fastapi>=0.100.0", "pydantic"],
            "optional-dependencies": {"dev": ["pytest"]},
        }
    }
    deps = _extract_toml_dependencies(data)
    assert "fastapi>=0.100.0" in deps
    assert "pydantic" in deps
    assert "pytest" in deps


def test_extract_toml_dependencies_poetry() -> None:
    data = {
        "tool": {
            "poetry": {
                "dependencies": {"flask": "^3.0"},
                "dev-dependencies": {"pytest": "^8.0"},
            }
        }
    }
    deps = _extract_toml_dependencies(data)
    assert "flask" in deps
    assert "pytest" in deps


def test_stack_detector_pep621_fastapi(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "my-app"
dependencies = [
    "fastapi>=0.100.0",
    "torch>=2.0.0",
    "pydantic"
]
""",
        encoding="utf-8",
    )
    stack = detect_project_stack(tmp_path)
    assert "python" in stack
    assert "fastapi" in stack
    assert "pytorch" in stack
    assert "pydantic" in stack


def test_stack_detector_poetry_django(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[tool.poetry]
name = "legacy-app"

[tool.poetry.dependencies]
python = "^3.11"
django = "^5.0"
""",
        encoding="utf-8",
    )
    stack = detect_project_stack(tmp_path)
    assert "python" in stack
    assert "django" in stack


def test_stack_detector_broken_toml_fallback(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\ndependencies = broken <<< fastapi >>",
        encoding="utf-8",
    )
    stack = detect_project_stack(tmp_path)
    # Фолбэк на substring-скан: python добавлен, fastapi найден substring'ом
    assert "python" in stack
    assert "fastapi" in stack


def test_extract_repo_name_from_git_url() -> None:
    from mcp_skills.stack_detector import _extract_repo_name_from_git_url

    assert (
        _extract_repo_name_from_git_url("git@github.com:CurioGizmo/knowledge-engine.git")
        == "knowledge-engine"
    )
    assert _extract_repo_name_from_git_url("https://github.com/org/repo-name.git") == "repo-name"
    assert _extract_repo_name_from_git_url("https://github.com/org/repo-name") == "repo-name"
    assert (
        _extract_repo_name_from_git_url("ssh://git@host:2222/group/subgroup/my-app.git") == "my-app"
    )
    assert _extract_repo_name_from_git_url("") is None


def test_detect_canonical_project_name_manifests(tmp_path: Path) -> None:
    from mcp_skills.stack_detector import detect_canonical_project_name

    # 1. Fallback to folder name
    assert detect_canonical_project_name(tmp_path) == tmp_path.name.lower()

    # 2. pyproject.toml
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "cool-py-project"\n', encoding="utf-8"
    )
    assert detect_canonical_project_name(tmp_path) == "cool-py-project"

    # 3. package.json in another dir
    node_dir = tmp_path / "node_proj"
    node_dir.mkdir()
    (node_dir / "package.json").write_text('{"name": "@my-org/web-service"}', encoding="utf-8")
    assert detect_canonical_project_name(node_dir) == "web-service"


# ---------- detect_mode: режим работы (env + файловые маркеры) ----------


def test_detect_mode_env_non_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP_ACTIVE_SERVERS задан и непуст → 'mcp' (приоритет над маркерами)."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine,audit")
    (tmp_path / ".agents" / "audit").mkdir(parents=True)  # маркер, но env сильнее
    assert detect_mode(tmp_path) == "mcp"


def test_detect_mode_env_empty_means_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP_ACTIVE_SERVERS задан и явно пуст → 'offline' (даже при наличии маркеров)."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "")
    (tmp_path / ".agents" / "audit").mkdir(parents=True)
    assert detect_mode(tmp_path) == "offline"


def test_detect_mode_env_absent_no_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """env отсутствует, маркеров нет → None."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    assert detect_mode(tmp_path) is None


def test_detect_mode_marker_audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """.agents/audit/ без MCP_ACTIVE_SERVERS — данные, не признак сервера → None."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    (tmp_path / ".agents" / "audit").mkdir(parents=True)
    assert detect_mode(tmp_path) is None


def test_detect_mode_marker_knowledge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """.agents/knowledge/ без MCP_ACTIVE_SERVERS → None."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    (tmp_path / ".agents" / "knowledge").mkdir(parents=True)
    assert detect_mode(tmp_path) is None


def test_detect_mode_marker_knowledge_fts_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.agents/.cache/knowledge_fts.db без MCP_ACTIVE_SERVERS → None."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    fts_db = tmp_path / ".agents" / ".cache" / "knowledge_fts.db"
    fts_db.parent.mkdir(parents=True)
    fts_db.write_bytes(b"")
    assert detect_mode(tmp_path) is None


def test_detect_mode_knowledge_db_not_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.agents/knowledge.db — НЕ маркер (артефакт бенчмарк-скрипта, а не движка)."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "knowledge.db").write_bytes(b"")
    assert detect_mode(tmp_path) is None


def test_detect_mode_env_priority_over_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env-значение любого уровня перекрывает файловые маркеры (явный offline)."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "   ")  # whitespace-only → пуст → offline
    fts_db = tmp_path / ".agents" / ".cache" / "knowledge_fts.db"
    fts_db.parent.mkdir(parents=True)
    fts_db.write_bytes(b"")
    assert detect_mode(tmp_path) == "offline"


# ---------- A5: package.json с "dependencies": null (план review-fixes, verdict_4 §3) ----------


def test_stack_detector_node_dependencies_null(tmp_path: Path) -> None:
    """ "dependencies": null не должен ронять детектор TypeError'ом (verdict_4 §3).

    Воспроизведение дефекта: ``dict(**None)`` -> ``TypeError: 'NoneType' object
    is not a mapping``, исключение не входит в ``except (OSError, ValueError)``
    и пробрасывается наружу.
    """
    (tmp_path / "package.json").write_text(
        '{"name": "app", "dependencies": null, "devDependencies": null}',
        encoding="utf-8",
    )
    # Без исключения; манифест найден -> node; технологии не определены
    stack = detect_project_stack(tmp_path)
    assert "node" in stack
    assert "react" not in stack


def test_stack_detector_node_dependencies_valid_still_works(tmp_path: Path) -> None:
    """Нул-толерантность не ломает обычный путь: react из dependencies находится."""
    (tmp_path / "package.json").write_text(
        '{"name": "app", "dependencies": {"react": "^18.0.0"}, "devDependencies": null}',
        encoding="utf-8",
    )
    stack = detect_project_stack(tmp_path)
    assert "node" in stack
    assert "react" in stack


def test_stack_detector_sqlite_detection(tmp_path: Path) -> None:
    # 1. db file in root
    (tmp_path / "app.db").write_bytes(b"")
    stack = detect_project_stack(tmp_path)
    assert "sqlite" in stack

    # 2. pyproject with sqlite3
    proj2 = tmp_path / "proj2"
    proj2.mkdir()
    (proj2 / "pyproject.toml").write_text(
        '[project]\ndependencies = ["sqlite3-api>=1.0"]\n', encoding="utf-8"
    )
    assert "sqlite" in detect_project_stack(proj2)

    # 3. Cargo.toml with rusqlite
    proj3 = tmp_path / "proj3"
    proj3.mkdir()
    (proj3 / "Cargo.toml").write_text('[dependencies]\nrusqlite = "0.31"\n', encoding="utf-8")
    stack3 = detect_project_stack(proj3)
    assert "sqlite" in stack3
    assert "rust" in stack3


def test_stack_detector_tauri_detection(tmp_path: Path) -> None:
    # 1. tauri.conf.json in root
    (tmp_path / "tauri.conf.json").write_text("{}", encoding="utf-8")
    stack = detect_project_stack(tmp_path)
    assert "tauri" in stack
    assert "rust" in stack

    # 2. src-tauri/tauri.conf.json
    proj2 = tmp_path / "proj2"
    (proj2 / "src-tauri").mkdir(parents=True)
    (proj2 / "src-tauri" / "tauri.conf.json").write_text("{}", encoding="utf-8")
    stack2 = detect_project_stack(proj2)
    assert "tauri" in stack2
    assert "rust" in stack2


def test_stack_detector_audio_ml_detection(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["faster-whisper>=1.0.0", "torchaudio"]\n',
        encoding="utf-8",
    )
    stack = detect_project_stack(tmp_path)
    assert "audio-ml" in stack
    assert "python" in stack


def test_stack_detector_monorepo_subdirectories(tmp_path: Path) -> None:
    """S-STACK (466c): detect_project_stack видит package.json в ограниченных подкаталогах."""
    # 1. frontend/package.json
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        '{"name": "my-fe", "dependencies": {"react": "^18.0", "tailwindcss": "^3.0"}}',
        encoding="utf-8",
    )

    # 2. apps/desktop/package.json
    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text(
        '{"name": "my-desktop", "dependencies": {"next": "^14.0"}}',
        encoding="utf-8",
    )

    # 3. packages/ui/package.json
    ui = tmp_path / "packages" / "ui"
    ui.mkdir(parents=True)
    (ui / "package.json").write_text(
        '{"name": "my-ui", "dependencies": {"vue": "^3.0"}}',
        encoding="utf-8",
    )

    # 4. Исключённые каталоги: node_modules, dist, .hidden
    nm = tmp_path / "node_modules" / "some-lib"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text(
        '{"name": "lib", "dependencies": {"electron": "^28.0"}}',
        encoding="utf-8",
    )

    dist = tmp_path / "dist" / "bundle"
    dist.mkdir(parents=True)
    (dist / "package.json").write_text(
        '{"name": "bundle", "dependencies": {"tauri": "^1.0"}}',
        encoding="utf-8",
    )

    hidden = tmp_path / ".hidden_app"
    hidden.mkdir()
    (hidden / "package.json").write_text(
        '{"name": "hidden", "dependencies": {"sqlite3": "^5.0"}}',
        encoding="utf-8",
    )

    # 5. Битый JSON в broken/
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "package.json").write_text("{broken json", encoding="utf-8")

    # 6. Корень с python
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root-app"\ndependencies = ["fastapi"]\n',
        encoding="utf-8",
    )

    stack = detect_project_stack(tmp_path)

    # Проверяем обнаруженные технологии
    assert "python" in stack
    assert "fastapi" in stack
    assert "node" in stack
    assert "react" in stack
    assert "tailwind" in stack
    assert "nextjs" in stack
    assert "vue" in stack

    # Проверяем, что из исключённых каталогов ничего не попало
    assert "electron" not in stack
    assert "tauri" not in stack
    assert "sqlite" not in stack
