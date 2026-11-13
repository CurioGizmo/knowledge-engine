---
id: aud-backend-ce17
title: Проекции ready_issues/search_debt отдают created_at/updated_at = null, хотя
  значения есть в md-файлах и в payload записи
category: backend
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_audit/storage.py
symbol: search_issues
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-09'
---
# aud-backend-ce17: Проекции ready_issues/search_debt отдают created_at/updated_at = null, хотя значения есть в md-файлах и в payload записи

## ❌ Симптом
Симуляция A1/A8: get_ready_backlog и search_debt возвращают элементы с created_at=null и updated_at=null, при этом в созданном md-файле карточки даты присутствуют, а record_debt в ответе их отдаёт. Агент не может отсортировать бэклог по возрасту долга и не видит, когда карточку последний раз трогали.

## 🔍 Техническая первопричина
Модели src/mcp_audit/models.py:147/228 объявляют created_at/updated_at как необязательные, а проекция для поисковой выдачи (и FTS-индекс артефактов) не заполняет эти поля из распарсенных метаданных — parser.py:172/284-285/345-346 даты читает и в meta кладёт, но на пути сборки компактной проекции они теряются.

## 🛠 Предлагаемое решение
Пробросить created_at/updated_at из распарсенной меты в компактные проекции search_debt/get_ready_backlog/search_artifacts (и, при необходимости, добавить в FTS-индекс как UNINDEXED-колонки для сортировки). Тест: записать долг → поиск/бэклог возвращают непустые даты, совпадающие с md-файлом.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-09: A-DATES: сохранение created_at/updated_at в audit_index (UNINDEXED) и возврат в search_debt и get_ready_backlog.
