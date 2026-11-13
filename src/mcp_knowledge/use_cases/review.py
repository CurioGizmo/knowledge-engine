"""Use Case: итоговое саморевью свежих карточек (review_knowledge).

Движок отбирает карточки-кандидаты (по task_id / since / ids), считает
детерминированные флаги и подсказки дублей, а семантическую проверку
выполняет сам агент по встроенной рубрике. Мусор помечается через
update_knowledge_status(action='dispute') — жёсткого удаления нет.
"""

import re
from datetime import date, timedelta
from typing import Any

from mcp_knowledge.logger import logger
from mcp_knowledge.storage import KnowledgeStorage

# Маркеры контента, который по правилам хранилища НЕ должен попадать в карточки
# (рабочие планы, чек-листы, TODO — всё это живёт в docs/plans репозитория).
PLAN_MARKER_PATTERN = re.compile(
    r"(TODO|FIXME|чек[-\s]?лист|check\s?-?list|рабочий план|план работ|шаг\s*\d+|step\s*\d+|черновик|draft)",
    re.IGNORECASE,
)

# Порог «тонкого» решения: чуть выше Anti-Spam Gate (50 символов),
# чтобы подсветить карточки, прошедшие гейт впритык.
THIN_SOLUTION_THRESHOLD = 80

REVIEW_RUBRIC = (
    "Семантическая рубрика саморевью карточки (пройди по каждому пункту):\n"
    "1. ПОЛЬЗА: принесёт ли карточка реальную пользу будущему агенту в ЭТОМ проекте? "
    "Если ответ «ну, может быть» — спроси пользователя (шаблон ниже).\n"
    "2. СПЕЦИФИЧНОСТЬ: привязана ли к реалиям кодовой базы (файлы, версии, платформа, флаги) "
    "или это общие слова, которые LLM и так знает? Общие слова — мусор.\n"
    "3. ТИП КОНТЕНТА: стабильное инженерное знание, а не план/чек-лист/заметки сессии "
    "(планы должны жить в docs/plans, не в карточках). "
    "Plans belong to the `audit-engine` API tools (record_plan/step_plan) while the server is "
    "reachable; a `docs/plans/<date>-<slug>.md` file is an offline fallback only.\n"
    "4. ДУБЛЬ: сравни с подсказками 'similar' — не пересказ ли существующей карточки? "
    "Если да: обнови существующую (append_note) или вытесни (supersedes), а не плод копий.\n"
    "5. ПРОВЕРЯЕМОСТЬ: можно ли утверждение подтвердить или опровергнуть на стенде/кодом?"
)

ASK_USER_TEMPLATE = (
    "Карточка '{id}': {title}\n  Симптом: {symptom}\n"
    "  Оставить это в базе знаний проекта? (полезно / спорно / мусор)"
)

VERDICT_ACTIONS = (
    "Вердикты применяй СУЩЕСТВУЮЩИМИ инструментами (review_knowledge ничего не мутирует):\n"
    "- ПОЛЕЗНО (перевод из draft в боевую базу): только по явному согласию пользователя (покажи карточку и получи аппрув) → update_knowledge_status(id='...', action='promote', source='user_confirmed', note='Утверждено пользователем').\n"
    "- ПОДТВЕРЖДЕНО НА СТЕНДЕ (для active): update_knowledge_status(id='...', action='verify', by='human', note='...').\n"
    "- НЕ УВЕРЕН → задай пользователю вопрос по шаблону ASK_USER_TEMPLATE и действуй по ответу.\n"
    "- МУСОР / план / сессионный шум → update_knowledge_status(id='...', action='dispute', "
    "note='почему это не знание'). Карточка мгновенно исчезнет из поиска. "
    "При полной уверенности файл карточки можно просто удалить с диска — индекс выздоровеет сам при следующем поиске.\n"
    "- ДУБЛЬ → update_knowledge_status(id='...', action='append_note') в существующую карточку, "
    "или пересоздай с supersedes='<existing_id>'."
)


