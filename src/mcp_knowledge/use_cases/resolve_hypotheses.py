"""Use Case: двухпроходная резолюция гипотез (Pass 1: Validate, Pass 2: Apply)."""

from __future__ import annotations

import sqlite3
from collections.abc import MutableMapping
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp_knowledge.content_gate import detect_report_like_content
from mcp_knowledge.errors import (
    CardNotFoundError,
    DuplicateCardError,
    ForbiddenUserConfirmedActionError,
    ReportLikeContentError,
    StorageUnavailableError,
    UncertainDuplicateError,
)
from mcp_knowledge.logger import logger
from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    GeneratedBy,
    HypothesesResolveRequest,
    HypothesisResolution,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import (
    append_log_entry,
    find_section_key,
)
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.use_cases._common import _validate_merge_target

if TYPE_CHECKING:
    from mcp_knowledge.coordinator import CardMutationCoordinator
    from mcp_knowledge.synchronizer import KnowledgeSynchronizer

# Ревью r4 (п. 3): явный список допустимых статусов цели duplicate_of —
# только карточки-факты. Симметрично критериям кандидатов confirm
# (classify_confirm ищет ACTIVE_STATUS_SQL + DRAFT, куда входит suspect).
# Ревью r6 (тупик suspect): suspect входит в ACTIVE_STATUS_SQL, поэтому
# classify_confirm при confirm блокирует гипотезу как дубль suspect-карточки
# (DuplicateCardError), но whitelist целей не содержал suspect — гипотеза
# не могла ни подтвердиться, ни закрыться дубликатом на ту же карточку.
# SUSPECT добавлен, чтобы снять несимметричность; отклоняются по-прежнему:
# открытые гипотезы, refuted, disputed, superseded.
_DUPLICATE_TARGET_ALLOWED_STATUSES = frozenset(
    {
        KnowledgeStatus.ACTIVE,
        KnowledgeStatus.VERIFIED,
        KnowledgeStatus.SUSPECT,
        KnowledgeStatus.DRAFT,
    }
)


