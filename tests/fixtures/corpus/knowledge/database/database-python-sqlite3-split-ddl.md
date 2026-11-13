---
id: database-python-sqlite3-split-ddl
title: 'Python sqlite3: наивный split(";") по DDL триггеров — фрагмент END парсится
  как COMMIT и молча закрывает BEGIN IMMEDIATE'
category: database
type: lesson
status: draft
confidence: 0.5
task_id: plan-fts5-trigram-v7-review
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
Миграция FTS5-схемы в mcp-knowledge-engine: `ensure_schema` обязан пересоздать индексы ВНУТРИ одной атомарной транзакции (`BEGIN IMMEDIATE` ... `ROLLBACK`/`COMMIT`). При этом Python-обёртка

### Первопричина (Root Cause)
`sqlite3.complete_statement()` — единственная корректная граница statement'а в Python-драйвере: она знает про вложенные `;` внутри `BEGIN...END` триггеров и про строковые литералы. Наивный `split(";")` этой информации не имеет. Ключевой побочный эффект: драйвер определяет границы транзакций по тексту statement'ов, и фрагмент, состоящий из одного `END`, распознаётся как конец транзакции (implicit COMMIT) — `conn.in_transaction` становится False, хотя пользовательская транзакция `BEGIN IMMEDIATE` логически ещё открыта.

### Решение / Выводы
Разрезать многострочный SQL на statement'ы построчной аккумуляцией с `sqlite3.complete_statement()`: `for line in sql.splitlines(True): buf += line; if sqlite3.complete_statement(buf): stmts.append(buf.strip()); buf = ""` (на стенде даёт ровно 4 statement'а, 3 триггера, propagation работает). Ещё надёжнее — вообще избежать сплита: хранить каждый DDL-объект отдельной one-statement константой (`ART_AI_SQL`/`ART_AD_SQL`/`ART_AU_SQL`) и исполнять поштучно. И обязательно: тест-шпион, что в rebuild-пути нет вызовов `executescript`, иначе «зелёный» обходной путь вернёт дефект атомарности.

### Обоснование / Почему именно так (Rationale)
Ловушка двойная. (1) DDL триггеров содержит вложенные `;` — тело `BEGIN ...; ...; END` режется пополам, поэтому из 4 statement'ов получается 8 фрагментов, 7 из них падают с `OperationalError`, и ни один триггер не создаётся (проверено экспериментально на sqlite 3.45.1). (2) Хуже: осиротевший фрагмент, состоящий только из `END`, парсится Python-драйвером `sqlite3` как завершение транзакции — после него `conn.in_transaction == False`, то есть `BEGIN IMMEDIATE` был закрыт МОЛЧА, посреди rebuild. Дальнейший `ROLLBACK` в обработчике ошибок падает с `cannot rollback - no transaction is active`, и вместо атомарного отката получается частично применённая схема. Особая опасность: «быстрый фикс» — вернуть `executescript()` для триггеров — даёт ПОЛНОСТЬЮ ЗЕЛЁНЫЕ тесты (все 3 триггера создаются) при сломанной атомарности, то есть дефект уходит в прод незамеченным. Нужен отдельный тест-шпион, запрещающий executescript в rebuild-пути.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
