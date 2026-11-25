---
id: aud-backend-b97b
title: Утечка и underflow счётчика lease в пуле сервисов ProjectServicePool
category: backend
priority: critical
autonomy: high
complexity: M
status: completed
target_file: src/mcp_core/service_pool.py
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-backend-b97b: Утечка и underflow счётчика lease в пуле сервисов ProjectServicePool

## ❌ Симптом
Сбой фабрики бандла может декрементировать чужой счётчик аренды lease до 0 и закрыть активную SQLite-базу параллельного запроса.

## 🔍 Техническая первопричина
В src/mcp_core/service_pool.py:177 canonical регистрируется в scope до вызова self.acquire(root). При исключении в фабрике release_scope декрементирует lease чужого параллельного запроса.

## 🛠 Предлагаемое решение
Добавлять canonical в scope только после успешного возврата из self.acquire(root).

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: В src/mcp_core/service_pool.py:177 перенесён scope[canonical]=None строго после успешного self.acquire(root), устранив утечку и underflow счетчика активных лиз. Покрыто тестами в test_plan_f_scoped_leases.py (AUDIT-FIX-P0).