class ResolveHypothesesUseCase:
    """Use case для двухпроходного подведения итогов по гипотезам."""

    def __init__(
        self,
        repo: CardRepository,
        storage: KnowledgeStorage,
        coordinator: CardMutationCoordinator | None = None,
        synchronizer: KnowledgeSynchronizer | None = None,
    ) -> None:
        self.repo = repo
        self.storage = storage
        # Coordinator — опционально: если не передан при создании, лениво
        # строится из synchronizer при первом применении резолюций.
        self._coordinator = coordinator
        self._synchronizer = synchronizer

    def _coordinator_or_none(self) -> CardMutationCoordinator | None:
        """Ленивый доступ к coordinator: построен из synchronizer при наличии."""
        if self._coordinator is None and self._synchronizer is not None:
            from mcp_knowledge.coordinator import CardMutationCoordinator

            self._coordinator = CardMutationCoordinator(
                repo=self.repo, synchronizer=self._synchronizer
            )
        return self._coordinator

    def _validate_duplicate_target(
        self,
        res: HypothesisResolution,
        fm: CardFrontmatter,
        sections: MutableMapping[str, str],
    ) -> None:
        """Pass 1: валидация цели duplicate_of (существование, не-superseded, классификация)."""
        if not res.duplicate_of or not res.duplicate_of.strip():
            raise ValueError(
                f"Для действия 'duplicate' по гипотезе '{res.id}' обязательно поле 'duplicate_of'"
            )
        target_id = res.duplicate_of.strip()
        if target_id == res.id:
            raise ValueError(f"Гипотеза '{res.id}' не может ссылаться на саму себя в duplicate_of")

        target_file = self.repo.find_card_file(target_id)
        if not target_file:
            raise CardNotFoundError(f"Целевая карточка для duplicate_of '{target_id}' не найдена")

        target_fm, target_sections = self.repo.read_card_file(target_file)
        if target_fm.status == KnowledgeStatus.SUPERSEDED or target_fm.superseded_by:
            raise ValueError(
                f"Целевая карточка '{target_id}' уже вытеснена (superseded) и не может быть целью duplicate_of"
            )
        # Ревью r2/r4/r6: цель duplicate_of — только карточка-факт с явно
        # допустимым статусом (см. _DUPLICATE_TARGET_ALLOWED_STATUSES,
        # включая suspect — иначе гипотеза-дубль suspect-карточки не могла
        # закрыться). Отклоняются: открытые гипотезы, refuted, disputed
        # и superseded.
        if target_fm.status not in _DUPLICATE_TARGET_ALLOWED_STATUSES:
            raise ValueError(
                f"Целевая карточка '{target_id}' имеет статус "
                f"'{target_fm.status.value}' и не может быть канонической "
                "целью duplicate_of: сначала подтвердите или закройте её саму."
            )

        sym_key = find_section_key(sections, "симптом", "контекст", "symptom", "context")
        hypo_symptom = sections.get(sym_key, fm.title).strip() if sym_key else fm.title
        rc_key = find_section_key(
            sections,
            "первопричина",
            "root cause",
            "рабочая гипотеза",
            "гипотеза",
            "hypothesis",
        )
        hypo_reason = sections.get(rc_key, "").strip() if rc_key else ""

        target_sym_key = find_section_key(
            target_sections, "симптом", "контекст", "symptom", "context"
        )
        target_symptom = (
            target_sections.get(target_sym_key, target_fm.title).strip()
            if target_sym_key
            else target_fm.title
        )
        target_rc_key = find_section_key(
            target_sections, "первопричина", "root cause", "причина", "гипотеза"
        )
        target_root_cause = target_sections.get(target_rc_key, "").strip() if target_rc_key else ""

        outcome = self.storage.classify_confirm_candidate(
            hypo_symptom=hypo_symptom,
            hypo_reason=hypo_reason,
            cand_symptom=target_symptom,
            cand_root_cause=target_root_cause,
        )
        if outcome != "duplicate":
            raise ValueError(
                f"Карточка '{target_id}' не является подтверждённым дубликатом гипотезы '{res.id}' "
                f"(исход классификации: '{outcome}'). Требуется подтверждённое совпадение симптома и причины."
            )

    def _validate_resolution_item(
        self, res: HypothesisResolution
    ) -> tuple[Path, CardFrontmatter, MutableMapping[str, str]]:
        """Проверяет корректность одной записи резолюции (Pass 1)."""
        card_file = self.repo.find_card_file(res.id)
        if not card_file:
            raise CardNotFoundError(f"Карточка-гипотеза '{res.id}' не найдена")

        fm, sections = self.repo.read_card_file(card_file)

        if fm.status != KnowledgeStatus.HYPOTHESIS:
            raise ValueError(
                f"Карточка '{res.id}' не является рабочей гипотезой (текущий статус: '{fm.status.value}'). "
                "Резолюция через resolve_hypotheses применима только к карточкам в статусе 'hypothesis'."
            )

        if fm.source == KnowledgeSource.USER_CONFIRMED:
            raise ForbiddenUserConfirmedActionError(
                f"Карточка '{res.id}' подтверждена пользователем (user_confirmed) "
                "и не может быть изменена или удалена через resolve_hypotheses."
            )

        if res.action == "confirm":
            if not res.solution or len(res.solution.strip()) < 10:
                raise ValueError(
                    f"Для подтверждения гипотезы '{res.id}' обязательно поле 'solution' длиной не менее 10 символов"
                )
            # Pass 1: проверка отсутствия дубликатов среди активных карточек до записи на диск
            sym_key = find_section_key(sections, "симптом", "контекст", "symptom", "context")
            symptom_val = sections.get(sym_key, fm.title).strip() if sym_key else fm.title
            rc_key = find_section_key(
                sections,
                "первопричина",
                "root cause",
                "рабочая гипотеза",
                "гипотеза",
                "hypothesis",
            )
            # ВАЖНО: заголовок карточки НЕ подставляется как доказательство причины!
            hypo_text = sections.get(rc_key, "").strip() if rc_key else ""

            dummy_req = CardRecordRequest.model_construct(
                title=fm.title,
                category=fm.category,
                type=KnowledgeType.LESSON,
                symptom=symptom_val,
                root_cause=hypo_text or "Подтвержденная первопричина решения проблемы",
                solution=res.solution.strip(),
                rationale=res.rationale or "Обоснование правильности подтверждено на практике",
                aliases=fm.aliases,
                tags=fm.tags,
                source=fm.source,
                force=False,
                confirmed_force=False,
            )
            report_reason = detect_report_like_content(dummy_req)
            if report_reason:
                raise ReportLikeContentError(report_reason)
            try:
                outcome, cand_id = self.storage.classify_confirm(
                    category=fm.category,
                    symptom=symptom_val,
                    hypo_reason=hypo_text,
                    exclude_id=res.id,
                )
                if outcome == "duplicate" and cand_id:
                    raise DuplicateCardError(cand_id)
                elif outcome == "uncertain" and cand_id:
                    raise UncertainDuplicateError(cand_id)
            except StorageUnavailableError:
                pass
        elif res.action == "duplicate":
            self._validate_duplicate_target(res, fm, sections)
        elif res.action == "refute":
            if not res.refutation_reason or len(res.refutation_reason.strip()) < 15:
                raise ValueError(
                    f"Для опровержения гипотезы '{res.id}' обязательно поле 'refutation_reason' длиной не менее 15 символов"
                )
            if res.merge_into:
                # Pass 1: только валидация, результат (target_file, target_fm, target_sections) отбрасывается
                _validate_merge_target(self.repo, res)

        return card_file, fm, sections

    def _resolve_confirm(
        self,
        card_file: Path,
        fm: CardFrontmatter,
        sections: MutableMapping[str, str],
        res: HypothesisResolution,
        today_str: str,
    ) -> dict[str, Any]:
        """Преобразует файл гипотезы в активную карточку-урок (confirm)."""
        if not res.solution or not res.solution.strip():
            raise ValueError(f"Для подтверждения гипотезы '{res.id}' обязательно поле 'solution'")

        # Quarantine policy (plan B): an agent's own confirm is not a proof of
        # human approval. Auto-inferred cards stay in draft; only cards already
        # human-confirmed (generated.by == human) may become active lessons.
        is_human_confirmed = (
            getattr(fm, "generated", None) is not None
            and getattr(fm.generated, "by", None) == GeneratedBy.HUMAN
        )
        if is_human_confirmed:
            fm.status = KnowledgeStatus.ACTIVE
            fm.type = KnowledgeType.LESSON
            fm.confidence = 0.85 if fm.confidence < 0.85 else fm.confidence
        else:
            fm.status = KnowledgeStatus.DRAFT
            fm.type = KnowledgeType.LESSON

        sol_content = res.solution.strip()
        if res.rationale and res.rationale.strip():
            rat_content = res.rationale.strip()
        else:
            rat_content = "Подтверждено на практике при проверке решения."

        sections["Решение / Выводы"] = sol_content
        sections["Обоснование / Почему именно так (Rationale)"] = rat_content
        append_log_entry(
            sections, f"- [{today_str}] CONFIRMED: Hypothesis confirmed as active lesson"
        )

        self.repo.save_card(card_file, fm, sections)
        return {
            "id": res.id,
            "action": "confirm",
            "status": "ok",
            "card_status": fm.status.value,
        }

    def _resolve_refute(
        self,
        card_file: Path,
        fm: CardFrontmatter,
        sections: MutableMapping[str, str],
        res: HypothesisResolution,
        today_str: str,
    ) -> dict[str, Any]:
        """Опровергает гипотезу (refute) со слиянием в нюансы или переводом в антипаттерн."""
        ref_reason = res.refutation_reason.strip()  # type: ignore[union-attr]
        if res.merge_into:
            # aud-backend-17b8 (ревью разворота): целевая карточка мутируется
            # через coordinator — полный цикл lock(merge_into) → read →
            # mutate → write → index. Чтение цели вне лока и save_card без
            # лока нарушали контракт RMW (lost update / конкурентный supersede).
            coordinator = self._coordinator_or_none()
            if coordinator is not None:

                def _merge_mutator(
                    target_fm: CardFrontmatter,
                    target_sections: MutableMapping[str, str],
                    *,
                    _res: HypothesisResolution = res,
                    _ref_reason: str = ref_reason,
                    _today_str: str = today_str,
                    _card_id: str = res.id,
                ) -> None:
                    if target_fm.status == KnowledgeStatus.SUPERSEDED:
                        raise ValueError(
                            f"Целевая карточка '{_res.merge_into}' вытеснена карточкой "
                            f"'{target_fm.superseded_by}' и не может принимать новые данные. "
                            "Укажите актуальную карточку для слияния."
                        )
                    if target_fm.is_human_protected:
                        raise ForbiddenUserConfirmedActionError(
                            f"Целевая карточка '{_res.merge_into}' подтверждена "
                            "пользователем (user_confirmed) и не может быть изменена "
                            "через авто-слияние гипотезы."
                        )
                    nuances_key = (
                        find_section_key(target_sections, "нюанс", "nuance", "ограничен")
                        or "Нюансы и ограничения (Nuances)"
                    )
                    merge_note = f"⛔ Опровергнутый подход ({_card_id}): {_ref_reason}"
                    current_nuances = target_sections.get(nuances_key, "").strip()
                    if not current_nuances:
                        target_sections[nuances_key] = merge_note
                    else:
                        target_sections[nuances_key] = f"{current_nuances}\n{merge_note}"
                    append_log_entry(
                        target_sections,
                        f"- [{_today_str}] REFUTATION MERGED: "
                        f"Refuted hypothesis {_card_id} merged into nuances",
                    )

                try:
                    coordinator.mutate(res.merge_into, _merge_mutator)
                except CardNotFoundError:
                    raise
                self.repo.delete_card(res.id)
                return {
                    "id": res.id,
                    "action": "refute",
                    "merged_into": res.merge_into,
                    "status": "ok",
                }

            # Fallback без synchronizer (прямой вызов use-case в тестах):
            # прежний путь с валидацией вне лока.
            # Pass 2: применяем слияние, используя target_sections из валидации
            target_file, target_fm, target_sections = _validate_merge_target(self.repo, res)

            nuances_key = (
                find_section_key(target_sections, "нюанс", "nuance", "ограничен")
                or "Нюансы и ограничения (Nuances)"
            )
            merge_note = f"⛔ Опровергнутый подход ({res.id}): {ref_reason}"
            current_nuances = target_sections.get(nuances_key, "").strip()
            if not current_nuances:
                target_sections[nuances_key] = merge_note
            else:
                target_sections[nuances_key] = f"{current_nuances}\n{merge_note}"

            append_log_entry(
                target_sections,
                f"- [{today_str}] REFUTATION MERGED: Refuted hypothesis {res.id} merged into nuances",
            )
            self.repo.save_card(target_file, target_fm, target_sections)
            self.repo.delete_card(res.id)

            return {
                "id": res.id,
                "action": "refute",
                "merged_into": res.merge_into,
                "status": "ok",
            }

        fm.status = KnowledgeStatus.REFUTED
        fm.type = KnowledgeType.ANTIPATTERN
        fm.confidence = 0.10
        fm.refutation_reason = ref_reason
        sections["Опровержение / Почему это тупик (Refutation Reason)"] = ref_reason
        append_log_entry(sections, f"- [{today_str}] REFUTED: {ref_reason}")

        self.repo.save_card(card_file, fm, sections)
        return {"id": res.id, "action": "refute", "status": "ok"}

    def _resolve_drop(self, res: HypothesisResolution) -> dict[str, Any]:
        """Удаляет файл неактуальной гипотезы (drop)."""
        self.repo.delete_card(res.id)
        return {"id": res.id, "action": "drop", "status": "ok"}

    def _resolve_duplicate(
        self,
        card_file: Path,
        fm: CardFrontmatter,
        sections: MutableMapping[str, str],
        res: HypothesisResolution,
        today_str: str,
    ) -> dict[str, Any]:
        """Закрывает гипотезу как superseded со ссылкой на существующий канонический дубликат.

        Ревью шага 3: цель duplicate_of читается под её card_lock — между
        проходами она могла конкурентно стать superseded, а ссылка на неё
        сделала бы новую карточку сиротой. Открытая гипотеза не является
        канонической целью: дубликат закрывается в пользу карточки-факта.
        """
        target_id = res.duplicate_of.strip()  # type: ignore[union-attr]
        with self.repo.card_lock(target_id):
            target_file = self.repo.find_card_file(target_id)
            if not target_file:
                raise CardNotFoundError(
                    f"Целевая карточка для duplicate_of '{target_id}' не найдена"
                )

            target_fm, target_sections = self.repo.read_card_file(target_file)
            if target_fm.status == KnowledgeStatus.SUPERSEDED or target_fm.superseded_by:
                raise ValueError(
                    f"Целевая карточка '{target_id}' уже вытеснена (superseded) "
                    "и не может быть целью duplicate_of"
                )
            if target_fm.status == KnowledgeStatus.HYPOTHESIS:
                raise ValueError(
                    f"Целевая карточка '{target_id}' сама является открытой гипотезой "
                    "и не может быть канонической целью duplicate_of: сначала "
                    "подтвердите или закройте её саму."
                )
            # Ревью r2/r4/r6: цель duplicate_of — только карточка-факт.
            # Явный список допустимых статусов (симметрично критериям
            # кандидатов confirm в classify_confirm, включая suspect):
            # ACTIVE, VERIFIED, SUSPECT и DRAFT (подтверждённые человеком
            # уроки в карантине). Отклоняются: открытые гипотезы,
            # опровергнутые (refuted), оспоренные (disputed), вытесненные
            # (superseded) карточки.
            if target_fm.status not in _DUPLICATE_TARGET_ALLOWED_STATUSES:
                raise ValueError(
                    f"Целевая карточка '{target_id}' имеет статус "
                    f"'{target_fm.status.value}' и не может быть канонической "
                    "целью duplicate_of: дубликат закрывается в пользу "
                    "подтверждённой карточки-факта "
                    "(active/verified/draft)."
                )

            sym_key = find_section_key(sections, "симптом", "контекст", "symptom", "context")
            hypo_symptom = sections.get(sym_key, fm.title).strip() if sym_key else fm.title
            rc_key = find_section_key(
                sections,
                "первопричина",
                "root cause",
                "рабочая гипотеза",
                "гипотеза",
                "hypothesis",
            )
            hypo_reason = sections.get(rc_key, "").strip() if rc_key else ""

            target_sym_key = find_section_key(
                target_sections, "симптом", "контекст", "symptom", "context"
            )
            target_symptom = (
                target_sections.get(target_sym_key, target_fm.title).strip()
                if target_sym_key
                else target_fm.title
            )
            target_rc_key = find_section_key(
                target_sections, "первопричина", "root cause", "причина", "гипотеза"
            )
            target_root_cause = (
                target_sections.get(target_rc_key, "").strip() if target_rc_key else ""
            )

            outcome = self.storage.classify_confirm_candidate(
                hypo_symptom=hypo_symptom,
                hypo_reason=hypo_reason,
                cand_symptom=target_symptom,
                cand_root_cause=target_root_cause,
            )
            if outcome != "duplicate":
                raise ValueError(
                    f"Карточка '{target_id}' не является подтверждённым дубликатом гипотезы '{res.id}' "
                    f"(исход классификации под локом: '{outcome}')."
                )

            fm.status = KnowledgeStatus.SUPERSEDED
            fm.superseded_by = target_id
            append_log_entry(sections, f"- [{today_str}] DUPLICATE: Superseded by {target_id}")

            self.repo.save_card(card_file, fm, sections)
        return {
            "id": res.id,
            "action": "duplicate",
            "status": "ok",
            "superseded_by": target_id,
        }

    def _apply_single_resolution(
        self,
        res: HypothesisResolution,
        today_str: str,
    ) -> dict[str, Any]:
        """Применяет резолюцию для одной карточки под локом с перечитыванием состояния."""
        with self.repo.card_lock(res.id):
            current_file = self.repo.find_card_file(res.id)
            if current_file is None:
                raise CardNotFoundError(f"Карточка-гипотеза '{res.id}' удалена между проходами")
            fm, sections = self.repo.read_card_file(current_file)
            if fm.status != KnowledgeStatus.HYPOTHESIS:
                raise ValueError(
                    f"Карточка '{res.id}' больше не является рабочей гипотезой "
                    f"(текущий статус: '{fm.status.value}'): конкурентная резолюция "
                    "опередила этот батч."
                )
            if fm.source == KnowledgeSource.USER_CONFIRMED:
                raise ForbiddenUserConfirmedActionError(
                    f"Карточка '{res.id}' подтверждена пользователем (user_confirmed) "
                    "и не может быть изменена или удалена через resolve_hypotheses."
                )
            if res.action == "confirm":
                return self._resolve_confirm(current_file, fm, sections, res, today_str)
            if res.action == "refute":
                return self._resolve_refute(current_file, fm, sections, res, today_str)
            if res.action == "drop":
                return self._resolve_drop(res)
            if res.action == "duplicate":
                return self._resolve_duplicate(current_file, fm, sections, res, today_str)
            raise ValueError(f"Неизвестное действие резолюции: '{res.action}'")

    def execute(self, req: HypothesesResolveRequest) -> dict[str, Any]:
        """Выполняет двухпроходное подведение итогов по гипотезам (Pass 1: Validate, Pass 2: Apply)."""
        today_str = date.today().isoformat()
        validated: list[
            tuple[HypothesisResolution, Path, CardFrontmatter, MutableMapping[str, str]]
        ] = []
        seen_res_ids: set[str] = set()

        # Pass 1: Валидация ВСЕХ резолюций до применения любых изменений
        for res in req.resolutions:
            if res.id in seen_res_ids:
                raise ValueError(f"Дублирующийся ID гипотезы '{res.id}' в батче резолюций")
            seen_res_ids.add(res.id)
            card_file, fm, sections = self._validate_resolution_item(res)
            validated.append((res, card_file, fm, sections))

        # Pass 2: Применение изменений.
        # AUDIT-ARCH-2026-10 (шаг 2) + ревью разворота (aud-backend-17b8):
        # RMW под per-card локом, и снимки ПЕРЕЧИТЫВАЮТСЯ под локом —
        # между Pass 1 и Pass 2 карточку мог конкурентно изменить другой
        # поток (append_note), и запись по устаревшему снимку затёрла бы
        # его правку (lost update). Полный RMW-цикл coordinator'а здесь
        # потребовал бы разрушения двухпроходного контракта batch-атомарности,
        # поэтому: card_lock + перечитывание.
        details: list[dict[str, Any]] = []
        sync_warning: str | None = None
        pass2_error: Exception | None = None

        try:
            for res, _card_file, _fm, _sections in validated:
                try:
                    details.append(self._apply_single_resolution(res, today_str))
                except Exception as err:
                    if len(details) > 0:
                        logger.warning(
                            "Сбой при обработке карточки '%s' в Pass 2 батча: %s. "
                            "Частичное применение: %d карточек успешно применены.",
                            res.id,
                            err,
                            len(details),
                        )
                        pass2_error = err
                        break
                    raise
        finally:
            # Синхронизация индекса обязательна ВСЕГДА, даже при сбое
            try:
                self.storage.sync()
            except (sqlite3.OperationalError, StorageUnavailableError) as err:
                logger.warning("Сбой синхронизации FTS5 индекса после резолюции гипотез: %s", err)
                sync_warning = (
                    f"резолюции применены на диске, но обновление индекса отложено: {err}"
                )

        if pass2_error is not None:
            resp: dict[str, Any] = {
                "status": "partial_failure",
                "task_id": req.task_id,
                "resolved_count": len(details),
                "details": details,
                "error": str(pass2_error),
            }
            if sync_warning:
                resp["warning"] = sync_warning
            return resp

        resp = {
            "status": "resolved",
            "task_id": req.task_id,
            "resolved_count": len(details),
            "details": details,
        }
        if sync_warning:
            resp["warning"] = sync_warning
        return resp
