"""Тесты архитектуры оверрайдов субагентов (Base + Single Override)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest
import yaml

from mcp_skills.adapters import get_adapter
from mcp_skills.bundler import (
    AgentBundler,
    _validate_agent_key,
    create_default_override,
    merge_profile_with_override,
)
from mcp_skills.git_manager import GitSkillsManager
from mcp_skills.models import AgentOverride, AgentPermissions, AgentProfile
from mcp_skills.server import load_agent, save_agent_override
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


@pytest.fixture(autouse=True)
def isolate_skills_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Изолирует глобальный кэш скиллов во временной директории для всех тестов модуля."""
    cache_dir = tmp_path / "cache_repo"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_SKILLS_CACHE_DIR", str(cache_dir))
    return cache_dir


def test_validate_agent_key_valid() -> None:
    """Корректные ключи агента проходят валидацию без ошибок."""
    _validate_agent_key("reviewer", "antigravity", "my-repo")
    _validate_agent_key("coder", "opencode", None)
    _validate_agent_key("lead-arch.v1", "cursor", "repo_123")


def test_validate_agent_key_invalid() -> None:
    """Некорректные параметры ключа отклоняются с ValueError."""
    with pytest.raises(ValueError, match="Недопустимое имя агента"):
        _validate_agent_key("../etc/passwd", "antigravity", "my-repo")

    with pytest.raises(ValueError, match="Недопустимое имя агента"):
        _validate_agent_key("reviewer/child", "antigravity", "my-repo")

    with pytest.raises(ValueError, match="Неподдерживаемый целевой рантайм"):
        _validate_agent_key("reviewer", "unknown_runtime", "my-repo")

    with pytest.raises(ValueError, match="Недопустимое имя проекта"):
        _validate_agent_key("reviewer", "antigravity", "../../traversal")

    with pytest.raises(ValueError, match="Недопустимое имя проекта"):
        _validate_agent_key("reviewer", "antigravity", "org/repo")


def test_merge_profile_with_override_skills_dedup() -> None:
    """extra_skills объединяются с base.skills с сохранением порядка и дедупликацией."""
    base = AgentProfile(
        name="test-agent",
        description="Base description",
        skills=["skill-a", "skill-b"],
        instructions="Base prompt",
    )
    override = AgentOverride(
        name="test-agent",
        target="antigravity",
        project="test-proj",
        extra_skills=["skill-b", "skill-c", "skill-a"],
    )

    merged = merge_profile_with_override(base, override)
    assert merged.skills == ["skill-a", "skill-b", "skill-c"]


def test_merge_profile_with_override_skills_replace() -> None:
    """override.skills полностью заменяет base.skills."""
    base = AgentProfile(
        name="test-agent",
        description="Base description",
        skills=["skill-a", "skill-b"],
        instructions="Base prompt",
    )
    override = AgentOverride(
        name="test-agent",
        target="antigravity",
        project="test-proj",
        skills=["skill-x", "skill-y", "skill-x"],
    )

    merged = merge_profile_with_override(base, override)
    assert merged.skills == ["skill-x", "skill-y"]


def test_merge_profile_with_override_permissions() -> None:
    """Permissions объединяются на уровне полей (field-level merge)."""
    base = AgentProfile(
        name="test-agent",
        description="Base description",
        permissions=AgentPermissions(write=False, mcp=True, subagents=False),
    )
    override = AgentOverride(
        name="test-agent",
        target="antigravity",
        project="test-proj",
        permissions=AgentPermissions(write=True, mcp=False, subagents=True),
    )

    merged = merge_profile_with_override(base, override)
    assert merged.permissions.write is True
    assert merged.permissions.mcp is False
    assert merged.permissions.subagents is True


