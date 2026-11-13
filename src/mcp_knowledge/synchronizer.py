"""Синхронизатор между файловой системой Markdown-карточек и SQLite FTS5 индексом."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp_core.markdown import SectionCollection
from mcp_core.sync import BaseSynchronizer, drop_with_move_protection
from mcp_knowledge.db_guard import _raise_sqlite_unavailable
from mcp_knowledge.logger import logger
from mcp_knowledge.models import (
    CardFrontmatter,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import find_section_content, parse_card_markdown
from mcp_knowledge.resolver import CanonicalCardResolver
from mcp_knowledge.telemetry import _TOOL_CALLS_RETENTION, _prune_tool_calls

if TYPE_CHECKING:
    from mcp_knowledge.repository import CardRepository
    from mcp_knowledge.storage import KnowledgeStorage


@contextmanager
def _transaction(cursor: sqlite3.Cursor) -> Generator[None]:
    """Оборачивает операции в транзакцию ``BEGIN IMMEDIATE`` с откатом при сбое.

    COMMIT выполняется только при штатном выходе; любое исключение
    (включая ``KeyboardInterrupt``/``SystemExit``) приводит к ROLLBACK
    и пробрасывается наружу.
    """
    cursor.execute("BEGIN IMMEDIATE;")
    try:
        yield
    except BaseException:
        cursor.execute("ROLLBACK;")
        raise
    else:
        cursor.execute("COMMIT;")


def calculate_card_confidence(frontmatter: CardFrontmatter) -> float:
    """Вычисляет динамический вес доверия карточки на основе статуса, источника и аудита."""
    if (
        frontmatter.status == KnowledgeStatus.REFUTED
        or frontmatter.type == KnowledgeType.ANTIPATTERN
    ):
        base_conf = 0.10
    elif frontmatter.status == KnowledgeStatus.HYPOTHESIS:
        base_conf = (
            frontmatter.confidence
            if frontmatter.confidence is not None and frontmatter.confidence != 0.75
            else 0.20
        )
    elif frontmatter.source == KnowledgeSource.USER_CONFIRMED:
        base_conf = 1.0
    elif frontmatter.status == KnowledgeStatus.VERIFIED or len(frontmatter.verified) > 0:
        base_conf = min(1.0, 0.85 + min(0.15, len(frontmatter.verified) * 0.05))
    elif frontmatter.status == KnowledgeStatus.ACTIVE:
        base_conf = frontmatter.confidence if frontmatter.confidence is not None else 0.75
    else:
        base_conf = frontmatter.confidence if frontmatter.confidence is not None else 0.50

    # Штрафы
    if frontmatter.status == KnowledgeStatus.SUSPECT:
        base_conf -= 0.40

    if frontmatter.stale_after:
        try:
            stale_date = date.fromisoformat(frontmatter.stale_after.split("T")[0])
            if stale_date < date.today():
                base_conf -= 0.20
        except ValueError:
            pass

    return round(max(0.0, min(1.0, base_conf)), 2)


def _card_body_content(sections: SectionCollection, content: str) -> str:
    """Собирает поисковый 'body' карточки из секций Markdown (или сырой текст как fallback).

    Ревью шага 3 (дополнение): если ВСЕ секции карточки пусты, прежний
    fallback подставлял в индекс сырой контент целиком — вместе с
    frontmatter и шаблонными ###-заголовками пустых секций. Для такой
    карточки body пуст: шаблонные заголовки не должны индексироваться,
    а метаданные frontmatter и так лежат в отдельных колонках.
    """
    body_parts = []
    if "_preamble" in sections and sections["_preamble"].strip():
        body_parts.append(sections["_preamble"].strip())
    for h, t in sections.items():
        if h != "_preamble" and t.strip():
            body_parts.append(t.strip())
    if body_parts:
        return "\n\n".join(body_parts)
    # Все секции пусты (или секций нет вовсе): у шаблонных карточек
    # единственный «контент» — заголовки пустых секций и frontmatter.
    # Индексировать их как body — мусор; возвращаем пустую строку.
    return ""


def resolve_canonical_card_file(files: list[Path]) -> Path | None:
    """K12 canonical-резолвер: max по (mtime_ns, size), tie-break — min(path).

    AUDIT-ARCH-2026-10: тонкий делегат к CanonicalCardResolver.select_canonical —
    единственному владельцу правила. Публичное имя сохранено для
    совместимости тестов; новая архитектура использует resolver напрямую.
    """
    return CanonicalCardResolver.select_canonical(files)


def _index_row_values(
    frontmatter: CardFrontmatter, sections: SectionCollection, content: str
) -> tuple[Any, ...]:
    """Формирует значения 20 колонок knowledge_index для INSERT (порядок = BM25_WEIGHTS)."""
    symptom_text = find_section_content(sections, "симптом", "symptom", "контекст", "context")
    root_cause_text = find_section_content(
        sections, "первопричина", "root cause", "cause", "гипотез", "hypothesis"
    )
    solution_text = find_section_content(sections, "решение", "solution", "выводы")

    return (
        frontmatter.id,
        frontmatter.title,
        frontmatter.category.value
        if isinstance(frontmatter.category, KnowledgeCategory)
        else str(frontmatter.category),
        frontmatter.type.value,
        frontmatter.status.value,
        frontmatter.source.value,
        frontmatter.generated.by.value,
        frontmatter.generated.created_at,
        frontmatter.stale_after,
        len(frontmatter.verified),
        calculate_card_confidence(frontmatter),
        frontmatter.task_id,
        " ".join(frontmatter.aliases),
        symptom_text,
        root_cause_text,
        solution_text,
        find_section_content(sections, "обоснование", "rationale", "why"),
        frontmatter.refutation_reason
        or find_section_content(sections, "опровержение", "refutation", "тупик"),
        " ".join(frontmatter.tags),
        _card_body_content(sections, content),
    )


class KnowledgeSynchronizer(BaseSynchronizer):
    """Оркестратор синхронизации Markdown-файлов репозитория с SQLite FTS5 индексом."""

    def __init__(self, storage: KnowledgeStorage, repo: CardRepository) -> None:
        self.storage = storage
        self.repo = repo

    def _is_cache_dirty(self) -> bool:
        """Проверяет в режиме чтения, изменились ли файлы на диске по сравнению с file_sync."""
        try:
            with self.storage.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT path, mtime, size FROM file_sync;")
                synced_map: dict[str, tuple[float, int]] = {
                    row[0]: (row[1], row[2]) for row in cursor.fetchall()
                }
        except Exception as err:
            logger.warning("Не удалось прочитать file_sync при dirty-check: %s", err)
            return True

        disk_stats = self.repo.stat_cards()
        disk_scan: dict[str, tuple[float, int]] = {
            str(p.resolve()): (mtime, size) for p, mtime, size in disk_stats
        }

        if disk_scan.keys() != synced_map.keys():
            return True

        for p, (_mtime, _size) in disk_scan.items():
            s_mtime, s_size = synced_map[p]
            if BaseSynchronizer.is_file_dirty(s_mtime, s_size, Path(p)):
                return True

        return False

    def _index_card_file(
        self,
        cursor: sqlite3.Cursor,
        md_file: Path,
        file_str: str,
        mtime: float,
        size: int,
        synced_files: dict[str, tuple[float, int, str, int]],
        conflict: int = 0,
        index_content: bool = True,
    ) -> str | None:
        """Парсит и индексирует один файл карточки в транзакции SQLite.

        K12: ``index_content=False`` (неканоничный дубликат) — запись в FTS
        не производится, file_sync сохраняется с conflict=1. frontmatter.id
        возвращается в любом случае (для dirty-учёта).
        """
        try:
            content = md_file.read_text(encoding="utf-8")
            frontmatter, sections = parse_card_markdown(content)
        except Exception as err:
            logger.warning("Пропуск невалидного файла карточки %s: %s", md_file, err)
            return None

        if frontmatter.id != md_file.stem:
            logger.warning(
                "Рассинхрон ID карточки и имени файла: frontmatter.id='%s' != filename='%s' (%s)",
                frontmatter.id,
                md_file.stem,
                md_file,
            )

        # Удаляем старые записи из FTS5 (по текущему и прежнему card_id этого пути).
        # K12: для неканоничного дубля (index_content=False) FTS-запись под id
        # не создаётся и НЕ удаляется: DELETE по id выполняется только когда
        # этот путь был прежним держателем индекса (conflict=0 в file_sync
        # ДО этой синхронизации) — иначе dirty-неканоничный файл стирал бы
        # FTS-запись каноничного держателя. Безопасность порядка: sync()
        # обрабатывает canonical последним в группе, поэтому DELETE бывшего
        # держателя всегда предшествует INSERT'у нового каноничного.
        old_conflict = synced_files[file_str][3] if file_str in synced_files else 1
        was_holder = old_conflict == 0
        if index_content or was_holder:
            cursor.execute("DELETE FROM knowledge_index WHERE id = ?;", (frontmatter.id,))
        if file_str in synced_files:
            _, _, old_card_id, _ = synced_files[file_str]
            if old_card_id != frontmatter.id:
                cursor.execute("DELETE FROM knowledge_index WHERE id = ?;", (old_card_id,))

        if index_content:
            row = _index_row_values(frontmatter, sections, content)

            cursor.execute(
                """
                INSERT INTO knowledge_index (
                    id, title, category, type, status, source,
                    generated_by, created_at, stale_after, verified_count, confidence, task_id,
                    aliases, symptom, root_cause, solution, rationale, refutation_reason, tags, body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                row,
            )

        cursor.execute(
            """
            INSERT INTO file_sync (path, mtime, size, card_id, conflict)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime = excluded.mtime,
                size = excluded.size,
                card_id = excluded.card_id,
                conflict = excluded.conflict;
            """,
            (file_str, mtime, size, frontmatter.id, conflict),
        )
        return frontmatter.id

    def _apply_card_group(
        self,
        cursor: sqlite3.Cursor,
        card_id: str,
        entries: list[tuple[Path, str, float, int, CardFrontmatter, SectionCollection, str]],
        synced_files: dict[str, tuple[float, int, str, int]],
        active_card_ids: set[str],
    ) -> None:
        """Фаза II (apply) для одной группы файлов одного frontmatter.id.

        K12: canonical обрабатывается последним — DELETE неканоничного
        бывшего держателя (conflict=0 → 1) выполняется ДО INSERT'а
        каноничного в FTS. Смена держателя (canonical takeover) обязана
        переиндексировать нового canonical даже при чистом mtime — иначе
        FTS навсегда держит контент прежнего (возможно удалённого) файла.
        """
        if len(entries) > 1:
            canonical_path = resolve_canonical_card_file([entry[0] for entry in entries])
            canonical_str = str(canonical_path.resolve()) if canonical_path else None
            dup_strs = [str(e[0].resolve()) for e in entries]
            warning = (
                f"DUPLICATE_CARD_ID: '{card_id}' в {len(entries)} файлах "
                f"({', '.join(sorted(dup_strs))}); canonical: {canonical_str}"
            )
            self.last_sync_warnings.append(warning)
            logger.warning(warning)
        else:
            canonical_str = entries[0][1]

        # K12: прежние держатели id (conflict=0 до этой синхронизации).
        prev_holders = {
            p for p, (_, _, cid, conf) in synced_files.items() if cid == card_id and conf == 0
        }
        canonical_takeover = bool(prev_holders) and canonical_str not in prev_holders

        for md_file, file_str, mtime, size, frontmatter, _sections, _content in sorted(
            entries, key=lambda e: str(e[0].resolve()) == canonical_str
        ):
            is_canonical = file_str == canonical_str
            force_reindex = is_canonical and canonical_takeover
            if file_str in synced_files and not force_reindex:
                sync_mtime, sync_size, old_card_id, _ = synced_files[file_str]
                if not BaseSynchronizer.is_file_dirty(sync_mtime, sync_size, md_file):
                    # Файл не менялся: обновляем только флаг conflict
                    cursor.execute(
                        "UPDATE file_sync SET conflict = ? WHERE path = ?;",
                        (0 if is_canonical else 1, file_str),
                    )
                    if is_canonical:
                        active_card_ids.add(old_card_id)
                    continue

            self._index_card_file(
                cursor=cursor,
                md_file=md_file,
                file_str=file_str,
                mtime=mtime,
                size=size,
                synced_files=synced_files,
                conflict=0 if is_canonical else 1,
                index_content=is_canonical,
            )
            if is_canonical:
                active_card_ids.add(frontmatter.id)

    def _reindex_after_duplicate_removal(
        self,
        cursor: sqlite3.Cursor,
        card_id: str,
        del_path: str,
        parsed: dict[
            str, list[tuple[Path, str, float, int, CardFrontmatter, SectionCollection, str]]
        ],
        synced_files: dict[str, tuple[float, int, str, int]],
        active_card_ids: set[str],
    ) -> None:
        """K12: удаление дубля → переиндексация оставшегося канона, если он ещё не активен."""
        if card_id not in parsed or card_id in active_card_ids:
            return
        remaining = [e for e in parsed[card_id] if e[1] != del_path]
        if not remaining:
            return
        canonical_path = resolve_canonical_card_file([e[0] for e in remaining])
        if canonical_path is None:
            return
        self._index_card_file(
            cursor=cursor,
            md_file=canonical_path,
            file_str=str(canonical_path.resolve()),
            mtime=canonical_path.stat().st_mtime,
            size=canonical_path.stat().st_size,
            synced_files=synced_files,
            conflict=0,
            index_content=True,
        )
        active_card_ids.add(card_id)

    def sync(self) -> None:
        """Сверяет файлы в репозитории с таблицей file_sync по mtime и размеру.

        K12 rev.3.4: двухфазный sync (collect → group → apply).
        Фаза I (collect): полный обход, парсинг всех карточек, группировка по
        frontmatter.id, вычисление canonical = max(mtime_ns, size), tie-break
        min(нормализованный путь). Фаза II (apply): FTS-индексация каноничных,
        file_sync-записи (неканоничные — с conflict=1, без индексации под id).
        Итоговый индекс не зависит от порядка rglob.
        """
        self.last_sync_warnings: list[str] = []
        if not self._is_cache_dirty():
            return

        try:
            with self.storage.get_connection() as conn:
                conn.isolation_level = None
                cursor = conn.cursor()
                with _transaction(cursor):
                    cursor.execute("SELECT path, mtime, size, card_id, conflict FROM file_sync;")
                    synced_files: dict[str, tuple[float, int, str, int]] = {
                        row[0]: (row[1], row[2], row[3], row[4]) for row in cursor.fetchall()
                    }

                    # --- Фаза I: collect (без мутаций индекса) ---
                    disk_files: set[str] = set()
                    parsed: dict[
                        str,
                        list[tuple[Path, str, float, int, CardFrontmatter, SectionCollection, str]],
                    ] = {}
                    for md_file, mtime, size in self.repo.stat_cards():
                        file_str = str(md_file.resolve())
                        disk_files.add(file_str)
                        try:
                            content = md_file.read_text(encoding="utf-8")
                            frontmatter, sections = parse_card_markdown(content)
                        except Exception as err:
                            logger.warning(
                                "Пропуск невалидного файла карточки %s: %s", md_file, err
                            )
                            continue
                        parsed.setdefault(frontmatter.id, []).append(
                            (md_file, file_str, mtime, size, frontmatter, sections, content)
                        )

                    # --- Фаза II: apply ---
                    active_card_ids: set[str] = set()
                    for card_id, entries in parsed.items():
                        self._apply_card_group(
                            cursor, card_id, entries, synced_files, active_card_ids
                        )

                    deleted_paths = set(synced_files.keys()) - disk_files
                    for del_path in sorted(deleted_paths):
                        _, _, card_id, _ = synced_files[del_path]
                        drop_with_move_protection(
                            cursor,
                            del_path,
                            sync_table="file_sync",
                            index_table="knowledge_index",
                            id_column="id",
                            card_id=card_id if card_id not in active_card_ids else None,
                        )
                        # K12: удаление дубля → переиндексация оставшегося канона
                        self._reindex_after_duplicate_removal(
                            cursor, card_id, del_path, parsed, synced_files, active_card_ids
                        )

                    # Pruning устаревшей телеметрии (retention до _TOOL_CALLS_RETENTION записей)
                    _prune_tool_calls(cursor, _TOOL_CALLS_RETENTION)
        except sqlite3.OperationalError as e:
            _raise_sqlite_unavailable(
                e,
                warn_template="Сбой синхронизации базы знаний FTS5 (%s)",
                error_message="Сбой синхронизации базы знаний FTS5",
            )

    def index_single_card(self, card_id: str, path: Path) -> None:
        """Точечно индексирует или переиндексирует одну карточку без полного сканирования.

        K12 rev.3.5 canonical-протокол: перед записью в FTS — резолв всех файлов
        с данным frontmatter.id по правилу (max(mtime_ns, size), min(path));
        если переданный path — не canonical, индексация не выполняется (no-op
        с warning в last_sync_warnings: конфликт чинится на уровне файла).
        """
        self.last_sync_warnings = []
        try:
            stat = path.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except (FileNotFoundError, OSError):
            return

        file_str = str(path.resolve())
        try:
            with self.storage.get_connection() as conn:
                conn.isolation_level = None
                cursor = conn.cursor()
                with _transaction(cursor):
                    # AUDIT-ARCH-2026-10: canonical-резолв — единый владелец
                    # инварианта (CanonicalCardResolver), не локальная копия.
                    resolver = CanonicalCardResolver(self.repo.knowledge_dir)
                    same_id = resolver.files_for_id(card_id)
                    if len(same_id) > 1:
                        canonical = resolver.select_canonical(same_id)
                        if canonical is not None and str(canonical.resolve()) != file_str:
                            warning = (
                                f"INDEX_NON_CANONICAL: '{card_id}' — путь {file_str} "
                                f"неканоничен (canonical: {canonical}); индексация пропущена"
                            )
                            self.last_sync_warnings.append(warning)
                            logger.warning(warning)
                            return

                    cursor.execute(
                        "SELECT path, mtime, size, card_id, conflict FROM file_sync WHERE path = ?;",
                        (file_str,),
                    )
                    row = cursor.fetchone()
                    synced_files: dict[str, tuple[float, int, str, int]] = {}
                    if row:
                        synced_files[row[0]] = (row[1], row[2], row[3], row[4])

                    self._index_card_file(
                        cursor=cursor,
                        md_file=path,
                        file_str=file_str,
                        mtime=mtime,
                        size=size,
                        synced_files=synced_files,
                        conflict=0,
                        index_content=True,
                    )
        except sqlite3.OperationalError as e:
            _raise_sqlite_unavailable(
                e,
                warn_template="Сбой синхронизации карточки '%s' в FTS5 (%s)",
                warn_params=(card_id,),
                error_message="Сбой синхронизации базы знаний FTS5",
            )
