"""Сервисный слой MCP Knowledge Engine (KnowledgeService).

Композитный фасад, объединяющий Use Cases:
- RecordKnowledgeUseCase: создание карточек и вытеснение.
- ProposeHypothesisUseCase: фиксация гипотез.
- ResolveHypothesesUseCase: двухпроходная резолюция гипотез.
- UpdateStatusUseCase: обновление статусов и верификация.
- QueryKnowledgeUseCase: получение и поиск карточек (CQRS Query Side).
"""

import contextlib
from pathlib import Path
from typing import Any

from mcp_knowledge.errors import (
    CardNotFoundError,
    DuplicateCardError,
    ForbiddenUserConfirmedActionError,
    ReportLikeContentError,
    StorageUnavailableError,
    UserConfirmedConflictError,
)
from mcp_knowledge.models import (
    CardFrontmatter,
    CardGetResponse,
    CardRecordRequest,
    CardStatusUpdateRequest,
    FacetedSearchResponse,
    HypothesesResolveRequest,
    HypothesisRecordRequest,
    KnowledgeCategory,
    KnowledgeSnippet,
    KnowledgeSource,
)
from mcp_knowledge.repository import (
    CardRepository,
    _atomic_write_text,
)
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.synchronizer import KnowledgeSynchronizer
from mcp_knowledge.use_cases import (
    ProposeHypothesisUseCase,
    QueryKnowledgeUseCase,
    RecordKnowledgeUseCase,
    ResolveHypothesesUseCase,
    ReviewKnowledgeUseCase,
    UpdateStatusUseCase,
)

__all__ = [
    "CardNotFoundError",
    "DuplicateCardError",
    "ForbiddenUserConfirmedActionError",
    "KnowledgeService",
    "ReportLikeContentError",
    "StorageUnavailableError",
    "UserConfirmedConflictError",
    "_atomic_write_text",
]