def test_merge_profile_with_override_instructions_and_model() -> None:
    """Инструкции дополняются суффиксом, модель переопределяется."""
    base = AgentProfile(
        name="test-agent",
        description="Base description",
        instructions="Base instructions.",
        model="flash",
        readonly_acknowledged=True,
    )
    override = AgentOverride(
        name="test-agent",
        target="opencode",
        project="test-proj",
        instructions_suffix="Project-specific override instructions.",
        model="pro",
        model_name="claude-3-7-sonnet",
        tools=["git", "read"],
    )

    merged = merge_profile_with_override(base, override)
    assert merged.instructions == "Base instructions.\n\nProject-specific override instructions."
    assert merged.model == "pro"

    # Проверяем экспорт через OpenCode адаптер.
    # Профиль read-only (write=False по умолчанию): контракт R2 deny-by-default —
    # wildcard-запрет первым, tools из оверрайда вне allowlist явно запрещены.
    adapter = get_adapter("opencode")
    exported = adapter.export_agent(merged, "")
    frontmatter_yaml = exported.split("---")[1]
    parsed = yaml.safe_load(frontmatter_yaml)
    assert parsed["model"] == "claude-3-7-sonnet"
    assert parsed["tools"]["*"] is False
    assert parsed["tools"]["git"] is False
    assert parsed["tools"]["read"] is True
    assert "Project-specific override instructions." in exported


def test_find_override_candidate_precedence(tmp_path: Path, isolate_skills_cache: Path) -> None:
    """Проверка приоритета кандидатов при поиске оверрайда."""
    storage = SkillsStorage(tmp_path / "skills.db")
    compiler = SlotCompiler(storage)
    bundler = AgentBundler(storage=storage, compiler=compiler)

    overrides_dir = isolate_skills_cache / "agents" / "coder" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)

    # Создаём кандидатов разного уровня специфичности
    f_general = overrides_dir / "coder.override.yaml"
    f_target = overrides_dir / "coder_opencode.override.yaml"
    f_proj = overrides_dir / "coder_my-repo.override.yaml"
    f_exact = overrides_dir / "coder_my-repo_opencode.override.yaml"

    f_general.write_text(
        yaml.safe_dump({"name": "coder", "instructions_suffix": "general"}), encoding="utf-8"
    )
    found = bundler.find_override("coder", "opencode", "my-repo")
    assert found is not None and found.instructions_suffix == "general"

    f_target.write_text(
        yaml.safe_dump({"name": "coder", "instructions_suffix": "target"}), encoding="utf-8"
    )
    found = bundler.find_override("coder", "opencode", "my-repo")
    assert found is not None and found.instructions_suffix == "target"

    f_proj.write_text(
        yaml.safe_dump({"name": "coder", "instructions_suffix": "project"}), encoding="utf-8"
    )
    found = bundler.find_override("coder", "opencode", "my-repo")
    assert found is not None and found.instructions_suffix == "project"

    f_exact.write_text(
        yaml.safe_dump({"name": "coder", "instructions_suffix": "exact"}), encoding="utf-8"
    )
    found = bundler.find_override("coder", "opencode", "my-repo")
    assert found is not None and found.instructions_suffix == "exact"


def test_load_agent_read_only_in_memory_draft(tmp_path: Path, isolate_skills_cache: Path) -> None:
    """load_agent при отсутствии оверрайда возвращает in-memory драфт и НИЧЕГО не пишет на диск."""
    storage = SkillsStorage(tmp_path / "skills.db")
    agents_dir = tmp_path / ".agents" / "agents" / "reviewer"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "AGENT.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "reviewer",
                "description": "Code reviewer",
                "skills": [],
                "instructions": "Be rigorous.",
            }
        ),
        encoding="utf-8",
    )

    res = load_agent(
        name="reviewer",
        target="antigravity",
        project="cool-project",
        project_root=tmp_path,
        storage=storage,
    )

    assert res["agent"] == "reviewer"
    assert res["target"] == "antigravity"
    assert res["project"] == "cool-project"
    assert res["is_draft"] is True
    assert res["draft_override"] is not None
    assert res["draft_override"]["base_agent"] == "reviewer"
    assert res["draft_override"]["target"] == "antigravity"
    assert res["draft_override"]["project"] == "cool-project"
    assert "Be rigorous." in res["content"]

    # КРИТИЧЕСКИЙ ИНВАРИАНТ: Никаких файлов оверрайда не создано на диске!
    overrides_dir = isolate_skills_cache / "agents" / "reviewer" / "overrides"
    assert not overrides_dir.exists()
    assert not (tmp_path / ".agents" / "agents" / "reviewer" / "overrides").exists()


