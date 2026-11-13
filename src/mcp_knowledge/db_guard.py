"""Единый SQLite-guard: переводит OperationalError в StorageUnavailableError.

Модуль намеренно низкоуровневый (импортирует только errors и logger), чтобы
его могли использовать storage, synchronizer и telemetry без циклических
импортов (storage импортирует synchronizer).
"""

import sqlite3
from typing import Any, NoReturn

from mcp_knowledge.errors import StorageUnavailableError
from mcp_knowledge.logger import logger

__all__ = ["_raise_sqlite_unavailable"]


def _raise_sqlite_unavailable(
    err: sqlite3.OperationalError,
    *,
    warn_template: str,
    warn_params: tuple[Any, ...] = (),
    debug_context: str | None = None,
    error_message: str,
) -> NoReturn:
    """Логирует сбой SQLite и поднимает ``StorageUnavailableError`` с исходной причиной.

    Единая точка перевода ``sqlite3.OperationalError`` → ``StorageUnavailableError``
    для storage / synchronizer / telemetry (аудит: декоратор sqlite-guard).

    Args:
        err: пойманное исключение ``sqlite3.OperationalError``.
        warn_template: шаблон warning-сообщения; последним позиционным
            аргументом подставляется имя класса ошибки (``%s``).
        warn_params: дополнительные позиционные аргументы шаблона.
        debug_context: имя метода для debug-лога; ``None`` — без контекста
            (``«Детали ошибки SQLite: %s»``).
        error_message: сообщение ошибки; имя класса ошибки добавляется в скобках.

    Raises:
        StorageUnavailableError: всегда (обёртка исходного ``err``).
    """
    logger.warning(warn_template, *warn_params, type(err).__name__)
    if debug_context is None:
        logger.debug("Детали ошибки SQLite: %s", err)
    else:
        logger.debug("Детали ошибки SQLite в %s: %s", debug_context, err)
    raise StorageUnavailableError(f"{error_message} ({type(err).__name__})") from err
