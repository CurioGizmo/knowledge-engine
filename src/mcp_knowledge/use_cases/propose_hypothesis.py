"""Use Case: фиксация рабочей гипотезы (Investigation Recording)."""

from pathlib import Path
from typing import Any

from mcp_knowledge.content_gate import (
    REPORT_REJECTION_MESSAGE,
    detect_report_like_content,
)
from mcp_knowledge.errors import (
    DuplicateCardError,
    ReportLikeContentError,
    StorageUnavailableError,
)
from mcp_knowledge.messages import format_report_confirmation
from mcp_knowledge.models import HypothesisRecordRequest
from mcp_knowledge.parser import (
    serialize_hypothesis_markdown,
)
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.synchronizer import KnowledgeSynchronizer
from mcp_knowledge.use_cases._common import _safe_index, category_value, generate_card_id


class ProposeHypothesisUseCase:
    """Use case для фиксации гипотезы с дедупликацией и индексацией."""

    def __init__(
        self,
        repo: CardRepository,
        storage: KnowledgeStorage,
        synchronizer: KnowledgeSynchronizer,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.synchronizer = synchronizer

    def _check_duplicate(self, req: HypothesisRecordRequest) -> str | None:
        """Проверяет наличие похожих активных гипотез."""
        dup_warning: str | None = None
        try:
            dup_id = self.storage.check_duplicate_hypothesis(req)
            if dup_id:
                raise DuplicateCardError(
                    f"Обнаружена похожая активная гипотеза с похожим симптомом: '{dup_id}'."
                )
        except StorageUnavailableError as err:
            dup_warning = f"проверка дубликатов пропущена: {err}"
        return dup_warning

    def _write_hypothesis(self, card_id: str, req: HypothesisRecordRequest) -> Path:
        """Сохраняет markdown-файл гипотезы на диск."""
        cat_val = category_value(req.category)
        markdown_content = serialize_hypothesis_markdown(card_id=card_id, req=req)
        return self.repo.write_new_card(cat_val, card_id, markdown_content)

    def execute(self, req: HypothesisRecordRequest) -> dict[str, Any]:
        """Выполняет полный сценарий фиксации гипотезы."""
        # 0. Проверка фильтрации отчётов (Content Gate)
        # K7: force-путь — двухфазный, как в record.py: force без
        # confirmed_force возвращает confirmation_required, а не reject.
        report_reason = detect_report_like_content(req)
        if report_reason:
            if not req.force:
                raise ReportLikeContentError(
                    f"{REPORT_REJECTION_MESSAGE} Причина: {report_reason}."
                )
            if not req.confirmed_force:
                return {
                    "status": "confirmation_required",
                    "reason": "REPORT_CONFIRMATION_REQUIRED",
                    "message": format_report_confirmation(report_reason),
                }

        # 1. Проверка на дубликаты среди существующих активных гипотез
        dup_warning = self._check_duplicate(req)

        # 2. Генерация идентификатора карточки
        card_id = generate_card_id(self.repo, req)

        # 3. Сохранение файла новой гипотезы на диск
        file_path = self._write_hypothesis(card_id, req)

        # 4. Синхронизация индекса FTS5
        sync_warning = _safe_index(self.synchronizer, card_id, file_path, "гипотеза")

        response_data: dict[str, Any] = {
            "status": "proposed",
            "id": card_id,
            "path": str(file_path),
        }
        all_warnings = [w for w in (dup_warning, sync_warning) if w]
        if all_warnings:
            response_data["warning"] = "; ".join(all_warnings)
        return response_data
