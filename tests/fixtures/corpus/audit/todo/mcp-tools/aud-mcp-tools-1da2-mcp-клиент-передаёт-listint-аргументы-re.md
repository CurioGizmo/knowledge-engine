---
id: aud-mcp-tools-1da2
title: MCP-клиент передаёт list/int аргументы record_review как строку — валидация
  падает
category: mcp-tools
priority: important
autonomy: high
complexity: M
status: planned
target_file: src/mcp_audit/server.py
created_at: '2026-11-10'
updated_at: '2026-11-10'
---

# aud-mcp-tools-1da2: MCP-клиент передаёт list/int аргументы record_review как строку — валидация падает

## ❌ Симптом
record_review отклоняет comments/round: "got string, want array" при передаче корректного JSON-массива объектов ReviewComment и int round

## 🔍 Техническая первопричина
Инструменты audit-engine с параметрами list[Model] и int (record_review: comments, round) при вызове через этот клиент сериализуются как строка JSON вместо нативного массива/числа, сервер получает string и pydantic-валидация падает с "got string, want array". Инструменты без list-параметров (record_plan, update_plan, search_debt) работают.

## 🛠 Предлагаемое решение
Воспроизвести минимальным MCP-клиентом: вызвать record_review с comments=[dict]. Проверить, где теряется тип: в сериализации аргументов клиентом или в FastMCP schema. При необходимости — починить формат передачи (нативный JSON-массив) или добавить серверу приём str→json.loads для обратной совместимости.
