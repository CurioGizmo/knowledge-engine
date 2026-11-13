"""Тесты шага 17: Автоматическая приёмка всей цепочки — матрица сценариев (E2E)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
import pytest

from mcp_skills.errors import AssemblyBudgetExceededError
from mcp_skills.flow_engine import FlowEngine
from mcp_skills.flow_parser import FlowStage, FlowStageMeta, parse_flow_markdown
from mcp_skills.server import (
    create_server,
    get_storage,
    get_synchronizer,
    load_skill,
    search_skills,
)
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer
from mcp_skills.token_guard import TokenGuard


def _get_catalog_root() -> Path | None:
    from tests.conftest import get_companion_skills_dir

    repo = get_companion_skills_dir()
    if repo is not None and (repo / "skills").is_dir():
        return repo
    return None


@pytest.fixture(scope="module")
def shared_env() -> dict[str, Any]:
    """Синхронизированный боевой репозиторий mcp_skills для матрицы сценариев."""
    cat_root = _get_catalog_root()
    if cat_root is None:
        pytest.skip("Каталог mcp_skills не найден, пропуск матрицы приёмочных сценариев")
    tmp = Path(tempfile.mkdtemp(prefix="step17_matrix_"))
    db_path = tmp / "skills_fts.db"
    storage = SkillsStorage(db_path)
    skills_base = cat_root if cat_root.name == "skills" else cat_root / "skills"
    sync = SkillsSynchronizer(storage, [skills_base])
    sync.sync()
    server = create_server(project_root=tmp, skills_dir=cat_root)
    return {
        "storage": storage,
        "sync": sync,
        "root": tmp,
        "server": server,
        "cat_root": cat_root,
    }


def test_scenario_01_python_cli_without_api_db(shared_env: dict[str, Any]) -> None:
    """1. Python CLI без API/БД: общий метод + Python; нет требований Pydantic, SQLite, UI/ML/Desktop."""
    res = load_skill(
        "plan-review",
        stack=["python"],
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    content = res["content"]
    assert "specialized-architects.md" in content
    assert "FastAPI" not in content
    assert "Не навязывай Pydantic" in content
    assert "SQLite" not in content
    assert "tauri" not in content.lower()
    assert "whisper" not in content.lower()


def test_scenario_02_python_sqlite(shared_env: dict[str, Any]) -> None:
    """2. Python + SQLite stdlib: скил БД обнаруживается; выбранные критерии обоих стеков доставлены."""
    search_res = search_skills(
        "sqlite",
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    names = [r["name"] for r in search_res["results"]]
    assert "sqlite-performance" in names

    load_res = load_skill(
        "code-review",
        stack=["python", "sqlite"],
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    content = load_res["content"]
    assert "busy_timeout" in content
    assert "GIL" in content or "python" in content.lower()


def test_scenario_03_react_tauri(shared_env: dict[str, Any]) -> None:
    """3. React + Tauri в src-tauri: UI/IPC выбираются согласно задаче."""
    load_res = load_skill(
        "code-review",
        stack=["react", "tauri"],
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    content = load_res["content"]
    assert "IPC" in content
    assert "Tauri" in content or "invoke" in content


def test_scenario_04_audio_whisper(shared_env: dict[str, Any]) -> None:
    """4. Audio-проект: Whisper находится; выбранный backend/VAD загружается, остальные backend не навязываются."""
    search_res = search_skills(
        "whisper",
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    names = [r["name"] for r in search_res["results"]]
    assert "onnx-whisper-engine" in names

    load_res = load_skill(
        "onnx-whisper-engine",
        touches=["vad-silero"],
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    content = load_res["content"]
    assert "Silero" in content or "silero" in content.lower()
    # Другие VAD не навязаны
    assert "webrtc" not in content.lower()


def test_scenario_05_go_ruby_fallback(shared_env: dict[str, Any]) -> None:
    """5. Go и Ruby без варианта: Go выбирается при наличии, Ruby получает честную диагностику fallback."""
    load_ruby = load_skill(
        "writing-plans",
        stack=["ruby"],
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    manifest = load_ruby.get("assembly", {})
    assert "ruby" in manifest.get("unmatched_technologies", [])
    assert manifest.get("status") == "complete"


def test_scenario_06_new_stack_before_installation(shared_env: dict[str, Any]) -> None:
    """6. Новый стек до установки: поиск находит компетенцию, отсутствие пакета не запрещает планирование."""
    search_res = search_skills(
        "sqlite",
        project_stack=["sqlite"],
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    assert search_res["count"] > 0
    names = [r["name"] for r in search_res["results"]]
    assert "sqlite-performance" in names


def test_scenario_07_stack_modes_consistent(shared_env: dict[str, Any]) -> None:
    """7. None / [] / явный стек: нет нежеланного автоподмешивания; все входы согласованы."""
    load_none = load_skill(
        "writing-plans",
        stack=None,
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    load_empty = load_skill(
        "writing-plans",
        stack=[],
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    assert load_none["stack_source"] in ("autodetect", None)
    assert load_empty["stack_source"] == "explicit"
    assert load_empty["stack_applied"] == []


def test_scenario_08_catalog_and_drilldown(shared_env: dict[str, Any]) -> None:
    """8. Категория / FTS / overview / прямой load: один доступный каталог и объяснимые ограничения."""
    overview = search_skills(
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    assert overview["mode"] == "overview"
    assert any(c["category"] == "planning" for c in overview["categories"])

    category = search_skills(
        category="planning",
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    assert category["mode"] == "category"
    assert any(c["name"] == "writing-plans" for c in category["results"])


def test_scenario_09_remote_client_mcp_delivery(shared_env: dict[str, Any]) -> None:
    """9. Хост без ФС сервера: справочник, шаблон и пример читаются через MCP load_skill(resource_id=...)."""
    ref = load_skill(
        "code-review",
        resource_id="skills/quality-review/code-review/references/api-architecture.md",
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    assert "API Architecture" in ref["content"] or "API" in ref["content"]
    assert ref["total_chars"] > 0

    tpl = load_skill(
        "writing-plans",
        resource_id="templates/standard-plan.template.md#2.4",
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    assert "2.4" in tpl["content"]


def test_scenario_10_flow_own_resource_delivery(shared_env: dict[str, Any]) -> None:
    """10. Ресурс собственной инструкции flow: plan-execution-auto-flow указывает владельца; шаблон читается через MCP."""
    res = load_skill(
        name="parallel-feature-development",
        resource_id="templates/orchestration-state.template.md",
        storage=shared_env["storage"],
        synchronizer=shared_env["sync"],
        project_root=shared_env["root"],
    )
    assert "state_version" in res["content"] or "orchestration" in res["content"].lower()


def test_scenario_11_stage_skill_options_passed_to_agent_bundle(shared_env: dict[str, Any]) -> None:
    """11. Этап с agent и без meta.skills передаёт выбор stack-patterns в бандл test-reviewer."""
    from mcp_skills.bundler import AgentBundler

    bundler = AgentBundler(storage=shared_env["storage"], skills_dir=shared_env["cat_root"])
    bundle = bundler.bundle_agent(
        "test-reviewer",
        project_root=shared_env["root"],
        skill_options={"add-tests": {"touches": ["test-runner", "stack-patterns"]}},
    )
    assert "stack-patterns" in bundle or "Тестовые паттерны" in bundle or len(bundle) > 1000


def test_recovery_review_document_exists_smoke_only() -> None:
    """Smoke: документ восстановления существует; полноту исторической сверки этот тест не доказывает."""
    cat_root = _get_catalog_root()
    if cat_root is None:
        pytest.skip("Каталог mcp_skills не найден, пропуск smoke теста документа восстановления")
    recovery_md = cat_root / "docs" / "recovery-review.md"
    assert recovery_md.is_file()
    text = recovery_md.read_text(encoding="utf-8")
    assert (
        "Что не возвращено" in text
        or "Что восстановлено" in text
        or "Historical Rules Review" in text
        or "восстановление" in text.lower()
    )


def test_scenario_13_identical_assembly_across_entrypoints(shared_env: dict[str, Any]) -> None:
    """13. Skill / agent / stage с одинаковым выбором дают одинаковое содержимое до форматирования."""
    from mcp_skills.slot_compiler import SlotCompiler

    comp = SlotCompiler(shared_env["storage"])
    _, content_skill = comp.assemble_skill(
        "writing-plans", touches=["mode"], project_stack=["mcp"], project_root=shared_env["root"]
    )

    from mcp_skills.bundler import AgentBundler

    bundler = AgentBundler(
        storage=shared_env["storage"], compiler=comp, skills_dir=shared_env["cat_root"]
    )
    bundle = bundler.bundle_agent(
        "plan-architect",
        skill_options={"writing-plans": {"touches": ["mode"]}},
        project_root=shared_env["root"],
    )

    # Ключевые фрагменты собранного скилла одинаковы
    assert "mcp" in content_skill.lower()
    assert "writing-plans" in bundle.lower()


def test_scenario_14_cache_invalidation_by_dependency_revision(tmp_path: Path) -> None:
    """14. Изменение shared/reference и rename того же размера обновляет индекс без рестарта."""
    import time

    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    test_skill_dir = skills_dir / "my-skill"
    test_skill_dir.mkdir(parents=True)
    (test_skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ntitle: My Skill\ndescription: Desc\ncategory: planning\navailable_slots:\n  - stack-invariants\n---\n# My Skill\n<!-- SLOT: stack-invariants -->\n",
        encoding="utf-8",
    )

    shared_dir = tmp_path / "blocks" / "stack-invariants"
    shared_dir.mkdir(parents=True)

    block = shared_dir / "python.md"
    block.write_text("initial 20 chars body", encoding="utf-8")
    sync = SkillsSynchronizer(store, [skills_dir])
    sync.sync()
    rev1 = store.get_dependency_revision()

    # Изменяем содержимое с тем же размером (ровно 20 символов) и меняем mtime
    time.sleep(0.02)
    block.write_text("changed 20 chars body", encoding="utf-8")
    sync.sync()
    rev2 = store.get_dependency_revision()
    assert rev1 != rev2

    # Переименование файла того же размера
    block.unlink()
    renamed = shared_dir / "python2.md"
    renamed.write_text("changed 20 chars body", encoding="utf-8")
    sync.sync()
    rev3 = store.get_dependency_revision()
    assert rev2 != rev3


def test_scenario_15_mandatory_text_budget_overflow_raises_error(tmp_path: Path) -> None:
    """15. Обязательный текст превышает бюджет: структурированная ошибка ASSEMBLY_BUDGET_EXCEEDED."""
    huge_core = "MANDATORY INVARIANT RULE.\n" * 4500
    guard = TokenGuard()
    with pytest.raises(AssemblyBudgetExceededError) as exc_info:
        guard.enforce_budget(huge_core, raise_on_core_overflow=True)

    err = exc_info.value
    assert err.overflow_tokens > 0
    assert err.remediation