def test_save_agent_override_atomic_and_effective(
    tmp_path: Path, isolate_skills_cache: Path
) -> None:
    """save_agent_override сохраняет оверрайд в кэш-репозиторий, после чего load_agent подтягивает его."""
    storage = SkillsStorage(tmp_path / "skills.db")
    agents_dir = tmp_path / ".agents" / "agents" / "architect"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "AGENT.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "architect",
                "description": "Architectural agent",
                "skills": [],
                "instructions": "Think high level.",
                "permissions": {"subagents": False},
            }
        ),
        encoding="utf-8",
    )

    override_payload = {
        "name": "architect",
        "target": "antigravity",
        "project": "engine-app",
        "instructions_suffix": "Focus on distributed scalability.",
        "permissions": {"subagents": True},
    }
    content = yaml.safe_dump(override_payload)

    # 1. Сохраняем оверрайд через save_agent_override
    save_res = save_agent_override(
        name="architect",
        project="engine-app",
        target="antigravity",
        content=content,
        cache_dir=isolate_skills_cache,
    )
    assert save_res["success"] is True
    saved_file = Path(save_res["path"])
    assert saved_file.is_file()
    assert saved_file.parent.name == "overrides"
    assert saved_file.name == "architect_engine-app_antigravity.override.yaml"

    # 2. Проверяем, что локальный проект остался чистым (в .agents/architect ничего лишнего не появилось)
    assert list((tmp_path / ".agents" / "agents" / "architect").iterdir()) == [
        tmp_path / ".agents" / "agents" / "architect" / "AGENT.yaml"
    ]

    # 3. Вызываем load_agent: теперь это уже не драфт, оверрайд смержен
    res = load_agent(
        name="architect",
        target="antigravity",
        project="engine-app",
        project_root=tmp_path,
        storage=storage,
    )
    assert res["is_draft"] is False
    assert res["draft_override"] is None
    payload = json.loads(res["content"])
    assert "Focus on distributed scalability." in payload["system_prompt"]
    assert payload["enable_subagent_tools"] is True


def test_save_agent_override_security_rejections(isolate_skills_cache: Path) -> None:
    """Безопасность: path traversal, спам/секреты и битый YAML отклоняются."""
    # Path traversal в параметрах
    res_trav = save_agent_override(
        name="test",
        project="../../etc",
        target="antigravity",
        content="name: test\n",
        cache_dir=isolate_skills_cache,
    )
    assert res_trav["success"] is False
    assert "валидации" in res_trav["message"] or "Недопустимое" in str(res_trav["issues"])

    # Утечка секретов через SpamGate
    leak_content = yaml.safe_dump(
        {
            "name": "test",
            "instructions_suffix": "API key: sk-abcdef1234567890abcdef1234567890",
        }
    )
    res_leak = save_agent_override(
        name="test",
        project="my-repo",
        target="antigravity",
        content=leak_content,
        cache_dir=isolate_skills_cache,
    )
    assert res_leak["success"] is False
    assert "SpamGate" in res_leak["message"] or "секрет" in str(res_leak["issues"])

    # Некорректный YAML
    res_yaml = save_agent_override(
        name="test",
        project="my-repo",
        target="antigravity",
        content="name: [unclosed list",
        cache_dir=isolate_skills_cache,
    )
    assert res_yaml["success"] is False
    assert "YAML" in res_yaml["message"] or "YAML" in str(res_yaml["issues"])


def test_save_agent_override_with_git_commit(isolate_skills_cache: Path) -> None:
    """Если кэш-директория является git-репозиторием, save_agent_override делает git staging/commit."""
    import subprocess

    subprocess.run(["git", "init"], cwd=str(isolate_skills_cache), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "TestUser"],
        cwd=str(isolate_skills_cache),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(isolate_skills_cache),
        check=True,
    )

    content = yaml.safe_dump({"name": "bot", "instructions_suffix": "Git enabled."})
    res = save_agent_override(
        name="bot",
        project="repo1",
        target="opencode",
        content=content,
        commit_message="Add override for repo1",
        cache_dir=isolate_skills_cache,
    )
    assert res["success"] is True

    # Проверяем, что коммит создан
    proc = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=str(isolate_skills_cache),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Add override for repo1" in proc.stdout


