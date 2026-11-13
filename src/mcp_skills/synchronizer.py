"""Синхронизатор файлов скиллов и SQLite FTS5 кэша."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from mcp_core.sync import BaseSynchronizer
from mcp_skills.models import SkillCard
from mcp_skills.parser import parse_skill_markdown
from mcp_skills.storage import BLOCKS_SECTION_MARKER, SECTION_META_PREFIX, SkillsStorage

__all__ = ["SkillsSyncResult", "SkillsSynchronizer"]

logger = logging.getLogger("mcp_skills.synchronizer")


@dataclass
class SkillsSyncResult:
    indexed_count: int = 0
    removed_count: int = 0
    # D2: коллизии имён скиллов из разных каталогов — last-wins сохраняется,
    # но потребитель должен видеть факт перезаписи (раньше — только logger.warning).
    collisions: list[dict[str, str]] = field(default_factory=list)


class SkillsSynchronizer:
    """Синхронизирует Markdown-файлы скиллов с SQLite FTS5 хранилищем."""

    def __init__(self, storage: SkillsStorage, skills_dirs: list[Path]) -> None:
        self.storage = storage
        self.skills_dirs = [Path(d) for d in skills_dirs]

    def sync(self) -> SkillsSyncResult:
        """Сканирует директории скиллов, обновляет изменённые и удаляет несуществующие."""
        result = SkillsSyncResult()
        active_skills: set[str] = set()
        skill_sources: dict[str, str] = {}

        # Guard (M9): если ни одна директория физически не существует на диске
        # (репо не склонирован, сетевой диск отключён), пропускаем сканирование и cleanup,
        # чтобы случайно не стереть существующий FTS-индекс.
        if not any(d.exists() for d in self.skills_dirs):
            logger.warning("Директории скиллов недоступны на диске, очистка FTS-кэша пропущена.")
            return result

        catalog_root = self._detect_catalog_root()
        for base_dir in self.skills_dirs:
            if not base_dir.exists():
                continue

            # Ищем файлы SKILL.md и SKELETON.md.
            # D2: sorted() — детерминированный порядок обхода ФС: rglob без
            # сортировки возвращает элементы в порядке каталога ОС, из-за чего
            # last-wins при коллизии имён непредсказуем между запусками.
            for path in sorted(base_dir.rglob("*.md")):
                if path.name not in ("SKILL.md", "SKELETON.md"):
                    continue

                skill_dir = path.parent
                try:
                    text = path.read_text(encoding="utf-8")
                    card, body = parse_skill_markdown(text, base_dir=skill_dir)
                except Exception as e:
                    logger.warning("Ошибка парсинга скилла в %s: %s", path, e)
                    continue

                if card.name in active_skills:
                    logger.warning(
                        "Коллизия имени скилла '%s': файл %s перезаписывает ранее найденный",
                        card.name,
                        path,
                    )
                    result.collisions.append(
                        {
                            "skill": card.name,
                            "previous": skill_sources.get(card.name, ""),
                            "current": str(path),
                        }
                    )
                active_skills.add(card.name)
                skill_sources[card.name] = str(path)
                card_files = self._collect_skill_files(
                    skill_dir, card=card, catalog_root=catalog_root
                )
                is_dirty = self._is_skill_dirty(card.name, card_files, skill_dir)

                if is_dirty:
                    self.storage.index_skill(card, body, catalog_root=catalog_root)
                    self._update_file_sync(card.name, card_files)
                    result.indexed_count += 1

        # Удаление устаревших скиллов
        result.removed_count = self._cleanup_stale_skills(active_skills)
        return result

    def _detect_catalog_root(self) -> Path:
        """Определяет корень репозитория скиллов для общих блоков и ресурсов."""
        from mcp_core.paths import resolve_skills_repo_dir

        for d in self.skills_dirs:
            if d.name == "skills" and d.parent.exists():
                return d.parent.resolve()
            if (d / "blocks").is_dir() or (d / "skills").is_dir():
                return d.resolve()
            if d.parent and (d.parent / "blocks").is_dir():
                return d.parent.resolve()
        return resolve_skills_repo_dir()

    def _collect_skill_files(
        self,
        skill_dir: Path,
        card: SkillCard | None = None,
        catalog_root: Path | None = None,
    ) -> list[Path]:
        """Собирает все связанные файлы скилла (SKILL.md, SKELETON.md, блоки, используемые общие блоки и ресурсы)."""
        files: list[Path] = []
        for p in skill_dir.rglob("*.md"):
            files.append(p)

        if card is not None and catalog_root is not None:
            # Общие блоки для используемых слотов
            shared_blocks_dir = catalog_root / "blocks"
            if shared_blocks_dir.is_dir() and card.available_slots:
                for slot in card.available_slots:
                    slot_dir = shared_blocks_dir / slot
                    if slot_dir.is_dir():
                        for p in slot_dir.glob("*.md"):
                            if p not in files:
                                files.append(p)

            # Связанные текстовые ресурсы из SKILL.md
            skill_md = skill_dir / "SKILL.md"
            if skill_md.is_file():
                try:
                    text = skill_md.read_text(encoding="utf-8")
                    from mcp_skills.resources import extract_resources_from_markdown

                    _, res_list = extract_resources_from_markdown(
                        text, skill_md, catalog_root, card.name
                    )
                    for r in res_list:
                        rid = r.get("resource_id")
                        if rid:
                            clean_id = rid.split("#", 1)[0]
                            res_file = (catalog_root / clean_id).resolve()
                            if res_file.is_file() and res_file not in files:
                                files.append(res_file)
                except (OSError, ValueError, UnicodeError) as err:
                    logger.warning("Не удалось прочитать ресурсы %s: %s", skill_md, err)

        return sorted(list(dict.fromkeys(files)))

    def _is_skill_dirty(self, skill_name: str, files: list[Path], skill_dir: Path) -> bool:
        """Проверяет, изменился ли хотя бы один файл скилла с момента последней синхронизации."""
        with self.storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT path, mtime, size FROM file_sync WHERE skill_name = ?",
                (skill_name,),
            ).fetchall()
            stored: dict[str, tuple[float, int]] = {
                r["path"]: (r["mtime"], r["size"]) for r in rows
            }
            # D1 (review-fixes): миграция для существующих БД. Блок-файлы
            # отслеживаются в file_sync и раньше (rglob("*.md")), поэтому mtime-
            # dirty-check не заметит, что в колонке content ещё нет блочной
            # секции — индексы, построенные до D1, никогда не переиндексировались
            # бы. Проверка по маркеру закрывает ровно этот пробел: блок-файлы
            # есть на диске, а в content маркера нет — форсируем переиндексацию,
            # чтобы до-индексировать блоки. Обратный случай (маркер есть,
            # блок-файлы удалены) не требует отдельной проверки: удалённые
            # файлы исчезают из files, но остаются в stored — срабатывает
            # len-проверка ниже, content пересобирается без блочной секции.
            # Блок-файлы ищем точной проверкой принадлежности skill_dir/"blocks"
            # (а не "blocks" среди любых родителей): скилл, лежащий сам в
            # каталоге с именем blocks, не должен ложно попадать под миграцию —
            # иначе вечная переиндексация. И проверяем именно наличие файлов,
            # а не существование директории blocks/: пустой каталог без файлов
            # не форсирует dirty (маркер в content при пустом blocks/ не
            # пишется).
            blocks_root = skill_dir / "blocks"
            has_block_files = any(blocks_root in f.parents for f in files)
            content_row = conn.execute(
                "SELECT content FROM skills_index WHERE name = ?",
                (skill_name,),
            ).fetchone()
            if has_block_files and (
                content_row is None or BLOCKS_SECTION_MARKER not in content_row["content"]
            ):
                return True

            # Шаг 3 (skills-correctness-and-search): одноразовая миграция формата
            # метаданных секций без поднятия SCHEMA_VERSION. Старый payload
            # ('[resource=' / '[slot=' в блочной секции) разбирается только
            # частично (title с пробелами/']' рвётся) — форсируем переиндексацию
            # неизменённых файлов при штатном sync. Поиск ведётся только внутри
            # блочной секции (после BLOCKS_SECTION_MARKER), чтобы текст каркаса
            # с легитимным '[resource=' не давал ложного dirty. Условие точное:
            # старые маркеры есть, нового префикса '===section: ' нет. После
            # первого sync в content остаётся только новый формат — проверка
            # не повторяется на каждом поиске.
            if content_row is not None and content_row["content"]:
                parts = content_row["content"].split(BLOCKS_SECTION_MARKER, 1)
                if len(parts) == 2:
                    payload_part = parts[1]
                    if (
                        "[resource=" in payload_part or "[slot=" in payload_part
                    ) and SECTION_META_PREFIX not in payload_part:
                        return True

        if len(stored) != len(files):
            return True

        for f in files:
            path_str = str(f.resolve())
            if path_str not in stored:
                return True
            stored_mtime, stored_size = stored[path_str]
            if BaseSynchronizer.is_file_dirty(stored_mtime, stored_size, f):
                return True

        return False

    def _update_file_sync(self, skill_name: str, files: list[Path]) -> None:
        """Обновляет записи mtime и size в таблице file_sync."""
        with self.storage.get_connection() as conn:
            conn.execute("DELETE FROM file_sync WHERE skill_name = ?", (skill_name,))
            for f in files:
                try:
                    stat = f.stat()
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO file_sync(path, mtime, size, skill_name)
                        VALUES (?, ?, ?, ?)
                        """,
                        (str(f.resolve()), stat.st_mtime, stat.st_size, skill_name),
                    )
                except OSError:
                    continue
            conn.commit()

    def _cleanup_stale_skills(self, active_skills: set[str]) -> int:
        """Удаляет из базы скиллы, чьи файлы были удалены с диска."""
        removed = 0
        with self.storage.get_connection() as conn:
            rows = conn.execute("SELECT DISTINCT skill_name FROM file_sync").fetchall()
            all_tracked = [r["skill_name"] for r in rows]

            for s_name in all_tracked:
                if s_name not in active_skills:
                    conn.execute("DELETE FROM skills_index WHERE name = ?", (s_name,))
                    conn.execute("DELETE FROM file_sync WHERE skill_name = ?", (s_name,))
                    removed += 1
            conn.commit()
        return removed
