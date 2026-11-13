"""R7 (ревизия 4): одна форма requires_servers + честный fallback.

Контракты (правки ревьюера к docs/analysis/2026-10-18-post-implementation-review.md):

1. Метаданные блока/скилла — ТОЛЬКО ``requires_servers: [имена]`` (list).
   Список означает «нужны ВСЕ перечисленные серверы». Форма dict
   (slot → [имена]) отклоняется при парсинге — неоднозначность
   «лишняя» в ревизии 4. Серверные зависимости хранятся отдельно от
   технологических осей stack; ось «integration server» не добавляется.
2. absent-сервер → блок offline/default. Если такого блока НЕТ —
   ПОНЯТНАЯ ОШИБКА о невозможности сборки (не молчаливый пропуск
   обязательных инструкций через missing-заглушку).
3. unknown (MCP_ACTIVE_SERVERS не задан) → mcp-блок + предупреждение
   В ОТВЕТЕ ИНСТРУМЕНТА (assembly.sections), не только в логах.
4. Нет requires_servers → прежнее поведение (проверка не применяется).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_skills import project_context as pc_module
from mcp_skills.parser import parse_skill_markdown
from mcp_skills.server import load_skill
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


SKILL_LIST_FORM_MD = """---
name: writing-plans
title: Writing Plans
category: planning
available_slots: [integration]
requires_servers: [audit-engine, knowledge-engine]
---
# Writing Plans

<!-- SLOT: integration -->

Follow checklist.
"""

SKILL_NO_SERVERS_MD = """---
name: plain-skill
title: Plain Skill
category: general
available_slots: [integration]
---
# Plain Skill

<!-- SLOT: integration -->