def test_merge_profile_partial_permissions_preserves_base() -> None:
    """Частичный оверрайд permissions не сбрасывает неуказанные поля в дефолтные значения."""
    base = AgentProfile(
        name="test-agent",
        description="Base description",
        permissions=AgentPermissions(write=False, mcp=False, subagents=False),
    )
    # Оверрайд задает ТОЛЬКО write=True в YAML
    override_data = yaml.safe_load("name: test-agent\npermissions:\n  write: true\n")
    override = AgentOverride.model_validate(override_data)

    merged = merge_profile_with_override(base, override)
    assert merged.permissions.write is True
    # mcp должен остаться False (как в base), а не сброситься в дефолт True!
    assert merged.permissions.mcp is False
    assert merged.permissions.subagents is False


def test_save_agent_override_parameter_mismatch_rejection(isolate_skills_cache: Path) -> None:
    """Несоответствие base_agent, target или project внутри контента и аргументов отклоняется."""
    # Несовпадение имени агента
    bad_name_content = yaml.safe_dump({"name": "evil-agent", "instructions_suffix": "hacked"})
    res1 = save_agent_override(
        name="good-agent",
        project="repo1",
        target="antigravity",
        content=bad_name_content,
        cache_dir=isolate_skills_cache,
    )
    assert res1["success"] is False
    assert "Несоответствие" in res1["message"] or "не совпадает" in str(res1["issues"])

    # Несовпадение целевого рантайма
    bad_target_content = yaml.safe_dump({"name": "bot", "target": "opencode"})
    res2 = save_agent_override(
        name="bot",
        project="repo1",
        target="antigravity",
        content=bad_target_content,
        cache_dir=isolate_skills_cache,
    )
    assert res2["success"] is False
    assert "Несоответствие" in res2["message"] or "не совпадает" in str(res2["issues"])


def test_cursor_and_prompt_adapters_render_instructions() -> None:
    """CursorAdapter и PromptAdapter корректно включают instructions профиля."""
    profile = AgentProfile(
        name="coder",
        role="Senior Developer",
        description="Writes robust code",
        instructions="Always write type annotations and docstrings.",
    )

    cursor_adapter = get_adapter("cursor")
    cursor_output = cursor_adapter.export_agent(profile, "Skill content")
    assert "## Instructions" in cursor_output
    assert "Always write type annotations and docstrings." in cursor_output

    prompt_adapter = get_adapter("prompt")
    prompt_output = prompt_adapter.export_agent(profile, "Skill content")
    assert "## Instructions" in prompt_output
    assert "Always write type annotations and docstrings." in prompt_output


def test_load_agent_passes_project_root_to_find_override(tmp_path: Path) -> None:
    """load_agent учитывает переданный project_root при поиске оверрайда."""
    storage = SkillsStorage(tmp_path / "skills.db")
    project_dir = tmp_path / "custom_project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # Базовый агент
    agent_dir = project_dir / ".agents" / "agents" / "tester"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "AGENT.yaml").write_text(
        yaml.safe_dump(
            {"name": "tester", "description": "QA", "instructions": "Base instructions"}
        ),
        encoding="utf-8",
    )

    # Локальный проектный оверрайд
    (project_dir / ".agents" / "agents" / "tester_my-app_antigravity.override.yaml").write_text(
        yaml.safe_dump({"name": "tester", "instructions_suffix": "Found in custom root!"}),
        encoding="utf-8",
    )

    res = load_agent(
        name="tester",
        target="antigravity",
        project="my-app",
        project_root=project_dir,
        storage=storage,
    )
    assert res["is_draft"] is False
    assert "Found in custom root!" in res["content"]


