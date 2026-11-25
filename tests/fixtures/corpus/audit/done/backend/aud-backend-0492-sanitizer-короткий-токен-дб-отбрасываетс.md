---
id: aud-backend-0492
title: 'sanitizer: короткий токен «дб» отбрасывается (нет в SHORT_ACRONYMS); search_skills
  type=all с query не возвращает flows'
category: backend
priority: suggestion
autonomy: high
complexity: M
status: completed
target_file: src/mcp_core/sanitizer.py
symbol: sanitize_fts_query
task_id: mcp-simulation-skills
created_at: '2026-10-12'
updated_at: '2026-11-24'
---
# aud-backend-0492: sanitizer: короткий токен «дб» отбрасывается (нет в SHORT_ACRONYMS); search_skills type=all с query не возвращает flows

## ❌ Симптом
Симуляция S1: «фастапи дб» → count=0, т.к. «дб» выкинут фильтром длины<3 (в SHORT_ACRONYMS есть бд/db, но не дб). S3: type=all с непустым query ведёт себя как skill — flows в ответе отсутствуют, хотя описание инструмента обещает общий поиск.

## 🔍 Техническая первопричина
Белый список SHORT_ACRONYMS неполон для разговорных аббревиатур; ветка type=all в search_skills при непустом query вызывает только FTS-поиск скиллов без _search_flows.

## 🛠 Предлагаемое решение
Добавить перевёрнутые/вариантные акронимы (дб, кд, пи и т.п.) либо не отбрасывать короткие токены при их малом количестве; для type=all объединять результаты скиллов и flows (compact-представление flows). Тесты на оба кейса.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: После ребейза на `main` (`AUDIT-FIX-P2`) короткий токен «дб» обрабатывается алгоритмическим детектором `is_technical_short_token` в `src/mcp_core/text.py` (согласные без гласных, `SHORT_ACRONYMS` заменён на алгоритмическую детекцию), а `search_skills(type="all", query=...)` возвращает `flows` и `flows_count` в `src/mcp_skills/server.py`. Покрыто тестами.
