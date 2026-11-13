"""Tests for plan B (search-and-data-integrity followup): promote/human consent contract.

Проверяет (TDD, пункт B плана docs/plans/2026-10-18-search-and-data-integrity-followup.md):
1. promote без note проходит через настоящий MCP tools/call (in-process call_tool) и получает default-note.
2. Другие действия (suspect/verify/dispute/append_note) без note отклоняются ошибкой валидации.
3. Draft-карантин hint учит агента запрашивать согласие человека, а не вызывать promote с by='human' самостоятельно.
4. confirm auto_inferred-гипотезы остаётся в карантине (draft) без human-гейта; user_confirmed — как раньше.
5. Промоушен не перезаписывает происхождение (generated.by/confidence); факт согласования фиксируется в verified-записи.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from mcp_knowledge.messages import DRAFT_QUARANTINE_HINT_TEMPLATE, format_draft_hint
from mcp_knowledge.models import (
    CardRecordRequest,
    CardStatusUpdateRequest,
    HypothesesResolveRequest,
    HypothesisRecordRequest,
    HypothesisResolution,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import parse_card_markdown
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.use_cases.review import VERDICT_ACTIONS


@pytest.fixture
def env(tmp_path: Path):
    k_dir = tmp_path / "knowledge"
    cache_db = tmp_path / "cache.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)
    return storage, service, server


def _wire_data(res: CallToolResult) -> dict[str, Any]:
    assert isinstance(res, CallToolResult)
    if res.structured_content:
        return res.structured_content
    return json.loads(res.content[0].text)


async def _record_draft_card(server) -> str:
    res = await server.call_tool(
        "record_knowledge",
        {
            "title": "Wire promote consent card",
            "category": "backend",
            "type": "lesson",
            "source": "auto_inferred",
            "symptom": "Draft card recorded autonomously via wire test",
            "root_cause": "Auto-inferred cards land in draft quarantine before human approval",
            "solution": "Promotion requires explicit human consent transmitted by the caller",
            "rationale": "Quarantine keeps unreviewed agent content out of live search",
            "slug": "wire-promote-consent",
            "task_id": "task-wire-promote",
        },
    )
    data = _wire_data(res)
    assert data["card_status"] == "draft", data
    return data["id"]


@pytest.mark.asyncio
async def test_wire_promote_without_note_uses_default_note(env):
    """1. promote без note через настоящий tools/call принимается и логирует default-note.

    Поведение СОГЛАШЕНИЯ (R3, ADR docs/analysis/adr-promote-consent-boundary.md):
    сервер доверяет строкам source/by от вызывающего — это НЕ проверка
    «защищённости» от самостоятельного promote. Технической границы нет;
    тест фиксирует фактическое поведение сервера при корректно оформленном
    вызове (согласие предполагается полученным на стороне вызывающего).
    """
    _, _, server = env
    card_id = await _record_draft_card(server)

    res = await server.call_tool(
        "update_knowledge_status",
        {"id": card_id, "action": "promote", "source": "user_confirmed", "by": "human"},
    )
    assert not res.is_error, _wire_data(res)
    data = _wire_data(res)
    assert data["status"] == "updated"
    assert data["new_status"] == "active"

    # На диске: default-note попал в лог
    card_file = env_knowledge_dir(env) / "backend" / f"{card_id}.md"
    fm, sections = parse_card_markdown(card_file.read_text(encoding="utf-8"))
    log_content = "\n".join(sections.values())
    assert "PROMOTED" in log_content


def env_knowledge_dir(env) -> Path:
    return env[1].knowledge_dir  # KnowledgeService хранит knowledge_dir


@pytest.mark.asyncio
async def test_wire_suspect_without_note_is_validation_error(env):
    """2. promote default-note не ослабляет требования других действий: suspect без note -> ошибка."""
    _, _, server = env
    card_id = await _record_draft_card(server)

    res = await server.call_tool(
        "update_knowledge_status",
        {"id": card_id, "action": "suspect"},
    )
    assert res.is_error, _wire_data(res)
    data = _wire_data(res)
    assert data.get("error") is True
    assert data.get("status") == "rejected"
    assert data.get("reason") in ("VALIDATION_FAILED", "INVALID_ARGUMENTS", "ANTI_SPAM_REJECTED")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    ["verify", "dispute", "append_note"],
)
async def test_wire_other_actions_without_note_rejected(env, action):
    """2b. Все не-promote действия без note отклоняются валидацией."""
    _, _, server = env
    card_id = await _record_draft_card(server)

    res = await server.call_tool(
        "update_knowledge_status",
        {"id": card_id, "action": action},
    )
    assert res.is_error
    data = _wire_data(res)
    assert data.get("error") is True


def test_draft_hint_requests_consent_instead_of_self_promotion():
    """3. Hint не содержит призыва к самостоятельному promote с by='human'."""
    hint = format_draft_hint("backend-example-card")
    assert "by='human'" not in hint
    assert 'by="human"' not in hint
    # Hint должен направлять к явному согласию пользователя, а не к действию агента
    lowered = hint.lower()
    assert "соглас" in lowered or "аппрув" in lowered or "approv" in lowered


def test_verdict_actions_do_not_teach_self_promotion():
    """3b. VERDICT_ACTIONS из review: promote-вердикт требует согласия, без шаблона самостоятельного by='human'."""
    promote_line = next(line for line in VERDICT_ACTIONS.splitlines() if "promote" in line)
    assert "by='human'" not in promote_line
    # Вердикт требует явного согласия пользователя
    assert "соглас" in promote_line.lower()


def test_promote_preserves_authorship_and_records_approval(env):
    """5. Происхождение сохраняется после promote: generated.by остаётся прежним, confidence не назначается."""
    _, service, _ = env
    resp = service.record(
        CardRecordRequest(
            title="Authorship preservation card",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="Promote overwrote authorship metadata unconditionally",
            root_cause="UpdateStatusUseCase set generated.by=human and confidence=1.0 on promote",
            solution="Keep generated.by and confidence intact; record approval in verified history",
            rationale="Author and approver are different provenance axes",
            slug="authorship-preservation",
            source=KnowledgeSource.AUTO_INFERRED,
        )
    )
    card_id = resp["id"]
    file_path = Path(resp["path"])

    promote_res = service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="promote",
            note="Human approved after review",
            by="human",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    assert promote_res["new_status"] == "active"

    fm, sections = parse_card_markdown(file_path.read_text(encoding="utf-8"))
    # Авторство агента сохранено (не подменено на human)
    assert fm.generated.by.value == "process"
    # confidence не принудительно 1.0
    assert fm.confidence != 1.0
    # Факт согласования зафиксирован в verified-истории
    assert fm.verified and fm.verified[0].by == "human"
    assert fm.verified[0].note and "Human approved" in fm.verified[0].note
    log_content = "\n".join(sections.values())
    assert "PROMOTED: Human approved after review" in log_content


def test_confirm_auto_inferred_hypothesis_stays_in_draft_quarantine(env):
    """4. confirm auto_inferred-гипотезы не выводит её в active: остаётся draft (карантин record-политики)."""
    _, service, _ = env
    prop = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Auto-inferred confirm quarantine hypothesis",
            category="backend",
            symptom="Hypothesis confirmed autonomously bypassed draft quarantine",
            hypothesis="resolve(confirm) promoted auto_inferred hypothesis to active without human gate",
            slug="confirm-quarantine-hypo",
        )
    )
    hypo_id = prop["id"]

    res = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo_id,
                    action="confirm",
                    solution="Confimed solution text long enough for validation gate",
                )
            ]
        )
    )
    assert res["status"] == "resolved"

    card = service.get([hypo_id]).cards[0]
    assert card.status == KnowledgeStatus.DRAFT
    # confidence не назначается автоматически при обходе согласования
    fm_card = service.get([hypo_id]).cards[0]
    assert fm_card.frontmatter.confidence < 0.85

    # Карантин: не видна в дефолтном поиске
    assert len(service.search(query="confirm quarantine hypothesis")) == 0
    # Видна с include_drafts
    draft_hits = service.search(query="hypothesis", include_drafts=True)
    assert any(hit.id == hypo_id for hit in draft_hits)


def test_confirm_user_confirmed_hypothesis_promotes_to_active(env):
    """4b. Согласованный человеком путь: confirm агента -> draft, затем human-promote -> active lesson."""
    _, service, _ = env
    prop = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Human consent flow hypothesis",
            category="backend",
            symptom="Agent-confirmed hypothesis needs human approval to reach active",
            hypothesis="Draft quarantine keeps confirmed hypothesis until explicit promotion",
            slug="human-consent-flow-hypo",
        )
    )
    hypo_id = prop["id"]

    res = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo_id,
                    action="confirm",
                    solution="Valid solution text awaiting human approval",
                )
            ]
        )
    )
    assert res["status"] == "resolved"
    assert res["details"][0]["card_status"] == "draft"

    # Человек явно согласует: promote с source=user_confirmed
    promote_res = service.update_status(
        CardStatusUpdateRequest(
            id=hypo_id,
            action="promote",
            note="User approved this confirmed hypothesis",
            by="human",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    assert promote_res["new_status"] == "active"

    card = service.get([hypo_id]).cards[0]
    assert card.status == KnowledgeStatus.ACTIVE
    assert card.type == KnowledgeType.LESSON


def test_hint_mentions_no_self_promotion_wording():
    """3c. Шаблон подсказки прямо запрещает самостоятельный promote (текст контракта)."""
    template = DRAFT_QUARANTINE_HINT_TEMPLATE.lower()
    # Явный запрет самостоятельного промоушена без согласия пользователя
    assert "never promote" in template
    assert "without actual user consent" in template


def test_docs_state_no_technical_consent_boundary():
    """R3 (ADR): справка и докстринг MCP-инструмента заявляют отсутствие
    технической границы доверия — подставленные source/by не отклоняются.

    Инвариант документации: если текст удалят, контракт «карантин =
    соглашение» перестанет быть заявленным, и пользователи могут
    ошибочно полагать, что promote технически защищён. ADR:
    docs/analysis/adr-promote-consent-boundary.md.
    """
    from mcp_knowledge.help_docs import get_help_text

    help_text = get_help_text("update_knowledge_status")
    assert help_text, "справка update_knowledge_status должна существовать"
    lowered = help_text.lower()
    # Ядро контракта: технической границы нет, сервер не отклоняет подставленные строки
    assert "не отклоняет" in lowered
    assert "технической границы доверия нет" in lowered
    assert "adr-promote-consent-boundary" in lowered

    # Докстринг MCP-инструмента содержит то же заявление (server.py).
    # Инструменты регистрируются вложенными функциями — inspect.getsource
    # самой create_server их не включает, читаем исходник модуля.
    import mcp_knowledge.server as mcp_knowledge_server

    server_module_path = Path(mcp_knowledge_server.__file__)
    server_src = server_module_path.read_text(encoding="utf-8")
    assert "Consent boundary" in server_src, (
        "докстринг update_knowledge_status должен содержать заявление Consent boundary (R3)"
    )

    # ADR-файл существует и фиксирует контракт
    adr_path = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "analysis"
        / "adr-promote-consent-boundary.md"
    )
    assert adr_path.exists(), f"ADR-файл отсутствует: {adr_path}"
    adr_text = adr_path.read_text(encoding="utf-8")
    assert "соглашение о процессе, а не техническая граница доверия" in adr_text.lower()