def test_save_override_with_long_project_and_agent_names(isolate_skills_cache: Path) -> None:
    """MAJOR-1: SpamGate не отклоняет оверрайды, когда сумма длин name + project + target превышает 64 символа."""
    long_name = "security-auditor-specialist"  # 27 chars
    long_project = "super-awesome-platform-repo"  # 27 chars
    target = "antigravity"  # 11 chars
    total_len = len(f"{long_name}_{long_project}_{target}")
    assert total_len == 67
    assert total_len > 64

    content = yaml.safe_dump(
        {
            "name": long_name,
            "project": long_project,
            "target": target,
            "instructions_suffix": "Deep security scan.",
        }
    )

    res = save_agent_override(
        name=long_name,
        project=long_project,
        target=target,
        content=content,
        cache_dir=isolate_skills_cache,
    )
    assert res["success"] is True
    assert Path(res["path"]).is_file()


def test_find_override_fail_closed_on_corrupted_file(
    tmp_path: Path, isolate_skills_cache: Path
) -> None:
    """MAJOR-2: Повреждённый или невалидный файл оверрайда вызывает fail-closed ошибку, а не молчаливый fallback."""
    bundler = AgentBundler()
    overrides_dir = isolate_skills_cache / "agents" / "coder" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)

    # Записываем оверрайд с невалидным типом в permissions
    corrupted_file = overrides_dir / "coder_my-repo_antigravity.override.yaml"
    corrupted_file.write_text("permissions:\n  write: notabool\n", encoding="utf-8")

    # Дополнительно создаем валидный общий оверрайд
    general_file = overrides_dir / "coder_antigravity.override.yaml"
    general_file.write_text("instructions_suffix: GENERAL\n", encoding="utf-8")

    # При поиске специфичного оверрайда должен произойти fail-closed (ValueError), а не fallback на general
    with pytest.raises(ValueError, match="Ошибка загрузки оверрайда"):
        bundler.find_override(
            name="coder",
            target="antigravity",
            project="my-repo",
            project_root=tmp_path,
        )


def test_all_adapters_honor_model_name_and_tools() -> None:
    """MAJOR-3: Все 4 адаптера (antigravity, opencode, cursor, prompt) учитывают model_name и tools."""
    profile = AgentProfile(
        name="polyglot",
        role="Multi-Runtime Expert",
        description="Works with all runtime environments",
        model="pro",
        model_name="claude-3-7-sonnet",
        tools=["search_knowledge", "record_knowledge"],
        readonly_acknowledged=True,
        instructions="Always test edge cases.",
    )

    # 1. AntigravityAdapter
    ag_out = get_adapter("antigravity").export_agent(profile, "Skill A")
    ag_data = json.loads(ag_out)
    assert ag_data["model"] == "claude-3-7-sonnet"
    assert ag_data["tools"] == ["search_knowledge", "record_knowledge"]

    # 2. OpenCodeAdapter
    oc_out = get_adapter("opencode").export_agent(profile, "Skill A")
    assert "model: claude-3-7-sonnet" in oc_out
    assert "tools:" in oc_out
    assert "search_knowledge: true" in oc_out

    # 3. CursorAdapter
    cur_out = get_adapter("cursor").export_agent(profile, "Skill A")
    assert "Model: claude-3-7-sonnet" in cur_out
    assert "Tools: search_knowledge, record_knowledge" in cur_out

    # 4. PromptAdapter
    prm_out = get_adapter("prompt").export_agent(profile, "Skill A")
    assert "Model: claude-3-7-sonnet" in prm_out
    assert "Allowed Tools: search_knowledge, record_knowledge" in prm_out


