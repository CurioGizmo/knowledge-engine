---
id: aud-mcp-tools-1da2
title: MCP-клиент передаёт list/int аргументы record_review как строку — валидация
  падает
category: mcp-tools
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_audit/server.py
created_at: '2026-11-10'
updated_at: '2026-11-13'
---
# aud-mcp-tools-1da2: MCP-клиент передаёт list/int аргументы record_review как строку — валидация падает

## ❌ Симптом
record_review отклоняет comments/round: "got string, want array" при передаче корректного JSON-массива объектов ReviewComment и int round

## 🔍 Техническая первопричина
Инструменты audit-engine с параметрами list[Model] и int (record_review: comments, round) при вызове через этот клиент сериализуются как строка JSON вместо нативного массива/числа, сервер получает string и pydantic-валидация падает с "got string, want array". Инструменты без list-параметров (record_plan, update_plan, search_debt) работают.

## 🛠 Предлагаемое решение
Воспроизвести минимальным MCP-клиентом: вызвать record_review с comments=[dict]. Проверить, где теряется тип: в сериализации аргументов клиентом или в FastMCP schema. При необходимости — починить формат передачи (нативный JSON-массив) или добавить серверу приём str→json.loads для обратной совместимости.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-11: Round 2 ревью плана SKILL-IMPORT-2026-11 сохранён вручную: карточки в .agents/reviews/ созданы скриптом, содержимое — file:///home/anton/.gemini/antigravity/brain/e857bfc9-41e7-4567-be0f-bd1ac46bb4a3/plan-review-summary.md. Ошибка сериализации list/int аргументов record_review через этот клиент подтверждена трижды (round=1 int и comments array отклоняются как "got string, want array/integer").
- 2026-11-13: Устранены anyOf-схемы у параметров list/int (target_files, add_steps, comments, spawned_debts, round), добавлена BeforeValidator коэрция JSON-строк для обратной совместимости, обновлены схемы инструментов в ~/.gemini/antigravity/mcp/audit-engine/
