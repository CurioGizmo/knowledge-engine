"""Общие приватные хелперы для Use Cases MCP Knowledge Engine.

Выделены из дублированного кода record.py / propose_hypothesis.py /
update_status.py / resolve_hypotheses.py (DRY-консолидация, аудит v1).
"""

from __future__ import annotations

import sqlite3
from collections.abc import MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING

from mcp_knowledge.errors import (
    CardNotFoundError,
    ForbiddenUserConfirmedActionError,
    StorageUnavailableError,
)
from mcp_knowledge.logger import logger
from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    HypothesisRecordRequest,
    HypothesisResolution,
    KnowledgeCategory,
    KnowledgeStatus,
)
from mcp_knowledge.parser import generate_card_slug

if TYPE_CHECKING:
    from mcp_knowledge.repository import CardRepository
    from mcp_knowledge.synchronizer import KnowledgeSynchronizer


def category_value(category: KnowledgeCategory | str) -> str:
    """Возвращает строковое имя категории: значение StrEnum для KnowledgeCategory, как есть для str."""
    if isinstance(category, KnowledgeCategory):
        return category.value
    return str(category)


def generate_card_id(repo: CardRepository, req: CardRecordRequest | HypothesisRecordRequest) -> str:
    """Генерирует уникальный slug-идентификатор карточки через репозиторий."""
    existing_ids = repo.get_existing_ids()
    return generate_card_slug(
        category=req.category,
        title=req.title,
        custom_slug=req.slug,
        existing_ids=existing_ids,
    )


def _safe_index(
    synchronizer: KnowledgeSynchronizer,
    card_id: str,
    file_path: Path,
    subject: str,
) -> str | None:
    """Точечно индексирует карточку, возвращая warning вместо исключения при сбое FTS5."""
    try:
        synchronizer.index_single_card(card_id, file_path)
        return None
    except (sqlite3.OperationalError, StorageUnavailableError) as err:
        logger.warning(
            "Сбой синхронизации FTS5 индекса после создания %s '%s': %s", subject, card_id, err
        )
        return f"{subject} сохранен на диск, но обновление индекса отложено: {err}"


def _validate_merge_target(
    repo: CardRepository,
    res: HypothesisResolution,
) -> tuple[Path, CardFrontmatter, MutableMapping[str, str]]:
    """Валидирует целевую карточку слияния (существование, не superseded, не user_confirmed).

    Возвращает (target_file, target_fm, target_sections). Используется двумя проходами
    resolve_hypotheses: Pass 1 (validate) отбрасывает sections, Pass 2 (apply) — использует.
    """
    if res.merge_into == res.id:
        raise ValueError(f"Нельзя объединить гипотезу '{res.id}' саму в себя.")

    merge_into = res.merge_into
    if merge_into is None:
        raise ValueError(f"Не указана целевая карточка для слияния гипотезы '{res.id}'.")
    target_file = repo.find_card_file(merge_into)
    if not target_file:
        raise CardNotFoundError(f"Целевая карточка для слияния '{merge_into}' не найдена")
    target_fm, target_sections = repo.read_card_file(target_file)
    if target_fm.status == KnowledgeStatus.SUPERSEDED:
        raise ValueError(
            f"Целевая карточка '{merge_into}' вытеснена карточкой '{target_fm.superseded_by}' "
            "и не может принимать новые данные. Укажите актуальную карточку для слияния."
        )
    if target_fm.is_human_protected:
        raise ForbiddenUserConfirmedActionError(
            f"Целевая карточка '{res.merge_into}' подтверждена пользователем (user_confirmed) "
            "и не может быть изменена через авто-слияние гипотезы."
        )
    return target_file, target_fm, target_sections