def test_find_override_project_local_beats_cache(
    tmp_path: Path, isolate_skills_cache: Path
) -> None:
    """MINOR-5 (a): Проектно-локальный оверрайд имеет приоритет над центральным кэш-оверрайдом."""
    bundler = AgentBundler()
    project_root = tmp_path / "my_project"
    project_root.mkdir(parents=True, exist_ok=True)

    # 1. Оверрайд в центральном кэше
    cache_overrides = isolate_skills_cache / "agents" / "lead" / "overrides"
    cache_overrides.mkdir(parents=True, exist_ok=True)
    (cache_overrides / "lead_my-app_antigravity.override.yaml").write_text(
        yaml.safe_dump({"name": "lead", "instructions_suffix": "FROM_CACHE"}),
        encoding="utf-8",
    )

    # 2. Оверрайд в локальном проекте
    local_agents = project_root / ".agents" / "agents"
    local_agents.mkdir(parents=True, exist_ok=True)
    (local_agents / "lead_my-app_antigravity.override.yaml").write_text(
        yaml.safe_dump({"name": "lead", "instructions_suffix": "FROM_PROJECT_LOCAL"}),
        encoding="utf-8",
    )

    override = bundler.find_override(
        name="lead",
        target="antigravity",
        project="my-app",
        project_root=project_root,
    )
    assert override is not None
    assert override.instructions_suffix == "FROM_PROJECT_LOCAL"