class ReviewKnowledgeUseCase:
    """Use case саморевью свежих карточек: отбор кандидатов, флаги, дубли, рубрика."""

    def __init__(self, storage: KnowledgeStorage) -> None:
        self.storage = storage

    def _compute_flags(self, card: dict[str, Any]) -> list[str]:
        """Считает детерминированные сигналы «подозрительности» карточки."""
        flags: list[str] = []
        combined_text = " ".join(
            str(card.get(field) or "")
            for field in ("title", "symptom", "root_cause", "solution", "rationale")
        )
        if PLAN_MARKER_PATTERN.search(combined_text):
            flags.append("plan_markers")

        stale_after = str(card.get("stale_after") or "").strip()
        if stale_after:
            try:
                if date.fromisoformat(stale_after.split("T")[0]) < date.today():
                    flags.append("stale_after_expired")
            except ValueError:
                pass

        # Пустое или короткое решение — одинаковый подозрительный сигнал (гейт 50 символов
        # обходится ручными md-файлами с пустыми секциями).
        solution = str(card.get("solution") or "").strip()
        if len(solution) < THIN_SOLUTION_THRESHOLD:
            flags.append("thin_solution")

        verified_count = int(card.get("verified_count") or 0)
        if str(card.get("source") or "") == "auto_inferred" and verified_count == 0:
            flags.append("unverified_auto")
        if str(card.get("status") or "") == "draft":
            flags.append("draft_candidate")
        return flags

    def _collect_similar(self, card: dict[str, Any]) -> list[dict[str, Any]]:
        """Ищет похожие карточки-дубли (сбои поиска не ломают саморевью)."""
        try:
            return self.storage.find_similar_cards(
                card_id=str(card["id"]),
                category=str(card.get("category", "")),
                symptom=str(card.get("symptom") or ""),
                limit=3,
            )
        except Exception as err:
            logger.warning("Подсказки дублей для '%s' пропущены: %s", card.get("id"), err)
            return []

    def execute(
        self,
        ids: list[str] | None = None,
        task_id: str | None = None,
        since: str | None = None,
        include_user_confirmed: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Формирует пакет саморевью: кандидаты + флаги + дубли + рубрика."""
        since_date: str | None = None
        if since:
            # Явная валидация формата: мусорный since не должен молча давать пустую выборку.
            try:
                since_date = date.fromisoformat(since.split("T")[0]).isoformat()
            except ValueError as err:
                raise ValueError(
                    f"Некорректный формат since: '{since}'. Ожидается ISO-дата (YYYY-MM-DD, "
                    "время отбрасывается — сравнение на уровне дня)."
                ) from err

        clean_ids = list(dict.fromkeys(i.strip() for i in (ids or []) if i and i.strip()))[:10]

        candidates: list[dict[str, Any]] = []
        selection_note: str | None = None
        if clean_ids:
            selection_note = "явный список ids (фильтры source/since карточек не применяются)"
            # Точечная выборка по ID — не зависит от возраста карточки и лимитов выборки.
            by_id = {str(r.get("id")): r for r in self.storage.get_cards_by_ids(clean_ids)}
            for card_id in clean_ids:
                row = by_id.get(card_id)
                if row is None:
                    # Карточка может быть вытеснена/оспорена или не существовать — честно сообщаем.
                    candidates.append({"id": card_id, "error": "не найдена в активном индексе"})
                else:
                    candidates.append(row)
        else:
            source_filter = None if include_user_confirmed else "auto_inferred"
            effective_since = since_date
            if not task_id and not since_date:
                # Дефолтное окно саморевью: карточки за последний день (сравнение на уровне даты).
                effective_since = (date.today() - timedelta(days=1)).isoformat()
            rows = self.storage.list_recent_cards(
                task_id=task_id,
                source=source_filter,
                since=effective_since,
                limit=limit,
            )
            candidates = rows
            if task_id and since_date:
                selection_note = f"task_id='{task_id}' AND created_at >= {since_date}"
            elif task_id:
                selection_note = f"task_id='{task_id}'"
            else:
                selection_note = f"created_at >= {effective_since}"

        for card in candidates:
            if "error" in card:
                continue
            card["flags"] = self._compute_flags(card)
            card["similar"] = self._collect_similar(card)

        open_hypotheses = self.storage.list_open_hypotheses(task_id=task_id, limit=10)

        response: dict[str, Any] = {
            "status": "ok",
            "candidates": candidates,
            "candidates_count": len(candidates),
            "open_hypotheses": {
                "count": len(open_hypotheses),
                "items": open_hypotheses,
                "reminder": (
                    "Незакрытые гипотезы запрещено оставлять: вызови "
                    "resolve_hypotheses (confirm/refute/drop)."
                    if open_hypotheses
                    else None
                ),
            },
            "selection": selection_note
            or "по умолчанию: активные auto_inferred-карточки за последний день (сравнение на уровне даты)",
            "rubric": REVIEW_RUBRIC,
            "ask_user_template": ASK_USER_TEMPLATE,
            "verdict_actions": VERDICT_ACTIONS,
        }
        if not candidates:
            response["hint"] = (
                "Кандидатов для ревью нет: либо карточки уже проверены, либо селектор пуст. "
                "Проверьте task_id/since или запишите карточки с task_id через record_knowledge."
            )
        elif candidates and all("error" in card for card in candidates):
            response["hint"] = (
                "Ни один из запрошенных ID не найден в активном индексе: карточки вытеснены, "
                "оспорены (disputed/refuted) или не существуют. Проверьте ID через "
                "search_knowledge(include_archived=True)."
            )
        return response
