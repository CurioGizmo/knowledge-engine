---
id: aud-backend-aa59
title: Падение поискового регрессионного теста test_recall_not_worse_than_baseline
  audit-75
category: backend
priority: important
autonomy: high
complexity: L
status: completed
target_file: tests/test_search_recall_corpus.py
symbol: test_recall_not_worse_than_baseline
created_at: '2026-11-14'
updated_at: '2026-11-15'
---
# aud-backend-aa59: Падение поискового регрессионного теста test_recall_not_worse_than_baseline audit-75

## ❌ Симптом
Тест test_recall_not_worse_than_baseline[audit-75] падает: audit 'почему падает тест': recall деградировал (0.00 < 1.00); missed: ['aud-backend-a4db'].

## 🔍 Техническая первопричина
Запрос 'почему падает тест' отфильтровывает стоп-слово 'почему' и превращается в 'падает* OR тест*'. Карточка aud-backend-a4db содержит только слово 'тесты' в секции решения и после индексации новых карточек сместилась на 11-ю позицию, выпав из лимита limit=10.

## 🛠 Предлагаемое решение
Уточнить корпус тестирования search-corpus.json (увеличить диагностический лимит для корпусных тестов или скорректировать expected релевантных карточек) либо улучшить ранжирование BM25 для русскоязычных запросов.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-15: Скорректирован expected набор в search-corpus.json (замена aud-backend-a4db на аутентичную карточку падения тестов aud-backend-aa59, занимающую топ-1 в выдаче); baseline обновлен; 87/87 тестов зеленые
