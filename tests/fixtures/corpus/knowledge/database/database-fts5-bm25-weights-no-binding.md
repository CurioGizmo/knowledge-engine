---
id: database-fts5-bm25-weights-no-binding
title: 'FTS5 bm25(): аргументы-веса нельзя передать через ?-биндинги — только через
  SQL-строку'
category: database
type: hint
status: active
task_id: null
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-09-29'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to:
- review-external-recs-assessment
- database-sqlite-pragmas-canon
derived_from: null
aliases:
- bm25 веса параметризация
- fts5 auxiliary function bindings
- SQL интерполяция весов bm25
tags:
- sqlite
- fts5
- bm25
- sql
- refactoring
confidence: 0.75
---

### Симптом / Контекст
При рефакторинге BM25-весов в mcp-knowledge-engine (вынос 3 дублированных литералов в константу) есть соблазн параметризовать веса через prepared-statement биндинги

### Первопричина (Root Cause)
bm25(knowledge_index, w1, w2, ...) — вспомогательная функция FTS5, вызов является частью грамматики SQL-запроса, а не значения: параметризовать аргументы функции через ? нельзя, sqlite3 их не примет. Веса обязаны интерполироваться в строку SQL.

### Решение / Выводы
Собирать SQL-строку из контролируемой константы-кортежа: BM25_WEIGHTS: tuple[float, ...] = (...); SQL-snippet = ', '.join(str(w) for w in BM25_WEIGHTS). Обязательные условия: (1) источник строки — ТОЛЬКО кодовая константа, никогда пользовательский ввод (согласовано с существующим S608-ignore в pyproject); (2) assert len(BM25_WEIGHTS) == 19 рядом со схемой FTS-таблицы страхует константу от правок; (3) assert НЕ проверяет соответствие колонкам реальной таблицы (та создаётся отдельно в _init_db) — рассинхрон проявится только OperationalError на первом запросе, поэтому в тесты добавить кейс 'search сразу после init'.

### Обоснование / Почему именно так (Rationale)
Параметризация защищает от SQL-инъекций только VALUES, но не имена функций и их аргументы в грамматике запроса. Перенос весов в биндинги — типичная ошибка рефакторинга, которая вскрывается только в рантайме на первом же поисковом запросе.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
