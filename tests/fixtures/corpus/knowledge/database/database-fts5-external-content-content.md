---
id: database-fts5-external-content-content
title: 'FTS5: у external-content таблицы без content=/content_rowid= триггеры с командой
  ''delete'' молча ломают все DELETE/UPDATE'
category: database
type: lesson
status: draft
confidence: 0.5
task_id: plan-fts5-trigram-v6-review
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-13'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags: []
---

### Симптом / Контекст
При миграции токенизатора FTS5 (unicode61 → trigram) в монорепо mcp-knowledge-engine пересоздаётся virtual-таблица `artifacts_fts` через helper `fts5_table_sql(...)`. В исходной DDL

### Первопричина (Root Cause)
Без `content=`/`content_rowid=` SQLite создаёт ОБЫЧНУЮ (regular) FTS5-таблицу с собственным хранилищем контента. Служебная команда `'delete'`, которую триггеры отправляют FTS5, определена только для external-content таблиц: в regular-режиме она не «удаляет документ из индекса по rowid», а интерпретируется как некорректная и поднимает `sqlite3.OperationalError: SQL logic error` на ЛЮБОМ DELETE/UPDATE контент-таблицы. Тесты при этом остаются зелёными: CREATE TABLE, INSERT и SELECT/MATCH работают одинаково в обоих режимах, ломается только delete/update-путь, который покрыт отдельными сценариями.

### Решение / Выводы
Пересоздавая external-content FTS5-таблицу, всегда явно передавать `content='<контентная таблица>'` и `content_rowid='<колонка>'` в DDL-билдер. Обязательный дискриминатор-тест, отличающий режимы: (1) посеять строки в контентную таблицу ДО создания FTS, (2) выполнить `INSERT INTO <fts>(<fts>) VALUES('rebuild')`, (3) проверить matches > 0 — у regular-таблицы 'rebuild' не подтягивает контент и даёт ровно 0 матчей. Дополнительно ассерт по тексту схемы из `sqlite_master.sql`: содержит `content='...'` и `content_rowid='...'` (сравнение по whitespace-нормализованному тексту — SQLite хранит DDL дословно, ничего не нормализуя), плюс тесты DELETE- и UPDATE-путей.

### Обоснование / Почему именно так (Rationale)
Дефект молчаливый по двум причинам: (1) DDL без content= полностью валиден — падает не на создании, а на первой мутации контент-таблицы; (2) стандартные тесты поиска его не ловят, нужен дискриминатор (seed-before-create + 'rebuild' → matches>0 только у external-content, у regular ровно 0). Команда `'delete'` в FTS5 определена исключительно для external-content таблиц — это поведение задокументировано, но визуально из DDL неочевидно. Радиус поражения в проде: drop_artifact_path (audit storage.py:637) ← sync_artifacts (synchronizer.py:364/366), update_plan_details (:526), advance_plan_step (:576).

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
