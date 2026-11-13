"""CanonicalCardResolver — единоличный владелец инварианта канона (K12).

Архитектурный разворот AUDIT-ARCH-2026-10: до этого резолв «card_id →
canonical файл» существовал в четырёх независимых копиях (full sync,
index_single_card, service lookup, repository lookup), и их расхождение
порождало дефекты (DELETE правильной FTS-записи, мутация неканоничного
id.md). Теперь все пути обязаны делегировать сюда.

Правило канона (rev.3.6): max(mtime_ns, size), tie-break —
min(нормализованный путь). Детерминировано: порядок обхода файлов не
влияет на результат.

Источник данных о каноничности для lookup — file_sync (conflict=0):
это состояние, вычисленное последним sync/index_single_card. Если
file_sync пуст или устарел (кэш пересоздан), lookup деградирует до
дискового резолва с тем же правилом.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable
from pathlib import Path

from mcp_knowledge.parser import parse_card_markdown

logger = logging.getLogger(__name__)


class CanonicalCardResolver:
    """Резолв card_id → canonical-файл. Единственный владелец правила."""

    def __init__(
        self,
        knowledge_dir: Path,
        index_lookup: Callable[[str], Path | None] | None = None,
    ) -> None:
        self.knowledge_dir = Path(knowledge_dir)
        self._index_lookup = index_lookup

    # ------------------------------------------------------------------
    # Ядро правила: детерминированный выбор из группы файлов
    # ------------------------------------------------------------------
    @staticmethod
    def select_canonical(files: list[Path]) -> Path | None:
        """K12: canonical = max(mtime_ns, size), tie-break min(path).

        Порядок ``files`` не влияет на результат (инвариант проверяется
        property-тестом). Пустой список → None.
        """
        if not files:
            return None
        resolved = [(f, str(f.resolve())) for f in files]
        best_key = max((f.stat().st_mtime_ns, f.stat().st_size) for f, _ in resolved)
        equal = [
            item
            for item in resolved
            if (item[0].stat().st_mtime_ns, item[0].stat().st_size) == best_key
        ]
        return min(equal, key=lambda item: item[1])[0]

    # ------------------------------------------------------------------
    # Дисковая фаза: все файлы с данным frontmatter.id
    # ------------------------------------------------------------------
    def files_for_id(self, card_id: str) -> list[Path]:
        """Все файлы на диске, чей frontmatter.id == card_id.

        Имена файлов НЕ используются для матчинга (id в имени может
        расходиться с frontmatter — рассинхрон это warning, не правило).
        """
        if not card_id or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", card_id):
            return []
        same_id: list[Path] = []
        for cand in self.knowledge_dir.rglob("*.md"):
            if not cand.is_file():
                continue
            try:
                content = cand.read_text(encoding="utf-8")
                fm, _ = parse_card_markdown(content)
            except Exception as err:
                # некорректный Markdown — кандидат не в счёт; логируем для диагностики
                logger.debug("resolver: пропуск нечитаемого кандидата %s: %s", cand, err)
                continue
            if fm.id == card_id:
                same_id.append(cand)
        return same_id

    # ------------------------------------------------------------------
    # Lookup: index-first, disk-fallback
    # ------------------------------------------------------------------
    def lookup(self, card_id: str) -> Path | None:
        """Canonical-путь карточки: file_sync (conflict=0) → диск.

        Одинаковая семантика для indexed- и filesystem-lookup: индекс
        возвращает только строки conflict=0; при пустом/устаревшем индексе
        (путь не существует / вне knowledge_dir) — дисковый резолв по тому
        же правилу канона.
        """
        if self._index_lookup is not None:
            with contextlib.suppress(Exception):
                p = self._index_lookup(card_id)
                if self._is_valid(p, card_id):
                    return p
        return self.select_canonical(self.files_for_id(card_id))

    def _is_valid(self, p: Path | None, card_id: str) -> bool:
        """Индексный хит валиден: существует, внутри knowledge_dir, И его
        frontmatter.id совпадает с запрошенным.

        Сверка id (aud-backend-db07): при внешне перезаписанном файле
        (путь тот же, id X→Y) индексный хит указывал бы на чужую карточку —
        lookup обязан деградировать до дискового резолва, а не вернуть её.
        Проверка аналогична audit-стороннему ``_plan_file_id_matches``.
        """
        if p is None or not p.exists():
            return False
        with contextlib.suppress(OSError):
            if not p.resolve().is_relative_to(self.knowledge_dir.resolve()):
                return False
        with contextlib.suppress(Exception):
            fm, _ = parse_card_markdown(p.read_text(encoding="utf-8"))
            return fm.id == card_id
        return False

    # ------------------------------------------------------------------
    # Инверсный резолв: является ли путь каноном своего card_id
    # ------------------------------------------------------------------
    def is_canonical(self, path: Path) -> bool:
        """True, если path — canonical среди всех файлов с тем же frontmatter.id."""
        try:
            content = path.read_text(encoding="utf-8")
            fm, _ = parse_card_markdown(content)
        except (OSError, ValueError):
            return False
        canonical = self.select_canonical(self.files_for_id(fm.id))
        return canonical is not None and canonical.resolve() == path.resolve()