def test_save_override_git_failure_reported_in_issues(
    isolate_skills_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MINOR-4: Ошибка git-коммита возвращается в issues результата сохранения оверрайда."""
    import subprocess

    # Создаем фиктивный .git каталог в кэше
    (isolate_skills_cache / ".git").mkdir(parents=True, exist_ok=True)

    # Подменяем subprocess.run для имитации сбоя git commit
    original_run = subprocess.run

    def mock_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "add" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if "commit" in cmd:
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr="fatal: mock commit hook failed"
            )
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)

    res = save_agent_override(
        name="coder",
        project="demo",
        target="antigravity",
        content=yaml.safe_dump({"name": "coder"}),
        commit_message="Add coder override",
        cache_dir=isolate_skills_cache,
    )
    assert res["success"] is True
    assert any("mock commit hook failed" in issue for issue in res["issues"])


# ---------- A6: bundle_agent теряет блоки (план review-fixes, verdict_4 §1) ----------


from tests.conftest import get_companion_skills_dir

_catalog = get_companion_skills_dir()
CATALOG_SKILLS_DIR = (
    (_catalog / "skills")
    if _catalog is not None
    else (Path(__file__).resolve().parents[1].parent / "mcp_skills" / "skills")
)


def _isolate_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убирает влияние окружения на detect_mode в тесте."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)


def test_bundle_agent_injects_blocks_add_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6 (ядро): бандл скилла add-tests из реального каталога = 7831 симв., не 5776.

    Воспроизведение verdict_4 §1: bundle_agent() не передаёт touches ->
    effective_slots = [] -> _inject_slots зачищает маркеры, блоки blocks/
    (python.md с conftest-паттернами) теряются. Эталон — load_skill
    (server.py:717-732): effective_slots = available_slots при touches=None.
    """
    if not CATALOG_SKILLS_DIR.is_dir():
        pytest.skip(f"Каталог скиллов {CATALOG_SKILLS_DIR} недоступен")

    _isolate_mode_env(monkeypatch)
    storage = SkillsStorage(tmp_path / "skills.db")
    SkillsSynchronizer(storage, [CATALOG_SKILLS_DIR]).sync()
    compiler = SlotCompiler(storage)
    bundler = AgentBundler(storage=storage, compiler=compiler)

    # Пустой python-проект: детектор даст ['python'], режим неизвестен (None)
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text(
        "[project]\nname = 'bundle-test'\n", encoding="utf-8"
    )

    profile = AgentProfile(
        name="test-agent",
        description="Bundle blocks guard",
        skills=["add-tests"],
        instructions="Follow TDD.",
    )
    bundle = bundler.bundle_agent(
        profile=profile,
        target="prompt",
        project_root=project_root,
    )

    assert "pytest" in bundle.lower()  # обязательный блок test-runner на месте
    assert "conftest" not in bundle  # опциональный блок stack-patterns исключён по умолчанию
    skills_section = bundle.split("## Guidelines & Skills\n\n", 1)[1].strip()
    assert len(skills_section) > 1000


def test_bundle_agent_mode_block_injected_for_mcp_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6 (detect_mode): бандл подставляет mode/mcp.md в mcp-проекте (3699, не 1365).

    verdict_4 §1: technical-debt-management со слотами = 3699 симв. при
    project_stack=['mcp'] — значит bundler обязан передавать detect_mode(root)
    в project_stack, а не только технологии.
    """
    if not CATALOG_SKILLS_DIR.is_dir():
        pytest.skip(f"Каталог скиллов {CATALOG_SKILLS_DIR} недоступен")

    _isolate_mode_env(monkeypatch)
    storage = SkillsStorage(tmp_path / "skills.db")
    SkillsSynchronizer(storage, [CATALOG_SKILLS_DIR]).sync()
    compiler = SlotCompiler(storage)
    bundler = AgentBundler(storage=storage, compiler=compiler)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text(
        "[project]\nname = 'bundle-mode-test'\n", encoding="utf-8"
    )
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine")

    profile = AgentProfile(
        name="debt-agent",
        description="Debt registry guard",
        skills=["technical-debt-management"],
        instructions="Track debt.",
    )
    bundle = bundler.bundle_agent(
        profile=profile,
        target="prompt",
        project_root=project_root,
    )
    # блок mode/mcp.md содержит инструкции MCP-инструментов, не файлового режима
    assert "check_known_debt" in bundle
    assert "record_debt" in bundle
    assert "offline" not in bundle.lower()


def test_bundle_agent_pyproject_only_project_gets_default_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6 (default-блок): без mcp-маркеров подставляется mode/default.md (2274, не 1365)."""
    if not CATALOG_SKILLS_DIR.is_dir():
        pytest.skip(f"Каталог скиллов {CATALOG_SKILLS_DIR} недоступен")

    _isolate_mode_env(monkeypatch)
    storage = SkillsStorage(tmp_path / "skills.db")
    SkillsSynchronizer(storage, [CATALOG_SKILLS_DIR]).sync()
    bundler = AgentBundler(storage=storage, compiler=SlotCompiler(storage))

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text(
        "[project]\nname = 'bundle-default-test'\n", encoding="utf-8"
    )

    profile = AgentProfile(
        name="debt-agent",
        description="Debt registry guard",
        skills=["technical-debt-management"],
        instructions="Track debt.",
    )
    bundle = bundler.bundle_agent(
        profile=profile,
        target="prompt",
        project_root=project_root,
    )
    # default.md — «адаптивный/гибридный» режим (зонд), mcp.md — «Режим MCP (mcp_audit)»
    assert "Audit Probe" in bundle
    assert "Режим MCP (mcp_audit)" not in bundle


def test_bundle_agent_touches_predefined_by_override_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6 (фикстурный каталог): bundle_agent подставляет блоки минимального скилла.

    Не зависит от внешнего каталога: скилл с блоком python.md собирается
    в бандл целиком (маркер зачищается, контент блока остаётся).
    """
    _isolate_mode_env(monkeypatch)
    skills_dir = tmp_path / "skills"
    (skills_dir / "demo" / "blocks" / "implementation").mkdir(parents=True)
    (skills_dir / "demo" / "SKILL.md").write_text(
        """---
name: demo
title: Demo
category: general
available_slots:
  - implementation
---
# Demo

<!-- SLOT: implementation -->

Body.
""",
        encoding="utf-8",
    )
    (skills_dir / "demo" / "blocks" / "implementation" / "python.md").write_text(
        "PYTHON-BLOCK-MARKER", encoding="utf-8"
    )
    storage = SkillsStorage(tmp_path / "skills.db")
    SkillsSynchronizer(storage, [skills_dir]).sync()
    bundler = AgentBundler(storage=storage, compiler=SlotCompiler(storage))

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname = 'demo-app'\n", encoding="utf-8")

    profile = AgentProfile(
        name="demo-agent",
        description="Demo guard",
        skills=["demo"],
        instructions="Demo.",
    )
    bundle = bundler.bundle_agent(
        profile=profile,
        target="prompt",
        project_root=project_root,
    )
    assert "PYTHON-BLOCK-MARKER" in bundle
    assert "SLOT:" not in bundle
