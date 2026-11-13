"""Централизованная настройка логирования для MCP Knowledge Engine."""

import logging
import sys


def get_logger(name: str = "mcp_knowledge") -> logging.Logger:
    """Возвращает настроенный логгер с идемпотентным добавлением обработчика."""
    log = logging.getLogger(name)
    log.propagate = False
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s")
        )
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    return log


logger = get_logger("mcp_knowledge")
