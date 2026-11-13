---
id: platform-mcp-sdk-calltoolresult-is
title: 'mcp SDK: CallToolResult.is_error — Python-атрибут; isError лишь алиас сериализации
  (getattr молча врёт)'
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
- mcp-sdk
- pydantic
- calltoolresult
- is_error
- e2e-harness
- python
---

### Симптом / Контекст
В MCP-клиентском харнессе (mcp Python SDK, ClientSession.call_tool) проверка getattr(result, 'isError', False) всегда возвращает False, даже когда сервер вернул ошибку: протокольные ошибки (pydantic literal_error и др.) выглядят как успешные ответы, isError теряется, ассерты на error-пути ложно фейлятся/проходят.

### Первопричина (Root Cause)
В Pydantic v2 моделях mcp SDK алиасы (isError, structuredContent, _meta) работают только для сериализации/парсинга JSON; Python-атрибуты имеют snake_case имена (is_error). getattr по имени алиаса возвращает default.

### Решение / Выводы
Читать атрибут по Python-имени с fallback на алиас: is_error = getattr(result, 'is_error', None); if is_error is None: is_error = getattr(result, 'isError', False). Проверено на mcp SDK (CallToolResult.model_fields: is_error -> alias isError).

### Обоснование / Почему именно так (Rationale)
Ловушка стоила ложного FAIL в E2E-симуляциях skills-engine: сервер корректно возвращал isError=true (pydantic literal_error на verdict='maybe'), но харнесс не видел флага из-за неверного имени атрибута. Проверено на установленном mcp SDK: model_fields показывают is_error с alias isError; hasattr(instance,'isError')=False.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
