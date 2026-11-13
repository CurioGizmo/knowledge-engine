"""CardMutationCoordinator — единственный исполнитель RMW-цикла карточки.

AUDIT-ARCH-2026-10 (шаг 2): до этого критическая секция размазывалась по
use-cases — lookup выполнялся ДО захвата лока (устаревший path при
конкуренции), index — вне единого контракта. Coordinator гарантирует
полный инвариант:

    lock(card_id) → lookup (ПОД локом) → read → mutate → atomic write
    → index_single_card

Use-cases передают мутатор (callback), а не готовый снимок: мутатор
получает (fm, sections) и мутирует их по месту; валидационные ошибки
(вытеснена, карантин, барьер) рейзятся из мутатора и прерывают цикл
без записи на диск.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp_knowledge.errors import CardNotFoundError, StorageUnavailableError
from mcp_knowledge.logger import logger
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.synchronizer import KnowledgeSynchronizer

# Мутатор: получает (fm, sections) под локом; мутирует по месту; может
# рейзить доменные ошибки валидации (без записи на диск); возвращает
# произвольный результат (например, hint) для вызывающего use-case.
CardMutator = Callable[[Any, Any], Any]


class CardMutationCoordinator:
    """Единственный владелец полного RMW-цикла мутации карточки."""

    def __init__(self, repo: CardRepository, synchronizer: KnowledgeSynchronizer) -> None:
        self.repo = repo
        self.synchronizer = synchronizer

    def mutate(
        self,
        card_id: str,
        mutator: CardMutator,
    ) -> tuple[Any, Path]:
        """Полный цикл: lock → lookup → read → mutate → write → index.

        Возвращает (result мутатора, canonical path записи).

        Lookup выполняется ПОД локом (не до): конкурентный supersede/
        rename мог сменить canonical-файл между внешним find_card_file
        и захватом лока — мутация ушла бы в устаревший путь.
        index_single_card вызывается после атомарной записи; сбой FTS5
        НЕ откатывает файл — ошибка возвращается как warning-текст
        (контракт best-effort, как _safe_index).
        """
        with self.repo.card_lock(card_id):
            card_file = self.repo.find_card_file(card_id)
            if card_file is None:
                raise CardNotFoundError(f"Карточка '{card_id}' не найдена")

            fm, sections = self.repo.read_card_file(card_file)
            result = mutator(fm, sections)

            self.repo.save_card(card_file, fm, sections)

        # Индексация вне лока записи (index_single_card берёт свои
        # SQLite-транзакции), сбой — warning, не откат
        index_warning = self._safe_index(card_id, card_file)
        if index_warning:
            if isinstance(result, dict):
                # результат мутатора + предупреждение — возвращаем кортежем
                # известного вида только если result — dict (use-case контракт)
                result = {**result, "index_warning": index_warning}
            else:
                # Non-dict результат (например None у related-links lambda):
                # warning не может быть встроен в результат — логируем,
                # чтобы сбой индексации не терялся молча (aud-backend-17b8).
                logger.warning(
                    "Сбой индексации карточки '%s' (результат мутатора non-dict, "
                    "warning не пробрасывается): %s",
                    card_id,
                    index_warning,
                )
        return result, card_file

    def _safe_index(self, card_id: str, path: Path) -> str | None:
        try:
            self.synchronizer.index_single_card(card_id, path)
            return None
        except (sqlite3.OperationalError, StorageUnavailableError) as err:
            return f"изменение сохранено на диск, но обновление индекса отложено: {err}"
