---
id: aud-backend-d83a
title: 'Поиск по умолчанию скрывает draft-карточки: агент не находит только что записанное
  знание (count=0) и дублирует работу'
category: backend
priority: critical
autonomy: high
complexity: M
status: completed
target_file: src/mcp_knowledge/storage.py
symbol: _build_search_conditions
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-09'
---
# aud-backend-d83a: Поиск по умолчанию скрывает draft-карточки: агент не находит только что записанное знание (count=0) и дублирует работу

## ❌ Симптом
Симуляция T5/T11: record_knowledge сохраняет карточку в статусе draft (карантин), после чего search_knowledge(query='sqlite wal') без include_drafts возвращает count=0, а с include_drafts=true — 2. Практический эффект: агент, который только что зафиксировал урок, при следующем поиске его не видит и либо записывает дубль, либо решает, что знания нет.

## 🔍 Техническая первопричина
В src/mcp_knowledge/storage.py:358-399 (_build_search_conditions, параметр include_drafts: bool = False, ветки L382-386) статус draft жёстко исключён из выдачи по умолчанию, а подсказка о карантине возвращается только в момент записи, не в момент поиска. Нет ни бейджа «черновик существует», ни автопромоушена после verify, ни hint в пустом результате.

## 🛠 Предлагаемое решение
Минимум: при count=0 и наличии совпадений среди draft возвращать в ответе hint (поле уже публичное — FacetedSearchResponse.hint) вида «найдено N черновиков в карантине, повторите с include_drafts=true». Лучше: показывать draft-карточки вызывающему, который их создал (совпадение task_id/agent), с badge draft и пониженным score; плюс автопромоушен draft → active после update_knowledge_status(action='verify'). Тесты на hint при пустой выдаче и на видимость своих черновиков.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-09: K-SEARCH: реализован HINT_MATCHING_DRAFTS при пустой выдаче и наличии совпадающих черновиков; закрывает пункт по d83a из AUDIT-VERIFY-2026-10.
