"""Шаг 4 плана skills-correctness: валидация code fences в flow_parser."""

from __future__ import annotations

import pytest

from mcp_skills.errors import InvalidFlowFormatError
from mcp_skills.flow_parser import parse_flow_markdown

HEADER = """# Тестовый флоу

## Первый этап [id: stage_one]

Инструкция первого этапа.

## Второй этап [id: stage_two]

Инструкция второго этапа.
"""
EXPECTED_STAGES = ["stage_one", "stage_two"]


def _wrap(body: str) -> str:
    return HEADER + "\n" + body


def test_closed_backtick_and_tilde_fences_ok() -> None:
    """Корректно закрытые блоки с бэктиками и тильдами парсятся без ошибок."""
    content = _wrap(
        """```python
print("hello")
```

~~~
текст
~~~
"""
    )
    df = parse_flow_markdown(content, "fences_ok")
    assert list(df.stages) == EXPECTED_STAGES


def test_unclosed_backtick_and_tilde_fences_raise() -> None:
    """Незакрытые блоки с бэктиками и тильдами дают InvalidFlowFormatError."""
    with pytest.raises(InvalidFlowFormatError, match="Несбалансированные кодовые блоки"):
        parse_flow_markdown(_wrap("```python\nprint('hi')\n"), "unclosed_backticks")

    with pytest.raises(InvalidFlowFormatError, match="Несбалансированные кодовые блоки"):
        parse_flow_markdown(_wrap("~~~\nтекст\n"), "unclosed_tilde")


def test_indent_0_to_3_is_fence_4_is_not() -> None:
    """Отступы 0–3 пробела открывают fence; 4 пробела — индент-блок, не fence."""
    for indent in ("", " ", "  ", "   "):
        with pytest.raises(InvalidFlowFormatError, match="Несбалансированные кодовые блоки"):
            parse_flow_markdown(_wrap(f"{indent}```\ncode\n"), f"indent{len(indent)}")

    # 4 пробела: это индент-код, а не fence — ошибки быть не должно.
    df = parse_flow_markdown(_wrap("    ```\n    код\n"), "indent4")
    assert list(df.stages) == EXPECTED_STAGES


def test_four_backticks_not_closed_by_three() -> None:
    """Блок из четырёх бэктиков не закрывается строкой из трёх; 4+ закрывают."""
    with pytest.raises(InvalidFlowFormatError, match="Несбалансированные кодовые блоки"):
        parse_flow_markdown(_wrap("````\n```\n```\n"), "four_not_closed_by_three")

    # Закрытие четырьмя бэктиками — валидно; тройной маркер внутри — просто текст.
    df = parse_flow_markdown(_wrap("````\n```\n```\n````\n"), "four_closed_by_four")
    assert list(df.stages) == EXPECTED_STAGES

    # Закрытие пятью бэктиками (длина >= открывающей) тоже валидно.
    df = parse_flow_markdown(_wrap("````\nтекст\n`````\n"), "four_closed_by_five")
    assert list(df.stages) == EXPECTED_STAGES


def test_other_fence_char_or_info_does_not_close() -> None:
    """Другой символ fence или непустой info после маркера не закрывают блок."""
    # Тильда не закрывает блок из бэктиков.
    with pytest.raises(InvalidFlowFormatError, match="Несбалансированные кодовые блоки"):
        parse_flow_markdown(_wrap("```\ncode\n~~~\n"), "tilde_does_not_close_backticks")

    # Бэктики не закрывают блок из тильд.
    with pytest.raises(InvalidFlowFormatError, match="Несбалансированные кодовые блоки"):
        parse_flow_markdown(_wrap("~~~\ncode\n```\n"), "backticks_do_not_close_tilde")

    # Непустой info после закрывающего маркера не закрывает блок.
    with pytest.raises(InvalidFlowFormatError, match="Несбалансированные кодовые блоки"):
        parse_flow_markdown(_wrap("```\ncode\n``` python\n"), "info_does_not_close")


def test_heading_inside_code_block_not_section() -> None:
    """Заголовки ## внутри code block не становятся секциями flow."""
    content = _wrap(
        """```markdown
## Не этап

Это внутри блока кода.
```
"""
    )
    df = parse_flow_markdown(content, "heading_inside_fence")
    assert list(df.stages) == EXPECTED_STAGES

    content_tilde = _wrap(
        """~~~
## Тоже не этап
~~~
"""
    )
    df = parse_flow_markdown(content_tilde, "heading_inside_tilde")
    assert list(df.stages) == EXPECTED_STAGES
