"""Слой доступа к файловой системе для Markdown-карточек (Write / IO)."""

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
import contextlib
import hashlib
import os
import re
from collections.abc import Callable, MutableMapping
from contextlib import contextmanager
from pathlib import Path

from mcp_core.sync import _atomic_write_text
from mcp_knowledge.errors import CardNotFoundError, StorageUnavailableError
from mcp_knowledge.models import CardFrontmatter
from mcp_knowledge.parser import parse_card_markdown, serialize_card_from_parts
from mcp_knowledge.resolver import CanonicalCardResolver


def _import_msvcrt():
    """Лениво импортирует msvcrt (Windows); None на других платформах.

    Ревью r2: card_lock без fcntl обязан деградировать в msvcrt-лок,
    а не в no-op — иначе RMW-контракт теряется на Windows.
    """
    try:
        import msvcrt

        return msvcrt
    except ImportError:
        return None


class CardRepository:
    """Репозиторий для прямого управления Markdown-файлами базы знаний."""

    LOCK_DIR_NAME = ".locks"

    def __init__(
        self,
        knowledge_dir: Path,
        index_lookup: Callable[[str], Path | None] | None = None,
        create_dir: bool = True,
    ) -> None:
        self.knowledge_dir = Path(knowledge_dir)
        if create_dir:
            self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        # AUDIT-ARCH-2026-10: канон-lookup — единственный владелец
        # (CanonicalCardResolver): file_sync conflict=0 → disk-резолв
        # по тому же правилу. Repository больше не решает сам.
        self.index_lookup = index_lookup
        self.canonical_resolver = CanonicalCardResolver(
            self.knowledge_dir, index_lookup=index_lookup
        )

    @contextmanager
    def card_lock(self, card_id: str):
        """K4: per-card advisory-лок вокруг read-modify-write карточки.

        Лок файловый (fcntl.flock, LOCK_EX) по хэшированному card_id в
        ``<knowledge_dir>/.locks/`` — сериализует конкурентные мутации одной
        карточки между процессами и потоками. Ревью r2: на платформах без
        fcntl (Windows) больше не no-op — используется msvcrt.locking
        (как _advisory_lock в audit); без обоих модулей — задокументированный
        no-op.

        Файл локa открывается БЕЗ обрезки (os.open O_CREAT|O_RDWR):
        truncate-режим мог вырезать залоченные байты у конкурирующего
        процесса на Windows.
        """
        lock_dir = self.knowledge_dir / self.LOCK_DIR_NAME
        lock_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(card_id.encode("utf-8")).hexdigest()[:16]
        lock_path = lock_dir / f"card-{digest}.lock"

        if fcntl is None:
            msvcrt = _import_msvcrt()
            if msvcrt is not None:
                # Windows: эксклюзивный лок 1 байта от позиции 0.
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        with contextlib.suppress(OSError):
                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(fd)
                return
            # Ни fcntl, ни msvcrt: документированный no-op.
            yield
            return

        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def find_card_file(self, card_id: str) -> Path | None:
        """Находит путь к файлу карточки по ее ID с защитой от Path Traversal."""
        if not card_id or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", card_id):
            return None

        # AUDIT-ARCH-2026-10: канон-lookup делегирован CanonicalCardResolver
        # (index-first с фильтром conflict=0, disk-fallback — по frontmatter.id
        # с тем же правилом канона). Быстрый путь «<category>/<id>.md по имени»
        # убран: он и попадал на неканоничный id.md при дублях.
        return self.canonical_resolver.lookup(card_id)

    def get_existing_ids(self) -> set[str]:
        """Возвращает множество существующих ID карточек."""
        return {p.stem for p in self.knowledge_dir.rglob("*.md")}

    def read_card_file(self, path: Path) -> tuple[CardFrontmatter, MutableMapping[str, str]]:
        """Считывает и парсит карточку по точному пути файла.

        Рейзит:
            StorageUnavailableError: при ошибках файлового ввода-вывода.
        """
        try:
            return parse_card_markdown(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise StorageUnavailableError(
                f"Не удалось прочитать карточку '{path.stem}' ({type(e).__name__})"
            ) from e

    def read_card(self, card_id: str) -> tuple[CardFrontmatter, MutableMapping[str, str]]:
        """Считывает и парсит карточку по ID.

        Рейзит:
            CardNotFoundError: если файл не найден.
            StorageUnavailableError: при ошибках файлового ввода-вывода.
        """
        path = self.find_card_file(card_id)
        if not path or not path.exists():
            raise CardNotFoundError(f"Карточка '{card_id}' не найдена")
        return self.read_card_file(path)

    def save_card(
        self, path: Path, fm: CardFrontmatter, sections: MutableMapping[str, str]
    ) -> Path:
        """Сериализует и атомарно перезаписывает карточку по найденному пути (in-place).

        Рейзит:
            StorageUnavailableError: при ошибках файлового ввода-вывода.
        """
        content = serialize_card_from_parts(fm, sections)
        try:
            _atomic_write_text(path, content)
        except OSError as e:
            raise StorageUnavailableError(
                f"Не удалось записать карточку '{path.stem}' ({type(e).__name__})"
            ) from e
        return path

    def write_new_card(self, category: str, card_id: str, content: str) -> Path:
        """Атомарно записывает новую карточку по каноническому пути категории из готового контента.

        Рейзит:
            StorageUnavailableError: при ошибках файлового ввода-вывода.
        """
        target_dir = self.knowledge_dir / category
        target_file = target_dir / f"{card_id}.md"
        try:
            _atomic_write_text(target_file, content)
        except OSError as e:
            raise StorageUnavailableError(
                f"Не удалось записать карточку '{card_id}' ({type(e).__name__})"
            ) from e
        return target_file

    def write_card(
        self,
        category: str,
        card_id: str,
        fm: CardFrontmatter,
        sections: dict[str, str],
    ) -> Path:
        """Сериализует и атомарно записывает карточку в каталог категории."""
        target_dir = self.knowledge_dir / category
        target_file = target_dir / f"{card_id}.md"
        content = serialize_card_from_parts(fm, sections)
        try:
            _atomic_write_text(target_file, content)
        except OSError as e:
            raise StorageUnavailableError(
                f"Не удалось записать карточку '{card_id}' ({type(e).__name__})"
            ) from e
        return target_file

    def delete_card(self, card_id: str) -> bool:
        """Удаляет файл карточки по ID, если он существует."""
        path = self.find_card_file(card_id)
        if path and path.exists():
            try:
                path.unlink()
                return True
            except OSError as e:
                raise StorageUnavailableError(
                    f"Не удалось удалить файл карточки '{card_id}' ({type(e).__name__})"
                ) from e
        return False

    def stat_cards(self) -> list[tuple[Path, float, int]]:
        """Возвращает список кортежей (path, mtime, size) для всех карточек."""
        results: list[tuple[Path, float, int]] = []
        for p in self.knowledge_dir.rglob("*.md"):
            try:
                st = p.stat()
                results.append((p, st.st_mtime, st.st_size))
            except OSError:
                continue
        return results
