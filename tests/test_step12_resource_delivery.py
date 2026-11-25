"""Tests for Step 12: Delivery of references & templates via load_skill (P1 / F2)."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from mcp_core.paths import resolve_skills_repo_dir
from mcp_skills.errors import SkillNotFoundError
from mcp_skills.flow_engine import _extract_stage_resources, step_flow
from mcp_skills.models import FlowStage, FlowStageMeta
from mcp_skills.resources import (
    determine_resource_type,
    extract_anchor_section,
    extract_resources_from_markdown,
    is_resource_associated_with_skill,
    read_resource_chunk,
    resolve_link_to_resource_id,
    resolve_resource_file,
    validate_resource_id,
)
from mcp_skills.server import create_server, load_skill
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


from tests.conftest import get_companion_skills_dir


@pytest.fixture(autouse=True)
def configure_catalog_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    _cat = get_companion_skills_dir()
    repo = _cat if _cat is not None else Path("/home/anton/projects/mcp_skills")
    if not (repo / "skills").is_dir():
        pytest.skip(f"mcp_skills catalog not found at {repo}")
    monkeypatch.setenv("MCP_SKILLS_PATH", str(repo))
    return repo


@pytest.fixture
def catalog_root() -> Path:
    repo = resolve_skills_repo_dir()
    return repo if repo.name != "skills" else repo.parent


@pytest.fixture
def synced_storage(tmp_path: Path, catalog_root: Path) -> SkillsStorage:
    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    skills_dir = catalog_root / "skills"
    sync = SkillsSynchronizer(store, [skills_dir])
    sync.sync()
    return store


def test_schema_exactly_five_tools(tmp_path: Path) -> None:
    """Проверка инварианта: skills-engine экспонирует ровно 5 инструментов."""
    server = create_server(project_root=tmp_path)
    # FastMCP stores tools in server._tool_manager._tools or list_tools
    tool_names = set(server._tool_manager._tools.keys())
    assert tool_names == {
        "search_skills",
        "load_skill",
        "create_skill",
        "get_help",
        "step_flow",
    }
    assert len(tool_names) == 5

    load_tool = server._tool_manager._tools["load_skill"]
    # Check parameters
    param_names = set(load_tool.parameters["properties"].keys())
    assert "name" in param_names
    assert "stack" in param_names
    assert "touches" in param_names
    assert "resource_id" in param_names
    assert "cursor" in param_names


def test_old_load_skill_calls_still_work(
    synced_storage: SkillsStorage, catalog_root: Path, tmp_path: Path
) -> None:
    """Старые вызовы load_skill без resource_id продолжают работать и отдают resources."""
    res = load_skill(
        name="writing-plans",
        stack=["python"],
        storage=synced_storage,
        project_root=tmp_path,
    )
    assert res["name"] == "writing-plans"
    assert "content" in res
    assert "resources" in res
    assert isinstance(res["resources"], list)
    assert len(res["resources"]) > 0
    # Проверяем, что в resources есть standard-plan.template.md
    res_ids = [r["resource_id"] for r in res["resources"]]
    assert any("standard-plan.template.md" in rid for rid in res_ids)


def test_mcp_delivery_api_reference(
    synced_storage: SkillsStorage, catalog_root: Path, tmp_path: Path
) -> None:
    """Доставка API-справочника через load_skill(name, resource_id=...)."""
    res = load_skill(
        name="code-review",
        resource_id="skills/quality-review/code-review/references/api-architecture.md",
        storage=synced_storage,
        project_root=tmp_path,
    )
    assert res["name"] == "code-review"
    assert res["resource_id"] == "skills/quality-review/code-review/references/api-architecture.md"
    assert len(res["content"]) > 100
    assert "API" in res["content"] or "REST" in res["content"]
    assert res["revision"] is not None
    assert res["was_truncated"] is False


def test_mcp_delivery_standard_plan_template_section_24(
    synced_storage: SkillsStorage, catalog_root: Path, tmp_path: Path
) -> None:
    """Доставка стандартного шаблона с подразделом 2.4 по якорю."""
    res = load_skill(
        name="writing-plans",
        resource_id="templates/standard-plan.template.md#2.4",
        storage=synced_storage,
        project_root=tmp_path,
    )
    assert res["name"] == "writing-plans"
    assert "2.4. Пошаговая логика методов" in res["content"]
    assert "2.5. Зависимости" not in res["content"]
    assert res["anchor"] == "2.4"


def test_mcp_delivery_plan_example(
    synced_storage: SkillsStorage, catalog_root: Path, tmp_path: Path
) -> None:
    """Доставка примера плана через load_skill."""
    res = load_skill(
        name="writing-plans",
        resource_id="templates/examples/cli-plan.example.md",
        storage=synced_storage,
        project_root=tmp_path,
    )
    assert res["name"] == "writing-plans"
    assert "CLI" in res["content"]
    assert res["was_truncated"] is False


def test_mcp_delivery_contract_breach(
    synced_storage: SkillsStorage, catalog_root: Path, tmp_path: Path
) -> None:
    """Доставка документа CONTRACT_BREACH через load_skill."""
    res = load_skill(
        name="writing-plans",
        resource_id="docs/contract-breach.md",
        storage=synced_storage,
        project_root=tmp_path,
    )
    assert res["name"] == "writing-plans"
    assert "CONTRACT_BREACH" in res["content"]


def test_mutually_exclusive_parameters(synced_storage: SkillsStorage, tmp_path: Path) -> None:
    """В режиме ресурса stack и touches запрещены (неоднозначный запрос)."""
    with pytest.raises(ValueError, match="Неоднозначный запрос"):
        load_skill(
            name="writing-plans",
            resource_id="templates/standard-plan.template.md",
            stack=["python"],
            storage=synced_storage,
            project_root=tmp_path,
        )

    with pytest.raises(ValueError, match="Неоднозначный запрос"):
        load_skill(
            name="writing-plans",
            resource_id="templates/standard-plan.template.md",
            touches=["backend"],
            storage=synced_storage,
            project_root=tmp_path,
        )


def test_security_traversal_and_absolute_paths(catalog_root: Path) -> None:
    """Защита от directory traversal и абсолютных путей."""
    with pytest.raises(ValueError, match="Directory traversal"):
        validate_resource_id("../../etc/passwd")

    with pytest.raises(ValueError, match="Directory traversal"):
        validate_resource_id("skills/../secrets.txt")

    with pytest.raises(ValueError, match="относительным путем"):
        validate_resource_id("/etc/passwd")

    with pytest.raises(ValueError, match="Недопустимые символы"):
        validate_resource_id("foo\\bar")

    with pytest.raises(ValueError, match="Недопустимые символы"):
        validate_resource_id("foo\0bar")


def test_security_symlink_escape(catalog_root: Path, tmp_path: Path) -> None:
    """Защита от выхода за пределы catalog_root через symlink."""
    fake_catalog = tmp_path / "catalog"
    fake_catalog.mkdir()
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("secret", encoding="utf-8")

    link = fake_catalog / "escape.md"
    link.symlink_to(outside_file)

    with pytest.raises(PermissionError, match="за пределами каталога"):
        resolve_resource_file("escape.md", fake_catalog)


def test_security_unassociated_resource_rejected(
    synced_storage: SkillsStorage, catalog_root: Path, tmp_path: Path
) -> None:
    """Чтение чужого несвязанного ресурса отклоняется с PermissionError."""
    # parallel-feature-development не связан с api-architecture.md
    with pytest.raises(PermissionError, match="не связан со скиллом"):
        load_skill(
            name="parallel-feature-development",
            resource_id="skills/quality-review/code-review/references/api-architecture.md",
            storage=synced_storage,
            project_root=tmp_path,
        )


def test_missing_resource_raises_file_not_found(
    synced_storage: SkillsStorage, catalog_root: Path, tmp_path: Path
) -> None:
    """Несуществующий ресурс, связанный со скиллом (например битая ссылка), дает FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="не найден в каталоге"):
        read_resource_chunk(
            "skills/quality-review/code-review/references/non-existent.md", catalog_root
        )


