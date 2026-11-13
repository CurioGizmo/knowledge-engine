---
id: database-sqlite-pragmas-canon
title: SQLite-канон прагм в mcp_core.db.get_connection (WAL, busy_timeout, synchronous,
  foreign_keys=ON)
category: database
type: decision
status: active
confidence: 0.75
task_id: audit-followups-2026-10-07
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-07'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to:
- platform-mcp-mcp-core-mcp
- database-fts5-bm25-weights-no-binding
derived_from: null
aliases: []
tags: []
---

### Симптом / Контекст
PRAGMA-настройки (WAL, busy_timeout, foreign_keys) дублировались в трёх модулях и расходились: foreign_keys нигде не включался системно, WAL настраивался дважды в knowledge storage.

### Первопричина (Root Cause)
Каждый модуль (knowledge storage, skills flow_storage, core db) сам настраивал прагмы; дубликаты WAL расходились, FK нигде не включался системно.

### Решение / Выводы
Канонический набор PRAGMA в src/mcp_core/db.py::get_connection: journal_mode=WAL, busy_timeout=5000, synchronous=NORMAL, foreign_keys=ON. Дублирующий WAL-pragma удалён из mcp_knowledge/storage.py::_init_db, избыточный FK-pragma — из mcp_skills/flow_storage.py::get_connection (комментарий указывает на канон в core). Новые подключения к SQLite обязаны идти через mcp_core.db.get_connection.

### Обоснование / Почему именно так (Rationale)
Единая точка конфигурации соединений исключает дрейф прагм между модулями (раньше WAL/FK дублировались и расходились по серверам). foreign_keys=ON по умолчанию в SQLite выключен — без канонической прагмы FK-ограничения молча не enforce'ятся, что маскирует баги целостности. busy_timeout=5000 + WAL минимизируют SQLITE_BUSY при конкурентной записи из MCP-диспатча.

### Нюансы и ограничения (Nuances)
telemetry.py оставлен нетронутым намеренно: там mode=ro (read-only) подключение, прагмы записи неприменимы. Если новый модуль заводит своё подключение — использовать mcp_core.db.get_connection, а не sqlite3.connect напрямую.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
