---
id: platform-fastmcp-literal-pydantic
title: 'FastMCP: Literal-параметры валидируются pydantic на протоколе — доменная валидация
  тех же значений недостижима'
category: platform
type: lesson
status: draft
confidence: 0.5
task_id: skills-engine-e2e-sim
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-16'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- fastmcp
- pydantic
- literal
- validation-order
- error-contract
- mcp
---

### Симптом / Контекст
FastMCP-инструмент с параметром Literal["approved","needs_work"]: передача 'maybe' не доходит до доменной валидации (ValueError 'Недопустимый вердикт' недостижим), клиент получает pydantic 'Input should be approved or needs_work [type=literal_error]' — доменное сообщение и error-envelope (reason=INVALID_ARGUMENTS) для таких значений не срабатывают.

### Первопричина (Root Cause)
FastMCP строит pydantic-модель аргументов инструмента из сигнатуры; Literal[...] порождает literal_error на границе протокола, и обработчик исключения/доменный код (ValueError с человекочитаемым текстом) не выполняется.

### Решение / Выводы
Для Literal-параметров MCP-тулов ожидать на клиенте протокольную ошибку: isError=true, текст 'Input should be ... [type=literal_error]' — доменные ассерты писать только для in-process вызовов. Если нужен доменный текст ошибки для невалидного значения — не сужать тип до Literal, принимать str и валидировать вручную (ValueError → error envelope).

### Обоснование / Почему именно так (Rationale)
Ловушка исказила ожидания E2E-симуляций: ассерт на доменное сообщение 'Недопустимый вердикт' для verdict='maybe' никогда не может пройти через MCP-транспорт, хотя юнит-тест движка (прямой вызов advance_flow) такое поведение легитимно покрывает. Важно при проектировании error-контракта: доменная валидация Literal-параметров через MCP мертва, её текст виден только in-process.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