def test_pagination_and_revision_invalidation(tmp_path: Path) -> None:
    """Курсорная пагинация и инвалидация при изменении файла."""
    cat = tmp_path / "catalog"
    cat.mkdir()
    large_doc = cat / "doc.md"
    large_doc.write_text("A" * 15000, encoding="utf-8")

    # Чтение первого чанка с chunk_size=8000
    chunk1 = read_resource_chunk("doc.md", cat, chunk_size=8000)
    assert chunk1["was_truncated"] is True
    assert chunk1["offset"] == 0
    assert len(chunk1["content"]) == 8000
    assert chunk1["next_cursor"] is not None
    assert isinstance(
        chunk1["next_cursor"], str
    )  # Непрозрачный курсор, привязанный к ресурсу/якорю.

    # Чтение второго чанка
    chunk2 = read_resource_chunk("doc.md", cat, cursor=chunk1["next_cursor"], chunk_size=8000)
    assert chunk2["was_truncated"] is False
    assert chunk2["offset"] == 8000
    assert len(chunk2["content"]) == 7000
    assert chunk2["next_cursor"] is None

    # Изменение файла на диске: ревизия меняется
    large_doc.write_text("B" * 16000, encoding="utf-8")
    with pytest.raises(ValueError, match="изменился с момента последнего чтения"):
        read_resource_chunk("doc.md", cat, cursor=chunk1["next_cursor"], chunk_size=8000)

    # Некорректный курсор
    with pytest.raises(ValueError, match="Некорректный формат курсора"):
        read_resource_chunk("doc.md", cat, cursor="bad-cursor-format")


