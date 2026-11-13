"""Use Case: чтение и поиск карточек знаний (Query Side)."""

from mcp_knowledge.logger import logger
from mcp_knowledge.models import (
    CardDetail,
    CardGetResponse,
    FacetedSearchResponse,
    KnowledgeCategory,
    KnowledgeSnippet,
    compute_card_badge,
    safe_category_slug,
)
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.storage import KnowledgeStorage


class QueryKnowledgeUseCase:
    """Use case для получения и поиска карточек знаний (CQRS Query Side)."""

    def __init__(
        self,
        repo: CardRepository,
        storage: KnowledgeStorage,
    ) -> None:
        self.repo = repo
        self.storage = storage

    def get(self, ids: list[str]) -> CardGetResponse:
        """Возвращает детальную информацию по запрошенным карточкам (максимум 10 штук)."""
        cards: list[CardDetail] = []
        not_found: list[str] = []
        raw_unique_ids = list(dict.fromkeys(ids))
        warning: str | None = None
        if len(raw_unique_ids) > 10:
            truncated = raw_unique_ids[10:]
            unique_ids = raw_unique_ids[:10]
            warning = (
                f"Запрошено {len(ids)} карточек ({len(raw_unique_ids)} уникальных). "
                f"Количество ограничено первыми 10: {unique_ids}. Отброшены: {truncated}."
            )
        else:
            unique_ids = raw_unique_ids

        for card_id in unique_ids:
            card_file = self.repo.find_card_file(card_id)
            if not card_file:
                not_found.append(card_id)
                continue

            try:
                fm, sections = self.repo.read_card_file(card_file)
            except Exception as err:
                logger.warning("Не удалось распарсить карточку %s: %s", card_id, err)
                not_found.append(card_id)
                continue

            # Формируем тело контента
            body_parts = []
            preamble = sections.get("_preamble")
            if preamble:
                body_parts.append(preamble)
            for header, text in sections.items():
                if header == "_preamble":
                    continue
                body_parts.append(f"### {header}\n{text}")
            content = "\n\n".join(body_parts)

            see_also_items = [
                x
                for x in dict.fromkeys(
                    fm.related_to
                    + ([fm.supersedes] if fm.supersedes else [])
                    + ([fm.superseded_by] if fm.superseded_by else [])
                )
                if x != fm.id
            ]

            superseded_info: str | None = None
            if fm.supersedes and fm.superseded_by:
                superseded_info = f"Заменила '{fm.supersedes}', но сама вытеснена карточкой (актуальная: {fm.superseded_by})"
            elif fm.superseded_by:
                superseded_info = f"Вытеснена карточкой (актуальная: {fm.superseded_by})"
            elif fm.supersedes:
                superseded_info = f"Заменила карточку: {fm.supersedes}"

            badge = compute_card_badge(status=fm.status, card_type=fm.type)

            cards.append(
                CardDetail(
                    id=fm.id,
                    title=fm.title,
                    category=fm.category,
                    type=fm.type,
                    status=fm.status,
                    source=fm.source,
                    badge=badge,
                    frontmatter=fm,
                    content=content,
                    supersedes=fm.supersedes,
                    superseded_by=fm.superseded_by,
                    superseded_info=superseded_info,
                    see_also=see_also_items,
                )
            )

        return CardGetResponse(cards=cards, not_found=not_found, warning=warning)

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
        return self.storage.search(
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
        clean_categories: list[str] = []
        if categories:
            for d in categories:
                d_clean = safe_category_slug(d)
                if d_clean is None:
                    continue
                clean_categories.append(d_clean)

        unique_categories = list(dict.fromkeys(clean_categories))[:5]

        return self.storage.search_faceted(
            query=query.strip() if query and query.strip() else None,
            categories=unique_categories if unique_categories else None,
            task_id=task_id,
            include_hypotheses=include_hypotheses,
            include_antipatterns=include_antipatterns,
            limit=limit,
            limit_per_facet=limit_per_facet,
            include_overview=include_overview,
            include_archived=include_archived,
            include_drafts=include_drafts,
        )
