"""Базовые утилиты синхронизации файловой системы и перемещения карточек."""

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
import contextlib
import logging
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

__all__ = [
    "BaseSynchronizer",
    "MoveCollisionError",
    "_atomic_write_text",
    "drop_with_move_protection",
    "move_card",
]

logger = logging.getLogger("mcp_core.sync")


class MoveCollisionError(Exception):
    """A11: попытка переместить файл поверх существующего (no-clobber)."""


def _stale_sweep(dest_dir: Path, max_age_sec: float = 3600.0) -> None:
    """A11: очистка stale-артефактов (tmp-файлы, lock-файлы без живого держателя).

    PID читается ИЗ СОДЕРЖИМОГО lock-файла (имя лока PID не содержит);
    живой процесс (os.kill(pid, 0)) — артефакт не трогаем.
    """
    now = time.time()
    for entry in dest_dir.iterdir():
        try:
            if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                if now - entry.stat().st_mtime > max_age_sec:
                    entry.unlink(missing_ok=True)
            elif entry.name.endswith(".lock"):
                if now - entry.stat().st_mtime <= max_age_sec:
                    continue
                pid: int | None = None
                try:
                    pid = int(entry.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    pid = None
                if pid is not None and _pid_alive(pid):
                    continue
                # fcntl-проверка: незалоченный файл можно удалить
                if fcntl is not None:
                    try:
                        with entry.open("r+") as lf:
                            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                            entry.unlink(missing_ok=True)
                    except (OSError, BlockingIOError):
                        pass
                else:  # pragma: no cover - платформы без fcntl
                    entry.unlink(missing_ok=True)
        except OSError:
            continue


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _atomic_write_text(
    file_path: Path, content: str, encoding: str = "utf-8", overwrite: bool = True
) -> None:
    """Атомарно записывает текст в файл с использованием временного файла, fsync и Path.replace."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and file_path.exists():
        msg = f"Файл '{file_path}' уже существует."
        raise FileExistsError(msg)
    tmp_path = file_path.with_name(f".{file_path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp_path.open("w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if not overwrite and file_path.exists():
            msg = f"Файл '{file_path}' уже существует."
            raise FileExistsError(msg)
        tmp_path.replace(file_path)
    except BaseException:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def drop_with_move_protection(
    cursor: sqlite3.Cursor,
    path: str,
    sync_table: str,
    index_table: str,
    id_column: str,
    card_id: str | None = None,
) -> None:
    """Удаляет путь из sync-таблицы; при переданном card_id — запись из index-таблицы.

    Move-protection (Ф4, хвост): при переносе файла карточка переиндексируется
    по новому пути — удаление старого пути НЕ должно стирать карточку из
    индекса. Поэтому запись из index-таблицы удаляется только при явном
    ``card_id`` (вызывающий уже убедился, что карточка не переехала);
    без ``card_id`` чистится только sync-запись.

    Параметры:
        cursor: открытый курсор соединения (коммит остаётся на вызывающем).
        path: путь удаляемого файла.
        sync_table: таблица file-sync (например, ``file_sync``).
        index_table: FTS-таблица индекса (например, ``audit_index``).
        id_column: колонка ID в index-таблице (например, ``id``).
        card_id: ID записи для удаления из индекса, либо None.
    """
    if card_id is not None:
        cursor.execute(f"DELETE FROM {index_table} WHERE {id_column} = ?;", (card_id,))  # noqa: S608 — имена таблиц/колонок задаются литералами пакетов, не пользовательским вводом
    cursor.execute(f"DELETE FROM {sync_table} WHERE path = ?;", (path,))  # noqa: S608 — см. выше


def move_card(file_path: Path, target_dir: Path) -> Path:
    """Безопасно перемещает файл карточки в целевой каталог (git mv с fallback на temp-copy-rename).

    A11 (no-clobber): существующий dest НЕ перезаписывается — рейзится
    MoveCollisionError. Fallback переносим: temp-файл в dest_dir (rename
    без границы ФС), под dest-локом с PID внутри; stale-артефакты (tmp/lock
    старше часа без живого держателя) вычищаются sweep'ом.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / file_path.name
    if file_path.resolve() == dest.resolve():
        return dest

    # Primary: git mv (успех — сразу возврат)
    try:
        subprocess.run(
            ["git", "mv", str(file_path), str(dest)],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(file_path.parent),
        )
        return dest
    except Exception as err:
        logger.debug("git mv недоступен (%s), используется fallback", err)

    # A11: переносимый fallback — temp-copy-rename под dest-локом
    _stale_sweep(target_dir)
    lock_path = target_dir / f".{file_path.name}.lock"
    if fcntl is None:  # pragma: no cover - платформы без fcntl
        # no-fcntl fallback: exclusive create + PID + unlink в finally
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        try:
            _move_with_collision_check(file_path, dest, target_dir)
        finally:
            lock_path.unlink(missing_ok=True)
        return dest

    with lock_path.open("w", encoding="utf-8") as lock_file:
        lock_file.write(f"{os.getpid()}\n")
        lock_file.flush()
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _move_with_collision_check(file_path, dest, target_dir)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return dest


def _move_with_collision_check(src: Path, dest: Path, target_dir: Path) -> None:
    """A11: no-clobber перемещение src→dest через temp-файл в dest_dir."""
    if dest.exists():
        raise MoveCollisionError(
            f"Файл '{dest}' уже существует — перемещение '{src.name}' отклонено (no-clobber)"
        )
    temp = target_dir / f".{src.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        shutil.copy2(src, temp)
        # fsync temp перед rename
        fd = os.open(temp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        Path(temp).rename(dest)
        # fsync каталога
        dir_fd = os.open(target_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        src.unlink()
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


class BaseSynchronizer:
    """Базовый класс синхронизатора файловой системы и FTS5 кэша."""

    # Допуск погрешности mtime для dirty-предиката (см. is_file_dirty).
    MTIME_TOLERANCE = 1e-4

    @staticmethod
    def is_file_dirty(
        stored_mtime: float,
        stored_size: int,
        file_path: Path,
        *,
        tol: float | None = None,
    ) -> bool:
        """Проверяет, изменился ли файл на диске с допуском погрешности mtime.

        Граница (Ф4, intended-delta): дельта ровно ``tol`` — файл ЧИСТЫЙ
        (``> tol`` — dirty). До Ф4 knowledge/audit имели инвертированную
        границу (``>=`` dirty) — унификация по core-семантике; тест
        test_sync_dirty_boundary пинит границу.

        Допуск по умолчанию — :data:`MTIME_TOLERANCE` (класс-константа),
        переопределяется точечно через ``tol``.
        """
        effective_tol = BaseSynchronizer.MTIME_TOLERANCE if tol is None else tol
        try:
            stat = file_path.stat()
            return abs(stat.st_mtime - stored_mtime) > effective_tol or stat.st_size != stored_size
        except OSError:
            return True
