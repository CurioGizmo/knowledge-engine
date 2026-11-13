"""Ф0 (core-dedup v4): golden-инварианты сериализаторов (round-trip).

Инвариант (план, кейс 19): dump(parse(raw)) == dump(parse(dump(parse(raw))))
— идемпотентная каноникализация. Byte-exact против исходного raw невозможен
(сериализаторы каноникализируют), поэтому пиним:
1. round-trip идемпотентность для канонических карточек;
2. byte-stable: повторная сериализация той же модели даёт те же байты;
3. yaml-параметры дампа закреплены (мутация M6: default_flow_style=True
   обязана краснить — см. docs/testing/mutation-log.md).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mcp_audit.parser import (
    parse_audit_markdown,
    serialize_audit_markdown,
)
from mcp_knowledge.parser import (
    parse_card_markdown,
    serialize_card_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
AGENTS = FIXTURES / "corpus" if (FIXTURES / "corpus").is_dir() else REPO_ROOT / ".agents"
PINNED_AUDIT_IDS = {
    "aud-backend-106a",
    "aud-backend-0492",
    "aud-backend-1191",
    "aud-backend-4044",
    "aud-backend-466c",
    "aud-backend-479a",
    "aud-backend-5774",
    "aud-backend-a0c8",
}


@pytest.fixture(scope="module")
def knowledge_cards() -> list[Path]:
    return sorted((AGENTS / "knowledge").rglob("*.md"))[:8]


@pytest.fixture(scope="module")
def audit_cards() -> list[Path]:
    golden = json.loads((FIXTURES / "golden-serializers.json").read_text(encoding="utf-8"))
    ids = {"-".join(Path(rel).stem.split("-")[:3]) for rel in golden}
    assert len(golden) == len(PINNED_AUDIT_IDS) and ids == PINNED_AUDIT_IDS, (
        "golden-набор audit-карточек изменён; обнови независимый manifest намеренно"
    )
    cards = [AGENTS / rel for rel in sorted(golden)]
    assert all(card.is_file() for card in cards), "golden-карточка отсутствует в checkout"
    return cards


@pytest.mark.parametrize("which", ["audit"])
def test_serializers_roundtrip_idempotent(which: str, audit_cards: list[Path]) -> None:
    """dump(parse(raw)) == dump(parse(dump(parse(raw)))) для реальных карточек.

    Knowledge-сериализатор принимает CardRecordRequest, а не CardFrontmatter
    (не round-trip пара) — его byte-stable-контракт закрывает
    test_serializer_yaml_params_pinned ниже.
    """
    cards = audit_cards
    for card in cards:
        raw = card.read_text(encoding="utf-8")
        issue1 = parse_audit_markdown(raw, card)
        once = serialize_audit_markdown(issue1)
        issue2 = parse_audit_markdown(once, card)
        twice = serialize_audit_markdown(issue2)
        assert once == twice, (
            f"{card.name}: сериализация неидемпотентна — dump(parse(dump(parse(raw)))) разошёлся"
        )


def test_serializer_golden_bytes_pinned(audit_cards: list[Path]) -> None:
    """Golden-байты сериализации (ревью Ф2-5): ожидаемый РЕЗУЛЬТАТ, не только свойства.

    Идемпотентность + поиск строк в исходнике пропускали повреждение данных
    (сериализатор, возвращающий другую карточку, проходил). Пиним sha256
    сериализованного дампа на карточку в fixtures/golden-serializers.json.
    Любое изменение вывода (поля, порядок, формат) — дифф хеша: либо регрессия,
    либо плановый дифф Ф3 (обновить фикстуру в том же коммите).
    """
    golden = json.loads((FIXTURES / "golden-serializers.json").read_text(encoding="utf-8"))
    for card in audit_cards:
        rel = card.relative_to(AGENTS).as_posix()
        raw = card.read_text(encoding="utf-8")
        issue = parse_audit_markdown(raw, card)
        once = serialize_audit_markdown(issue)
        payload = once.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        assert digest == golden[rel]["sha256"], (
            f"{rel}: байты сериализации изменились (sha256) — регрессия или "
            "плановый дифф Ф3? обнови fixtures/golden-serializers.json тем же коммитом"
        )
        assert len(payload) == golden[rel]["bytes"]


def test_serializer_yaml_params_pinned() -> None:
    """Пин канонических параметров YAML-дампа (мутация M6).

    С Ф3-финал knowledge-сериализатор делегирует core dump_frontmatter:
    пиним источник core-модуля (единая точка параметров для всех пакетов)
    — «грубый, но машино-исполнимый страж против тихой смены формата».
    """
    src = (REPO_ROOT / "src" / "mcp_core" / "card_codec.py").read_text(encoding="utf-8")
    assert "default_flow_style=False" in src, (
        "default_flow_style=False пропал из dump_frontmatter — формат дампа "
        "изменился (intended? приложи плановый дифф)"
    )
    assert "sort_keys=False" in src
    assert "allow_unicode=True" in src
    # Делегирование сериализатора пакетов core-примитиву (Ф3-финал):
    knowledge_parser = (REPO_ROOT / "src" / "mcp_knowledge" / "parser.py").read_text(
        encoding="utf-8"
    )
    assert "dump_frontmatter" in knowledge_parser, (
        "knowledge._dump_yaml перестал делегировать core dump_frontmatter — "
        "CRLF-нормализация (кейс 21) больше не применяется на записи"
    )
