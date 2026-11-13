"""Use Case: создание и запись новой карточки знаний."""

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from mcp_knowledge.content_gate import (
    REPORT_REJECTION_MESSAGE,
    detect_report_like_content,
)
from mcp_knowledge.coordinator import CardMutationCoordinator
from mcp_knowledge.errors import (
    CardNotFoundError,
    DuplicateCardError,
    ReportLikeContentError,
    StorageUnavailableError,
    UserConfirmedConflictError,
)
from mcp_knowledge.logger import logger
from mcp_knowledge.messages import (
    format_draft_hint,
    format_duplicate_confirmation,
    format_duplicate_rejection,
    format_report_confirmation,
    format_user_confirmed_conflict,
)
from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    KnowledgeSource,
    KnowledgeStatus,
)
from mcp_knowledge.parser import (
    append_log_entry,
    serialize_card_markdown,
)
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.synchronizer import KnowledgeSynchronizer
from mcp_knowledge.use_cases._common import _safe_index, category_value, generate_card_id


class RecordKnowledgeUseCase:
    """Use case для создания новой карточки знаний с валидацией, дедупликацией и вытеснением."""

    def __init__(
        self,
        repo: CardRepository,
        storage: KnowledgeStorage,
        synchronizer: KnowledgeSynchronizer,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.synchronizer = synchronizer

    def _check_supersedes_guardrail(self, req: CardRecordRequest) -> None:
        """Проверяет существование вытесняемой карточки и барьер user_confirmed.

        K4: чтение карточки здесь — только для БАРЬЕРА (fail-fast до записи
        новой). Снимок НЕ переносится в _apply_supersede: там карточка
        перечитывается под card_lock — иначе параллельные изменения
        вытесняемой карточки затираются устаревшим снимком.
        """
        if not req.supersedes:
            return
        old_card_file = self.repo.find_card_file(req.supersedes)
        if not old_card_file:
            raise CardNotFoundError(f"Замещаемая карточка '{req.supersedes}' не найдена")

        old_fm, _ = self.repo.read_card_file(old_card_file)
        if old_fm.is_human_protected and req.source == KnowledgeSource.AUTO_INFERRED:
            raise UserConfirmedConflictError(format_user_confirmed_conflict(req.supersedes))

    def _check_duplicate(self, req: CardRecordRequest) -> tuple[str | None, dict[str, Any] | None]:
        """Проверяет карточку на дубликаты через Anti-Spam Gate."""
        dup_warning: str | None = None
        dup_id: str | None = None
        try:
            dup_id = self.storage.check_duplicate(req)
        except StorageUnavailableError as err:
            dup_warning = f"проверка дубликатов пропущена: {err}"

        if dup_id and dup_id != req.supersedes:
            if not req.force:
                raise DuplicateCardError(format_duplicate_rejection(dup_id))
            if req.force and not req.confirmed_force:
                existing_snippet = ""
                existing_file = self.repo.find_card_file(dup_id)
                if existing_file:
                    try:
                        _, sec = self.repo.read_card_file(existing_file)
                        existing_snippet = (
                            sec.get("Симптом / Контекст") or sec.get("Symptom / Context") or ""
                        ).strip()[:200]
                    except Exception as err:
                        logger.debug("Не удалось прочитать сниппет дубликата %s: %s", dup_id, err)

                return dup_warning, {
                    "status": "confirmation_required",
                    "reason": "DUPLICATE_CONFIRMATION_REQUIRED",
                    "existing_id": dup_id,
                    "existing_snippet": existing_snippet,
                    "message": format_duplicate_confirmation(dup_id, existing_snippet),
                }

        return dup_warning, None

    def _write_card(self, card_id: str, req: CardRecordRequest) -> tuple[Path, CardRecordRequest]:
        """Сохраняет новую карточку на диск."""
        clean_related = [r for r in req.related_to if r != card_id]
        clean_req = req.model_copy(update={"related_to": clean_related})

        cat_val = category_value(clean_req.category)
        markdown_content = serialize_card_markdown(card_id=card_id, req=clean_req)
        file_path = self.repo.write_new_card(cat_val, card_id, markdown_content)
        return file_path, clean_req

    def _apply_supersede(
        self,
        card_id: str,
        supersedes_id: str,
        old_card_file: Path | None = None,
        old_fm: CardFrontmatter | None = None,
        old_sections: dict[str, str] | None = None,
        req_source: KnowledgeSource = KnowledgeSource.AUTO_INFERRED,
        new_file_path: Path | None = None,
    ) -> str | None:
        """Помечает старую версию карточки как superseded.

        K4: карточка перечитывается ПОД card_lock — параллельные мутации
        (append_note и т.п.) не затираются устаревшим снимком, прочитанным
        до захвата лока. Аргументы-снимки сохранены в сигнатуре для
        обратной совместимости кастомного supersede_handler, но
        игнорируются (передаются как None из execute).
        """
        if not supersedes_id:
            return None
        try:
            # AUDIT-ARCH-2026-10 (шаг 2): полный RMW-цикл через
            # CardMutationCoordinator (lock → lookup под локом → read →
            # mutate → atomic write → index). Аргументы-снимки игнорируются.
            coordinator = CardMutationCoordinator(self.repo, self.synchronizer)

            def _mark_superseded(fm: CardFrontmatter, sections: Any) -> None:
                if fm.is_human_protected and req_source == KnowledgeSource.AUTO_INFERRED:
                    raise UserConfirmedConflictError(format_user_confirmed_conflict(supersedes_id))
                fm.status = KnowledgeStatus.SUPERSEDED
                fm.superseded_by = card_id
                today_str = date.today().isoformat()
                append_log_entry(sections, f"- [{today_str}] SUPERSEDED: Replaced by {card_id}")

            coordinator.mutate(supersedes_id, _mark_superseded)
            return None
        except CardNotFoundError:
            return (
                f"Карточка '{card_id}' создана, но старая версия "
                f"'{supersedes_id}' не найдена при вытеснении"
            )
        except UserConfirmedConflictError:
            if new_file_path is not None:
                new_file_path.unlink(missing_ok=True)
            raise
        except Exception as err:
            logger.error("Сбой обновления вытесненной карточки '%s': %s", supersedes_id, err)
            return (
                f"Карточка '{card_id}' создана, но старая версия '{supersedes_id}' "
                f"не помечена вытесненной: {err}"
            )

    def _sync_related_links(self, card_id: str, related_to: list[str]) -> list[str]:
        """Синхронизирует двусторонние ссылки related_to.

        K4: RMW чужих карточек — под per-card локами, захваченными в
        детерминированном порядке (сортировка по card_id — защита от
        deadlock при пересекающихся наборах карточек). Контракт атомарности
        многокарточной операции — best-effort: сбой обновления отдельной
        карточки не откатывает остальные, а возвращается списком
        partial_update_warnings.
        """
        warnings: list[str] = []
        # K4: детерминированный порядок захвата локов (sorted — защита
        # от deadlock); каждая карточка — полный RMW-цикл coordinator'а
        coordinator = CardMutationCoordinator(self.repo, self.synchronizer)
        for rel_id in sorted(related_to):
            if rel_id == card_id:
                continue
            try:
                coordinator.mutate(
                    rel_id,
                    lambda fm, _sections: (
                        fm.related_to.append(card_id) if card_id not in fm.related_to else None
                    ),
                )
            except CardNotFoundError:
                # карточки нет — не partial update, а отсутствующая связь
                continue
            except Exception as err:
                logger.warning("Не удалось обновить связь related_to для %s: %s", rel_id, err)
                warnings.append(f"Связь related_to с '{rel_id}' не синхронизирована: {err}")
        return warnings

    def execute(
        self,
        req: CardRecordRequest,
        supersede_handler: Callable[..., str | None] | None = None,
    ) -> dict[str, Any]:
        # 0. Защита карантина черновиков (Draft Quarantine):
        # Автономные карточки агентов (source=auto_inferred) всегда создаются со статусом draft
        if req.source != KnowledgeSource.USER_CONFIRMED:
            req = req.model_copy(update={"status": KnowledgeStatus.DRAFT})

        # 1. Проверка вытеснения и защитного барьера (user_confirmed)
        # K4: снимок не переносится — _apply_supersede читает под card_lock
        self._check_supersedes_guardrail(req)

        # 2. Проверка жанра контента (Content Gate: защита от отчётов о работе и чеклистов)
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

        # 3. Проверка на дубликаты (Anti-Spam Gate)
        dup_warning, confirmation_response = self._check_duplicate(req)
        if confirmation_response is not None:
            return confirmation_response

        # 4. Генерация идентификатора карточки
        card_id = generate_card_id(self.repo, req)

        # 4. Сохранение файла новой карточки на диск
        file_path, clean_req = self._write_card(card_id, req)

        # 5. Вытеснение старой карточки (если указано)
        if supersede_handler:
            try:
                supersede_warning = supersede_handler(
                    card_id,
                    req.supersedes or "",
                    req_source=clean_req.source,
                    new_file_path=file_path,
                )
            except TypeError:
                supersede_warning = supersede_handler(card_id, req.supersedes or "")
        else:
            supersede_warning = self._apply_supersede(
                card_id,
                req.supersedes or "",
                req_source=clean_req.source,
                new_file_path=file_path,
            )

        if supersede_warning:
            cat_val = category_value(clean_req.category)
            try:
                conflicts = self.storage.check_active_card_consistency(category=cat_val)
                if conflicts:
                    logger.warning(
                        "Обнаружены потенциально конфликтующие активные карточки в категории %s: %s",
                        cat_val,
                        conflicts,
                    )
            except Exception as err:
                logger.debug("Проверка консистентности активных карточек пропущена: %s", err)

        # 6. Двусторонняя синхронизация связей related_to
        related_warnings: list[str] = []
        if clean_req.related_to:
            related_warnings = self._sync_related_links(
                card_id=card_id, related_to=clean_req.related_to
            )

        # 7. Синхронизация индекса FTS5
        sync_warning = _safe_index(self.synchronizer, card_id, file_path, "карточка")

        if clean_req.source == KnowledgeSource.USER_CONFIRMED:
            card_status = (
                clean_req.status.value
                if isinstance(clean_req.status, KnowledgeStatus)
                else (
                    str(clean_req.status).strip().lower()
                    if clean_req.status is not None
                    else "active"
                )
            )
        else:
            # Автономные карточки агентов безусловно имеют статус draft
            card_status = "draft"

        response_data: dict[str, Any] = {
            "status": "created",
            "id": card_id,
            "path": str(file_path),
            "card_status": card_status,
        }
        if card_status == "draft":
            response_data["hint"] = format_draft_hint(card_id)
        partial_update_warnings = list(related_warnings)
        all_warnings = [
            w for w in (dup_warning, supersede_warning, sync_warning) if w
        ] + partial_update_warnings
        if all_warnings:
            response_data["warning"] = "; ".join(all_warnings)

        return response_data