def test_the_nine_block_links_resolve_correctly(catalog_root: Path) -> None:
    """Все 9 найденных ссылок из вставленных блоков корректно разрешаются в существующие файлы."""
    nine_links = [
        (
            "skills/planning/writing-plans/blocks/integration/default.md",
            "../../references/mcp-plan-integration.md",
            "skills/planning/writing-plans/references/mcp-plan-integration.md",
        ),
        (
            "skills/planning/writing-plans/blocks/integration/default.md",
            "offline.md",
            "skills/planning/writing-plans/blocks/integration/offline.md",
        ),
        (
            "skills/quality-review/code-review/blocks/integration/mcp.md",
            "../../../../planning/writing-plans/references/mcp-plan-integration.md",
            "skills/planning/writing-plans/references/mcp-plan-integration.md",
        ),
        (
            "skills/quality-review/code-review/blocks/invariants/audio-ml.md",
            "../../../../../agents/plan-architect/references/specialized-architects.md",
            "agents/plan-architect/references/specialized-architects.md",
        ),
        (
            "skills/quality-review/code-review/blocks/invariants/python.md",
            "../../references/api-architecture.md",
            "skills/quality-review/code-review/references/api-architecture.md",
        ),
        (
            "skills/quality-review/code-review/blocks/invariants/react.md",
            "../../references/frontend-ui-ux.md",
            "skills/quality-review/code-review/references/frontend-ui-ux.md",
        ),
        (
            "skills/quality-review/code-review/blocks/invariants/sqlite.md",
            "../../references/storage-database.md",
            "skills/quality-review/code-review/references/storage-database.md",
        ),
        (
            "skills/quality-review/code-review/blocks/invariants/tauri.md",
            "../../references/desktop-native-ipc.md",
            "skills/quality-review/code-review/references/desktop-native-ipc.md",
        ),
        (
            "skills/quality-review/code-review/blocks/subsystem-audit/audio-ml.md",
            "../../../../../agents/plan-architect/references/specialized-architects.md",
            "agents/plan-architect/references/specialized-architects.md",
        ),
    ]

    for origin_rel, target_link, expected_res_id in nine_links:
        origin_file = catalog_root / origin_rel
        if not origin_file.is_file():
            continue
        res_id, anchor = resolve_link_to_resource_id(target_link, origin_file, catalog_root)
        assert res_id == expected_res_id, (
            f"Link {target_link} in {origin_rel} resolved to {res_id}, expected {expected_res_id}"
        )
        resolved_file, _ = resolve_resource_file(res_id, catalog_root)
        assert resolved_file.is_file(), f"Resolved target file {resolved_file} must exist on disk"


def test_legacy_plan_execution_auto_redirect_initializes_server_orchestration_state(
    synced_storage: SkillsStorage, tmp_path: Path
) -> None:
    """Legacy auto-вход переводит в unified flow с серверным трекером оркестрации."""
    payload = step_flow(
        action="advance",
        flow="plan-execution-auto-flow",
        project_root=tmp_path,
        skills_storage=synced_storage,
    )

    assert payload["status"] == "active"
    stage = payload["stage"]
    assert payload["requested_flow"] == "plan-execution-auto-flow"
    assert payload["canonical_flow"] == "execute-plan-flow"
    assert stage["id"] == "inspect_plan"
    # Состояние оркестрации создаётся сервером автоматически — ручной шаблон не требуется.
    resources = stage.get("resources") or []
    assert not any("orchestration-state.template.md" in r["resource_id"] for r in resources)
    orch = payload.get("orchestration_state")
    assert orch is not None
    assert (tmp_path / orch["path"]).is_file()


def test_unowned_flow_resource_raises_error(synced_storage: SkillsStorage, tmp_path: Path) -> None:
    """Обязательный ресурс этапа без объявленного владельца вызывает явную ошибку."""
    fake_stage = FlowStage(
        id="bad_stage",
        title="Bad Stage",
        order=1,
        instruction="Используй обязательный шаблон [Unowned](templates/completely-unowned-file.template.md)",
        meta=FlowStageMeta(agent="coder-agent"),
    )

    with pytest.raises(ValueError, match="не имеет объявленного скилла-владельца"):
        _extract_stage_resources(fake_stage, tmp_path, synced_storage)
