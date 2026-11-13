"""Use Case: обновление статуса карточки, верификация и добавление заметок."""

from collections.abc import MutableMapping
from datetime import date

from mcp_knowledge.content_gate import detect_report_like_content
from mcp_knowledge.coordinator import CardMutationCoordinator
from mcp_knowledge.errors import (
    ForbiddenUserConfirmedActionError,
    ReportLikeContentError,
)
from mcp_knowledge.messages import format_promote_hint, format_suspect_hint
from mcp_knowledge.models import (
    HUMAN_CALLER_NAMES,
    CardFrontmatter,
    CardStatusUpdateRequest,
    KnowledgeSource,
    KnowledgeStatus,
    VerificationRecord,
)
from mcp_knowledge.parser import (
    append_log_entry,
    find_section_key,
)
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.synchronizer import KnowledgeSynchronizer


class UpdateStatusUseCase:
    """Use case для управления жизненным циклом и статусами карточек знаний."""

    def __init__(
        self,
        repo: CardRepository,
        synchronizer: KnowledgeSynchronizer,
    ) -> None:
        self.repo = repo
        self.synchronizer = synchronizer

    def _apply_verify_action(
        self,
        fm: CardFrontmatter,
        sections: MutableMapping[str, str],
        req: CardStatusUpdateRequest,
        today_str: str,
    ) -> None:
        """Применяет действие верификации (verify), обновляя лог и нюансы.

        K8-идемпотентность: повторный verify тем же верификатором в тот же
        день с той же note не дублирует VerificationRecord и строку лога.
        """
        fm.status = KnowledgeStatus.VERIFIED
        verifier = req.by if req.by else "human"
        clean_note = req.note.strip() if req.note else None
        duplicate = any(
            v.by == verifier and v.at == today_str and (v.note or None) == clean_note
            for v in fm.verified
        )
        if not duplicate:
            append_log_entry(sections, f"- [{today_str}] VERIFIED: {req.note}")
            fm.verified.append(
                VerificationRecord(
                    by=verifier,
                    at=today_str,
                    note=clean_note,
                    action="verify",
                )
            )
        trivial_notes = {"ok", "ок", "verified", "подтверждено", "+", "yes", "да"}
        if req.note and req.note.strip().lower() not in trivial_notes:
            nuances_key = (
                find_section_key(sections, "нюанс", "nuance", "ограничен")
                or "Нюансы и ограничения (Nuances)"
            )
            current_nuances = sections.get(nuances_key, "").strip()
            if not current_nuances:
                sections[nuances_key] = req.note.strip()
            elif req.note not in current_nuances:
                sections[nuances_key] = f"{current_nuances}\n{req.note.strip()}"

    def _apply_promote_action(
        self,
        fm: CardFrontmatter,
        sections: MutableMapping[str, str],
        req: CardStatusUpdateRequest,
        today_str: str,
    ) -> str:
        """Применяет действие promote, переводя карточку из draft в active."""
        if fm.status != KnowledgeStatus.DRAFT:
            raise ValueError(
                f"Действие 'promote' применимо только к карточкам в статусе 'draft' (текущий статус: '{fm.status.value}')."
            )
        is_human_promoter = req.source == KnowledgeSource.USER_CONFIRMED or (
            bool(req.by and req.by.strip().lower() in HUMAN_CALLER_NAMES)
        )
        if not is_human_promoter:
            raise ForbiddenUserConfirmedActionError(
                f"Промоушен карточки '{req.id}' из draft в active требует подтверждения человека "
                "(укажите source='user_confirmed' или by='human')."
            )
        fm.status = KnowledgeStatus.ACTIVE
        # Origin preservation (plan B): by='human'/source='user_confirmed' are caller
        # claims, not proof of authorship. The card's generated.by and confidence stay
        # unchanged; the consent fact is recorded in the verified entry and log below.
        verifier = req.by if req.by else "human"
        note_text = req.note.strip() if req.note else "Promoted from draft to active"
        append_log_entry(sections, f"- [{today_str}] PROMOTED: {note_text}")
        fm.verified.append(
            VerificationRecord(
                by=verifier,
                at=today_str,
                note=f"Promoted to active: {note_text}",
                action="promote",
            )
        )
        return format_promote_hint(req.id)

    def execute(self, req: CardStatusUpdateRequest) -> dict[str, str]:
        """Выполняет обновление статуса карточки.

        AUDIT-ARCH-2026-10 (шаг 2): полный RMW-цикл — через
        CardMutationCoordinator: lock → lookup ПОД локом → read →
        mutate → atomic write → index. Use-case передаёт мутатор
        (валидации + переходы), а не снимок.
        """
        coordinator = CardMutationCoordinator(self.repo, self.synchronizer)
        resp, _card_file = coordinator.mutate(
            req.id, lambda fm, sections: self._apply_mutation(req, fm, sections)
        )
        return resp

    def _apply_mutation(
        self,
        req: CardStatusUpdateRequest,
        fm: CardFrontmatter,
        sections: MutableMapping[str, str],
    ) -> dict[str, str]:
        """Мутатор (выполняется под card_lock через coordinator).

        Валидации (вытеснена/карантин/барьер) рейзятся ДО изменения
        состояния — запись на диск не выполняется.
        """

        # Проверка инварианта вытеснения: вытесненные карточки не могут изменять свой статус
        if fm.status == KnowledgeStatus.SUPERSEDED:
            raise ValueError(
                f"Карточка '{req.id}' вытеснена карточкой '{fm.superseded_by}' "
                "и не может изменять свой статус. Используйте замещающую карточку."
            )

        # K2: матрица переходов. Карантин draft и гипотезы hypothesis не
        # переводятся в active-статусы напрямую — только через promote /
        # resolve_hypotheses соответственно (обход карантина запрещён;
        # совместно с aud-backend-d83a).
        if fm.status == KnowledgeStatus.DRAFT and req.action in ("suspect", "verify"):
            raise ValueError(
                f"Карточка '{req.id}' находится в статусе 'draft' (карантин): "
                f"действие '{req.action}' недоступно. Сначала продвиньте карточку "
                "через action='promote' (с подтверждением человека), затем меняйте статус."
            )
        if fm.status == KnowledgeStatus.HYPOTHESIS and req.action == "verify":
            raise ValueError(
                f"Карточка '{req.id}' — гипотеза: верификация через "
                "update_knowledge_status(action='verify') недоступна. Гипотезы "
                "закрываются через resolve_hypotheses (confirm с solution — "
                "конвертация в боевую карточку, refute — в антипаттерн)."
            )

        # Проверка защитного барьера user_confirmed / promoted (KS13 Вариант Б)
        is_human_caller = req.source == KnowledgeSource.USER_CONFIRMED or (
            bool(req.by and req.by.strip().lower() in HUMAN_CALLER_NAMES)
        )
        if fm.is_human_protected and not is_human_caller and req.action in ("dispute", "verify"):
            action_verb = "оспорена" if req.action == "dispute" else "верифицирована"
            raise ForbiddenUserConfirmedActionError(
                f"Карточка '{req.id}' подтверждена пользователем (user_confirmed) "
                f"или продвинута им и не может быть {action_verb} или изменена автоматически (auto_inferred)."
            )

        # Проверка Content Gate для заметок и верификации
        # K7: gate покрывает ВСЕ текст-несущие действия (suspect/dispute
        # пишут note в лог) — не только append_note/verify.
        if (
            req.action in ("append_note", "verify", "suspect", "dispute")
            and req.source != KnowledgeSource.USER_CONFIRMED
        ):
            report_reason = detect_report_like_content(req.note)
            if report_reason:
                raise ReportLikeContentError(report_reason)

        today_str = date.today().isoformat()
        hint: str | None = None

        if req.action == "suspect":
            fm.status = KnowledgeStatus.SUSPECT
            append_log_entry(sections, f"- [{today_str}] SUSPECT: {req.note}")
            if fm.supersedes:
                hint = format_suspect_hint(fm.supersedes)
        elif req.action == "dispute":
            fm.status = KnowledgeStatus.DISPUTED
            append_log_entry(sections, f"- [{today_str}] DISPUTED: {req.note}")
        elif req.action == "verify":
            self._apply_verify_action(fm, sections, req, today_str)
        elif req.action == "append_note":
            append_log_entry(sections, f"- [{today_str}] NOTE: {req.note}")
        elif req.action == "promote":
            hint = self._apply_promote_action(fm, sections, req, today_str)
        else:
            raise ValueError(f"Неизвестное действие action='{req.action}'")

        # Запись на диск + индексация — CardMutationCoordinator (после
        # возврата мутатора; ошибки валидации выше прерывают без записи)
        resp: dict[str, str] = {
            "status": "updated",
            "id": req.id,
            "new_status": fm.status.value,
        }
        if hint:
            resp["hint"] = hint
        return resp
