"""Тесты санитизации task_id (безопасность путей и имен файлов)."""

import pytest

from mcp_core.sanitizer import sanitize_task_id


def test_sanitize_task_id_valid():
    assert sanitize_task_id("PROJ-142") == "PROJ-142"
    assert sanitize_task_id("task_oauth2-v1.0") == "task_oauth2-v1.0"
    assert sanitize_task_id("simple") == "simple"
    assert sanitize_task_id("  GH-89  ") == "GH-89"


def test_sanitize_task_id_traversal_rejected():
    with pytest.raises(ValueError, match="path traversal"):
        sanitize_task_id("../../etc/passwd")

    with pytest.raises(ValueError, match="path traversal"):
        sanitize_task_id("..")

    with pytest.raises(ValueError, match="path traversal"):
        sanitize_task_id("folder/task")

    with pytest.raises(ValueError, match="path traversal"):
        sanitize_task_id("folder\\task")

    with pytest.raises(ValueError, match="path traversal"):
        sanitize_task_id("C:task")


def test_sanitize_task_id_length_and_characters():
    with pytest.raises(ValueError, match="Недопустимая длина"):
        sanitize_task_id("")

    with pytest.raises(ValueError, match="Недопустимая длина"):
        sanitize_task_id("a" * 65)

    with pytest.raises(ValueError, match="недопустимые символы"):
        sanitize_task_id("задача-123")  # non-ASCII

    with pytest.raises(ValueError, match="недопустимые символы"):
        sanitize_task_id("task with spaces")

    with pytest.raises(ValueError, match="не может заканчиваться на точку"):
        sanitize_task_id("task.")


def test_sanitize_task_id_windows_reserved():
    with pytest.raises(ValueError, match="зарезервированное имя"):
        sanitize_task_id("CON")

    with pytest.raises(ValueError, match="зарезервированное имя"):
        sanitize_task_id("nul")

    with pytest.raises(ValueError, match="зарезервированное имя"):
        sanitize_task_id("com1")
