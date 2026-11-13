"""F6 (review-fixes): тесты TokenGuard.enforce_budget — удаление секции «Примеры».

Дефект: examples_pattern.sub(r"\\1…") возвращал секцию «Примеры» ЦЕЛИКОМ
(группа 1 захватывала и заголовок, и тело секции) — бюджет не экономился.
Фикс: секция действительно удаляется/усекается; «Обязательные правила»
никогда не попадают под усечение.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_skills.token_guard import TokenGuard


def _make_text(examples_repeat: int = 16) -> str:
    """Текст, гарантированно превышающий бюджет за счёт секции Примеры."""
    examples_body = "\n".join(
        f"### Пример {i}\n```python\ndef example_{i}():\n    return {i}  # "
        + "x" * 3500  # длинная строка кода — раздуваем секцию (>12К токенов)
        + "\n```\n"
        for i in range(examples_repeat)
    )
    return (
        "# Скилл\n\n"
        "## Обязательные правила\n\n"
        "- Всегда пиши failing-тест перед фиксом.\n"
        "- Не трогай чужие файлы.\n"
        "- Пиши на диск после каждого микрошага.\n\n"
        "## Примеры\n\n"
        f"{examples_body}\n"
        "## Ссылки\n\n"
        "- docs/examples.md\n"
    )


def test_examples_section_removed_and_mandatory_intact() -> None:
    """Секция «Примеры» реально удаляется; «Обязательные правила» целы."""
    text = _make_text()
    guard = TokenGuard()
    assert not guard.is_within_budget(text)

    bounded, was_truncated = guard.enforce_budget(text)

    assert was_truncated is True
    assert guard.is_within_budget(bounded)
    # «Обязательные правила» НЕ резать никогда
    assert "## Обязательные правила" in bounded
    assert "failing-тест перед фиксом" in bounded
    # Тело примеров удалено (не должно пережить усечение целиком)
    assert "def example_0" not in bounded
    assert "def example_5" not in bounded


def test_examples_header_replaced_with_truncation_note() -> None:
    """После усечения остаётся маркер опущенных примеров, а не весь заголовок с телом."""
    text = _make_text()
    bounded, _ = TokenGuard().enforce_budget(text)
    # Либо секция удалена с маркером, либо заголовок остался без тела примеров
    assert "def example_" not in bounded
    assert "Текст усечён" in bounded or "опущены" in bounded or "примеры" not in bounded.lower()


def test_text_without_examples_unchanged_within_budget() -> None:
    """Текст без «Примеров», укладывающийся в бюджет, не меняется."""
    text = "# Скилл\n\n## Обязательные правила\n\n- Правило 1\n- Правило 2\n"
    bounded, was_truncated = TokenGuard().enforce_budget(text)
    assert bounded == text
    assert was_truncated is False


def test_examples_section_not_kept_whole() -> None:
    """Регресс F6: sub(r"\\1…") возвращал секцию целиком — проверяем, что длина упала."""
    text = _make_text()
    bounded, _ = TokenGuard().enforce_budget(text)
    # Секция примеров ~примерно 3.5К символов × 8; результат обязан быть заметно короче
    assert len(bounded) < len(text) * 0.5


def test_examples_end_at_new_skill_and_ignore_headings_inside_code() -> None:
    text = (
        "# First skill\n## Examples\n```text\n## fake boundary\n```\n" + "example detail\n" * 4000
    )
    text += "\n# Second skill\nMANDATORY_SECOND_SKILL_TAIL\n"
    bounded, omitted = TokenGuard().enforce_budget(text, raise_on_core_overflow=True)
    assert omitted and "MANDATORY_SECOND_SKILL_TAIL" in bounded
    assert "fake boundary" not in bounded


def test_example_heading_in_code_does_not_erase_mandatory_content() -> None:
    text = "# Skill\n```markdown\n## Examples\n```\n" + "Mandatory core line\n" * 4000
    text += "FINAL_MANDATORY_RULE"
    from mcp_skills.errors import AssemblyBudgetExceededError
    import pytest

    with pytest.raises(AssemblyBudgetExceededError):
        TokenGuard().enforce_budget(text, raise_on_core_overflow=True)
