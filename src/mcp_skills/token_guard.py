"""Модуль контроля бюджета токенов (Token Budget Guard)."""

from __future__ import annotations

import contextlib
import re

__all__ = ["TokenGuard"]

DEFAULT_TOKEN_BUDGET = 12000


class TokenGuard:
    """Контролирует размер Markdown-документов и скиллов в рамках лимита токенов."""

    def __init__(self, max_tokens: int = DEFAULT_TOKEN_BUDGET) -> None:
        self.max_tokens = max_tokens
        self._tokenizer = self._init_tokenizer()

    def _init_tokenizer(self) -> object | None:
        try:
            import tiktoken  # type: ignore[import-not-found]

            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None

    def count_tokens(self, text: str) -> int:
        """Подсчитывает примерное или точное количество токенов."""
        if not text:
            return 0

        if self._tokenizer is not None:
            with contextlib.suppress(Exception):
                return len(self._tokenizer.encode(text))  # type: ignore[attr-defined]

        # Офлайн-эвристика: ~4 символа на токен (латиница), ~2.5 (кириллица), ~1.0 (CJK иероглифы)
        cjk_chars = len(re.findall(r"[\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]", text))
        cyrillic_chars = len(re.findall(r"[\u0400-\u04FF]", text))
        latin_and_other = len(text) - cyrillic_chars - cjk_chars
        return int((latin_and_other / 4.0) + (cyrillic_chars / 2.5) + (cjk_chars / 1.0))

    def is_within_budget(self, text: str) -> bool:
        """Проверяет, укладывается ли текст в лимит токенов."""
        return self.count_tokens(text) <= self.max_tokens

    @staticmethod
    def _omit_examples(text: str) -> str:
        result: list[str] = []
        dropping = False
        fence = ""
        example_titles = {"примеры", "examples", "примеры реализации", "code examples"}
        for line in text.splitlines(keepends=True):
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if marker:
                chars, rest = marker.groups()
                if not fence:
                    fence = chars
                elif chars[0] == fence[0] and len(chars) >= len(fence) and not rest.strip():
                    fence = ""
            elif not fence:
                heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
                if heading and len(heading[1]) <= 2:
                    dropping = len(heading[1]) == 2 and heading[2].casefold() in example_titles
                    if dropping:
                        result.append(
                            "## Примеры\n\n> ⚠️ [Детальные примеры опущены для соблюдения лимита контекста]\n"
                        )
                        continue
            if not dropping:
                result.append(line)
        return "".join(result)

    def enforce_budget(
        self,
        text: str,
        *,
        raise_on_core_overflow: bool = False,
    ) -> tuple[str, bool]:
        """Усекает второстепенные секции (Примеры), если текст превышает лимит."""
        if self.is_within_budget(text):
            return text, False

        # Только целые необязательные секции; заголовки внутри кода не границы.
        truncated = self._omit_examples(text)

        if truncated != text and self.is_within_budget(truncated):
            return truncated, True

        # 2. Если всё ещё превышает: проверка политики отрезания обязательного ядра
        if raise_on_core_overflow:
            tokens = self.count_tokens(truncated)
            from mcp_skills.errors import AssemblyBudgetExceededError

            msg = (
                f"Обязательное ядро сборки превышает бюджет токенов ({tokens} > {self.max_tokens}). "
                "Хвостовые обязательные правила не могут быть безопасно усечены."
            )
            raise AssemblyBudgetExceededError(
                message=msg,
                estimated_tokens=tokens,
                budget=self.max_tokens,
                remediation=(
                    "Уменьшите выбор секций touches, исключите необязательные слоты или "
                    "используйте курсорное чтение ресурсов через load_skill(resource_id=..., cursor=...)."
                ),
            )

        # 3. Fallback: аккуратно обрезаем по границе параграфа
        char_limit = int(self.max_tokens * 2.5)
        raw_cut = truncated[:char_limit]

        # Ищем последнюю границу параграфа (\n\n или \n#)
        last_para = max(raw_cut.rfind("\n\n"), raw_cut.rfind("\n#"))
        safe_cut = raw_cut[:last_para] if last_para > len(raw_cut) // 2 else raw_cut

        # Закрываем открытый кодовый блок (если количество ``` нечётное)
        if safe_cut.count("```") % 2 == 1:
            safe_cut += "\n```"

        final_text = safe_cut.rstrip() + "\n\n> ⚠️ [Текст усечён по лимиту токенов]\n"

        # Гарантируем соблюдение бюджета даже при очень плотном тексте / CJK
        while not self.is_within_budget(final_text) and len(safe_cut) > 0:
            safe_cut = safe_cut[: max(0, int(len(safe_cut) * 0.75) - 1)]
            last_p = max(safe_cut.rfind("\n\n"), safe_cut.rfind("\n#"))
            if last_p > len(safe_cut) // 2 and last_p > 0:
                safe_cut = safe_cut[:last_p]
            if safe_cut.count("```") % 2 == 1:
                safe_cut += "\n```"
            final_text = (
                (safe_cut.rstrip() + "\n\n> ⚠️ [Текст усечён по лимиту токенов]\n")
                if safe_cut.strip()
                else "> ⚠️ [Текст усечён по лимиту токенов]\n"
            )

        return final_text, True
