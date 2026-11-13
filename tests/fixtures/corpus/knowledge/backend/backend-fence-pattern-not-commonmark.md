---
id: backend-fence-pattern-not-commonmark
title: '`_FENCE_PATTERN` не CommonMark-совместим: `~~~` внутри ```-блока закрывает fence → обрезка секции шагов и FALSE DONE'
category: backend
type: lesson
status: verified
task_id: null
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-11'
verified:
- by: qa-redteam
  at: '2026-10-11'
  note: 'Исправлено: реализована функция check_fence_line(line, active_fence) -> (is_boundary, new_fence) в mcp_core.markdown с валидацией длины и типа символа fence.'
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- mcp_audit
- mcp_core
- markdown
- fence
- commonmark
- false-done
confidence: 0.75
---

### Симптом / Контекст
Смешанные fence-маркеры приводят к преждевременному закрытию блока кода: следующий за ним `## ` ошибочно распознаётся как граница секции Markdown. Реальные шаги/чекбоксы после блока выпадают из секции, и отметка последнего распознанного шага преждевременно переводит план в статус DONE.

### Первопричина (Root Cause)
Упрощённый шаблон `re.compile(r"^(`{3,}|~{3,})[^`~]*$")` переключал флаг состояния вслепую (`in_fence = not in_fence`). По стандарту CommonMark закрывающий fence обязан использовать тот же символ (` или ~`) и иметь длину, большую либо равную открывающему маркеру. Строка `~~~` внутри блока, открытого с помощью ```, является обычным содержимым, а не маркером закрытия.

### Решение / Выводы
Хранить состояние открывающего fence в виде кортежа `(fence_char, fence_len)` и считать строку закрытием только при совпадении символа, длине >= открывающей и отсутствии info-строки. Реализовано в `mcp_core.markdown.check_fence_line(line, active_fence)` и используется во всех процедурах парсинга секций.

### Обоснование / Почему именно так (Rationale)
Наивный boolean-переключатель ломается на вложенных блоках и примерах кода в Markdown. Точное отслеживание символа и длины маркера соответствует спецификации CommonMark.

### Условия и окружение (Conditions)
Любые Markdown-документы (планы, карточки знаний), содержащие внутри fenced code block на ``` строки `~~~` (или наоборот).

### Журнал сбоев и наблюдений (Log)
- [2026-10-11] VERIFIED: Проверено тестом `test_mixed_fence_markers_commonmark_compliance`. Несоответствующие маркеры больше не приводят к рассинхронизации состояния парсера.

### Нюансы и ограничения (Nuances)
Маркеры с отступом более 3 пробелов по спецификации CommonMark не являются fence-блоками и также должны игнорироваться регулярным выражением (`^ {0,3}`).
