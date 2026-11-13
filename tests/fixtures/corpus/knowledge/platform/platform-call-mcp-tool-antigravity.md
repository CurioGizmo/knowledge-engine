---
id: platform-call-mcp-tool-antigravity
title: Мостик call_mcp_tool хоста Antigravity сериализует arguments строкой → «Invalid
  request parameters» (-32602) на всех lazy-серверах
category: platform
type: lesson
status: active
task_id: review-fixes-2026-10
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-15'
verified:
- by: human
  at: '2026-10-16'
  note: 'Promoted to active: Утверждено пользователем: баг сериализации аргументов
    в lazy-режиме хоста Antigravity'
  action: promote
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- mcp
- antigravity
- call_mcp_tool
- jsonrpc
- '-32602'
- lazy-server
- eager
- debugging
confidence: 1.0
---
### Симптом / Контекст
Вызов lazy-MCP-тула через call_mcp_tool стабильно падает с `Invalid request parameters`, даже при минимальных корректных аргументах (task_id/title/steps). Тот же тул через stdio с arguments-объектом работает. create_server() локально поднимается без ошибок. Падает не один сервер, а ВСЕ lazy-серверы с разными рантаймами: Python (audit-engine, sqlite), TypeScript/Zod (context7: «expected record, received string»), Go (github: «cannot unmarshal string into map[string]interface{}»). Нативные eager-тулы тех же серверов работают нормально.

### Первопричина (Root Cause)
Ошибка — JSON-RPC -32602 ПРОТОКОЛЬНОГО уровня, возникает в оболочке tools/call ДО входа в код тула: мостик call_mcp_tool хоста сериализует `Arguments` в JSON-строку и кладёт в `params.arguments` строкой вместо объекта. MCP SDK валидирует оболочку `CallToolRequestParams.arguments: dict | None` (mcp_types/_types.py) и мапит ValidationError → ErrorData(-32602) в mcp/shared/jsonrpc_dispatcher.py:88-101. Поэтому типизация list-параметров тула, нормализаторы вида _normalize_str_list и JSON-схемы в ~/.gemini/.../mcp/<server>/*.json НЕ при чём — код тула не выполняется вовсе. A/B-тест: повтор 1-в-1 успешного dict-вызова прошлых дней сегодня падает → регрессия сериализации мостика на стороне хоста.

### Решение / Выводы
Диагностика: отличать протокольную ошибку от туловой — туловая выглядит как `Error executing tool <name>: N validation errors` внутри CallToolResult(is_error=True), а не как -32602. Проверить stdio-репродукцией: arguments-объект → OK, arguments-строка → -32602; контроль на заведомо рабочем сервере. Обход в зоне проекта: перевести сервер в eager (нативные тулы не проходят мостик), например mcp-audit-engine --install --eager. Корневой фикс — на стороне хоста (мостик должен передавать arguments объектом). НЕ тратить время на перегенерацию схем и нормализаторы аргументов — они ниже точки отказа.

### Обоснование / Почему именно так (Rationale)
Ловушка уводит отладку в код проекта (типы, схемы, нормализаторы), хотя дефект в транспортном слое хоста и недостижим из приложения. Кросс-рантаймовое воспроизведение (Python/TS/Go) — быстрый способ доказать, что точка отказа общая, а не в конкретном сервере.

### Журнал сбоев и наблюдений (Log)
- [2026-10-16] PROMOTED: Утверждено пользователем: баг сериализации аргументов в lazy-режиме хоста Antigravity
- [2026-10-16] VERIFIED: Подтверждено воспроизведением на Python/TS/Go серверах

### Нюансы и ограничения (Nuances)
Подтверждено воспроизведением на Python/TS/Go серверах
