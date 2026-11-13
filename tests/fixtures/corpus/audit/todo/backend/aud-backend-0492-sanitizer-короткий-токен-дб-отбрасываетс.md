---
id: aud-backend-0492
title: 'sanitizer: короткий токен «дб» отбрасывается (нет в SHORT_ACRONYMS); search_skills
  type=all с query не возвращает flows'
category: backend
priority: suggestion
autonomy: high
complexity: M
status: planned
target_file: src/mcp_core/sanitizer.py
symbol: sanitize_fts_query
task_id: mcp-simulation-skills
created_at: '2026-10-12'
updated_at: '2026-10-12'
---

# aud-backend-0492: sanitizer: короткий токен «дб» отбрасывается (нет в SHORT_ACRONYMS); search_skills type=all с query не возвращает flows

## ❌ Симптом
Симуляция S1: «фастапи дб» → count=0, т.к. «дб» выкинут фильтром длины<3 (в SHORT_ACRONYMS есть бд/db, но не дб). S3: type=all с непустым query ведёт себя как skill — flows в ответе отсутствуют, хотя описание инструмента обещает общий поиск.

## 🔍 Техническая первопричина
Белый список SHORT_ACRONYMS неполон для разговорных аббревиатур; ветка type=all в search_skills при непустом query вызывает только FTS-поиск скиллов без _search_flows.

## 🛠 Предлагаемое решение
Добавить перевёрнутые/вариантные акронимы (дб, кд, пи и т.п.) либо не отбрасывать короткие токены при их малом количестве; для type=all объединять результаты скиллов и flows (compact-представление flows). Тесты на оба кейса.