Follow checklist.
"""


def _write_skill(
    skills_dir: Path, name: str, template: str, blocks: dict[str, str] | None = None
) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(template, encoding="utf-8")
    for fname, text in (blocks or {}).items():
        blocks_dir = skill_dir / "blocks" / "integration"
        blocks_dir.mkdir(parents=True, exist_ok=True)
        (blocks_dir / fname).write_text(text, encoding="utf-8")


def _make_catalog(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer, Path]:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)

    _write_skill(
        skills_dir,
        "writing-plans",
        SKILL_LIST_FORM_MD,
        {
            "mcp.md": "### Integration MCP\nCall audit-engine via MCP.",
            "offline.md": "### Integration offline\nFile-based protocol.",
        },
    )
    _write_skill(
        skills_dir,
        "plain-skill",
        SKILL_NO_SERVERS_MD,
        {"mcp.md": "### Integration MCP (plain)\nNo server deps."},
    )
    # Скилл с requires_servers, но mcp-only слотом (нет offline/default)
    mcp_only_with_rs = SKILL_LIST_FORM_MD.replace("name: writing-plans", "name: strict-mcp")
    _write_skill(
        skills_dir,
        "strict-mcp",
        mcp_only_with_rs,
        {"mcp.md": "### Integration MCP (strict)\nRequires all servers."},
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    return storage, sync, tmp_path


@pytest.fixture(autouse=True)
def _isolated_context_cache():
    pc_module._CONTEXT_CACHE.clear()
    yield
    pc_module._CONTEXT_CACHE.clear()


@pytest.fixture
def catalog(tmp_path: Path):
    return _make_catalog(tmp_path)


# --- 1. Форма метаданных: только list -------------------------------------


def test_r7b_dict_form_rejected_at_parse() -> None:
    """Ревизия 4: dict-форма requires_servers отклоняется при парсинге."""
    from mcp_skills.errors import InvalidSkillFormatError

    md = SKILL_LIST_FORM_MD.replace(
        "requires_servers: [audit-engine, knowledge-engine]",
        "requires_servers:\n  integration: [audit-engine]",
    )
    with pytest.raises(InvalidSkillFormatError, match="requires_servers"):
        parse_skill_markdown(md)


def test_r7b_csv_string_form_rejected_at_parse() -> None:
    """MINOR-4: CSV-строка отклоняется — единственная форма список, не строка.

    Ревизия 4: parser молча принимал ``requires_servers: "audit-engine, "```
    и разбирал её в список — форма не заявлена контрактом и скрывает
    нестрогие данные. Строгий list-only: строка = ошибка формата.
    """
    from mcp_skills.errors import InvalidSkillFormatError

    md = SKILL_LIST_FORM_MD.replace(
        "requires_servers: [audit-engine, knowledge-engine]",
        'requires_servers: "audit-engine, knowledge-engine"',
    )
    with pytest.raises(InvalidSkillFormatError, match="requires_servers"):
        parse_skill_markdown(md)


def test_r7b_list_form_parsed_as_list() -> None:
    """Список = «нужны все перечисленные»; парсится в list[str]."""
    card, _ = parse_skill_markdown(SKILL_LIST_FORM_MD)
    assert card.requires_servers == ["audit-engine", "knowledge-engine"]


def test_r7b_list_semantics_all_servers_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Список = «нужны ВСЕ перечисленные»: одного present-сервера мало.

    Скилл требует [audit-engine, knowledge-engine]; в MCP_ACTIVE_SERVERS
    только audit-engine → knowledge-engine остаётся absent → mcp-кандидат
    отклоняется (offline-фолбэк проверен поведенчески в r7b_absent-тестах).
    """
    from mcp_skills.slot_compiler import _slot_required_servers

    card, _ = parse_skill_markdown(SKILL_LIST_FORM_MD)
    assert card.requires_servers == ["audit-engine", "knowledge-engine"]
    # Список применяется без слотовой адресации (dict-форма отвергнута)
    assert _slot_required_servers(card) == ["audit-engine", "knowledge-engine"]

    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine")
    ctx = pc_module.get_project_context(tmp_path)
    assert ctx.server_availability("knowledge-engine") == "absent"
    assert ctx.server_availability("audit-engine") == "present"


# --- 2. absent: fallback offline/default, иначе ошибка ---------------------


def test_r7b_absent_server_offline_fallback_and_reason(
    catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """absent → offline-блок + причина (server_reason) в assembly-ответе."""
    storage, sync, root = catalog
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    res = load_skill(
        "writing-plans",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert "Integration offline" in res["content"]
    assert "Integration MCP" not in res["content"]
    sections = res["assembly"]["sections"]
    assert sections["integration"]["server_reason"] is not None
    assert "audit-engine" in sections["integration"]["server_reason"]


def test_r7b_absent_server_no_offline_block_is_error(
    catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """absent + нет offline/default блока → ошибка сборки (не missing-заглушка).

    Молчаливая missing-заглушка «> ⚠️ [Спецификация отсутствует]» —
    недопустима для обязательных интеграционных инструкций: потребитель не
    узнаёт, что скилл собран без обязательного блока.
    """
    from mcp_skills.errors import MissingRequiredBlockError

    storage, sync, root = catalog
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    with pytest.raises(MissingRequiredBlockError, match="offline|default|сервер"):
        load_skill(
            "strict-mcp",
            stack=["mcp"],
            storage=storage,
            synchronizer=sync,
            project_root=root,
        )


# --- 3. unknown: предупреждение в ответе ----------------------------------


def test_r7b_unknown_servers_warning_in_response(catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    """unknown → mcp-блок И предупреждение в ответе инструмента (не только лог)."""
    storage, sync, root = catalog
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    res = load_skill(
        "writing-plans",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert "Integration MCP" in res["content"]
    sections = res["assembly"]["sections"]
    reason = sections["integration"].get("server_warning")
    assert reason is not None, (
        "unknown: предупреждение обязано присутствовать в ответе инструмента "
        "(assembly.sections.server_warning), не только в логах"
    )
    assert "audit-engine" in reason


# --- 4. Нет requires_servers → прежнее поведение --------------------------


def test_r7b_no_requires_servers_previous_behavior(
    catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет requires_servers → mcp-кандидат без проверок (как до R7)."""
    storage, sync, root = catalog
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")  # audit-engine absent
    res = load_skill(
        "plain-skill",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
        touches=["integration"],
    )
    assert "Integration MCP (plain)" in res["content"]
    sections: dict[str, Any] = res["assembly"]["sections"]
    assert "server_reason" not in sections["integration"]


def test_r7b_one_of_two_servers_present_offline_assembly(
    catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NIT-8: partial-present — absent-сервер в списке требует все → offline.

    Реальная сборка: writing-plans требует [audit-engine, knowledge-engine],
    MCP_ACTIVE_SERVERS="audit-engine" (только один из двух). Список =
    «нужны ВСЕ», значит mcp-кандидат отклоняется целиком → offline-блок.
    """
    storage, sync, root = catalog
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine")
    res = load_skill(
        "writing-plans",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert "Integration offline" in res["content"]
    assert "Integration MCP" not in res["content"]
    sections = res["assembly"]["sections"]
    assert "knowledge-engine" in (sections["integration"].get("server_reason") or "")