class KnowledgeService:
    """Композитный фасад для управления карточками знаний и поисковым FTS5-индексом."""

    def __init__(
        self,
        storage: KnowledgeStorage,
        knowledge_dir: Path | None = None,
        repo: CardRepository | None = None,
        synchronizer: KnowledgeSynchronizer | None = None,
    ) -> None:
        self.storage = storage
        self.knowledge_dir = (
            Path(knowledge_dir) if knowledge_dir is not None else storage.knowledge_dir
        )
        create_dir = getattr(storage, "_create_dirs", True)
        self.repo = repo or CardRepository(
            knowledge_dir=self.knowledge_dir,
            index_lookup=self._lookup_in_index,
            create_dir=create_dir,
        )
        self.synchronizer = synchronizer or KnowledgeSynchronizer(
            storage=self.storage,
            repo=self.repo,
        )
        self.storage.set_synchronizer(self.synchronizer)

        self._record_use_case = RecordKnowledgeUseCase(
            repo=self.repo,
            storage=self.storage,
            synchronizer=self.synchronizer,
        )
        self._propose_use_case = ProposeHypothesisUseCase(
            repo=self.repo,
            storage=self.storage,
            synchronizer=self.synchronizer,
        )
        self._resolve_use_case = ResolveHypothesesUseCase(
            repo=self.repo,
            storage=self.storage,
            synchronizer=self.synchronizer,
        )
        self._update_status_use_case = UpdateStatusUseCase(
            repo=self.repo,
            synchronizer=self.synchronizer,
        )
        self._query_use_case = QueryKnowledgeUseCase(
            repo=self.repo,
            storage=self.storage,
        )
        self._review_use_case = ReviewKnowledgeUseCase(
            storage=self.storage,
        )

    def _lookup_in_index(self, card_id: str) -> Path | None:
        """Поиск пути карточки в таблице file_sync SQLite.

        K12 rev.3.6: фильтр conflict=0 — при нескольких строках одного
        card_id возвращается только canonical-путь (LIMIT 1 без фильтра
        мог вернуть неканоничный дубликат).
        """
        with contextlib.suppress(Exception), self.storage.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT path FROM file_sync WHERE card_id = ? AND conflict = 0 LIMIT 1;",
                (card_id,),
            )
            row = cursor.fetchone()
            if row:
                return Path(row[0])
        return None

    def _get_existing_ids(self) -> set[str]:
        """Возвращает множество существующих ID карточек (делегат repo)."""
        return self.repo.get_existing_ids()

    def _find_card_file(self, card_id: str) -> Path | None:
        """Находит путь к файлу карточки по ее ID (делегат repo)."""
        return self.repo.find_card_file(card_id)

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
        """Помечает вытесненную карточку (делегат RecordKnowledgeUseCase).

        K4: снимки не передаются — делегат читает карточку под card_lock.
        """
        return self._record_use_case._apply_supersede(
            card_id=card_id,
            supersedes_id=supersedes_id,
            req_source=req_source,
            new_file_path=new_file_path,
        )

    def record(self, req: CardRecordRequest) -> dict[str, Any]:
        """Создает и сохраняет новую карточку знаний, обновляя связи и FTS5-индекс."""
        return self._record_use_case.execute(req, supersede_handler=self._apply_supersede)

    def propose_hypothesis(self, req: HypothesisRecordRequest) -> dict[str, Any]:
        """Фиксирует рабочую гипотезу во время исследования (Investigation Recording)."""
        return self._propose_use_case.execute(req)

    def resolve_hypotheses(self, req: HypothesesResolveRequest) -> dict[str, Any]:
        """Выполняет двухпроходное подведение итогов по гипотезам (Pass 1: Validate, Pass 2: Apply)."""
        return self._resolve_use_case.execute(req)

    def update_status(self, req: CardStatusUpdateRequest) -> dict[str, str]:
        """Обновляет статус карточки, записывает лог и ограничения."""
        return self._update_status_use_case.execute(req)

    def review(
        self,
        ids: list[str] | None = None,
        task_id: str | None = None,
        since: str | None = None,
        include_user_confirmed: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Формирует пакет саморевью свежих карточек (кандидаты, флаги, дубли, рубрика)."""
        return self._review_use_case.execute(
            ids=ids,
            task_id=task_id,
            since=since,
            include_user_confirmed=include_user_confirmed,
            limit=limit,
        )

    def get(self, ids: list[str]) -> CardGetResponse:
        """Возвращает детальную информацию по запрошенным карточкам (максимум 10 штук)."""
        return self._query_use_case.get(ids)

    def search(
        self,
        query: str,
        category: str | KnowledgeCategory | None = None,
        include_archived: bool = False,
        include_hypotheses: bool = False,
        task_id: str | None = None,
        limit: int = 5,
        include_drafts: bool = False,
    ) -> list[KnowledgeSnippet]:
        """Выполняет поиск по карточкам с использованием FTS5-индекса."""
        return self._query_use_case.search(
            query=query,
            category=category,
            include_archived=include_archived,
            include_hypotheses=include_hypotheses,
            include_drafts=include_drafts,
            task_id=task_id,
            limit=limit,
        )

    def search_faceted(
        self,
        query: str | None = None,
        categories: list[str] | None = None,
        task_id: str | None = None,
        include_hypotheses: bool = False,
        include_antipatterns: bool = True,
        limit: int = 5,
        limit_per_facet: int = 3,
        include_overview: bool = True,
        include_archived: bool = False,
        include_drafts: bool = False,
    ) -> FacetedSearchResponse:
        """Выполняет фасетный многослойный поиск с мягкой нормализацией категорий."""
        return self._query_use_case.search_faceted(
            query=query,
            categories=categories,
            task_id=task_id,
            include_hypotheses=include_hypotheses,
            include_antipatterns=include_antipatterns,
            limit=limit,
            limit_per_facet=limit_per_facet,
            include_overview=include_overview,
            include_archived=include_archived,
            include_drafts=include_drafts,
        )
