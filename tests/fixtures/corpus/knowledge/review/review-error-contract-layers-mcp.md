---
id: review-error-contract-layers-mcp
title: 'Слои обработки ошибок MCP: Storage типизирован, единый error-контракт на уровне Server'
category: backend
type: lesson
status: active
confidence: 0.9
task_id: null
refuted_by: null
refutation_reason: null
generated:
  by: human
  agent: null
  created_at: '2026-09-29'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to:
- backend-pydantic-alias-direction-domain
derived_from: null
aliases:
- storage-error-contract-mcp
- storage-unavailable-error
tags:
- error-handling
- mcp
- architecture
- layers
---

### Симптом / Контекст
Возврат ad-hoc словарей `{"error": ...}` из слоя данных (`storage.py`) приводит к нарушению типизации (`AttributeError`, `ValidationError`) и ложным срабатываниям валидации в вызывающих сервисах.

### Первопричина (Root Cause)
1) **Нарушение типизации**: Методы `storage` типизированы моделями или кортежами (например, `list[KnowledgeSnippet]`, `str | None`). Возврат словаря с ошибкой вместо выброса исключения ломает обработку в `server.py` и проверки дубликатов (`truthy dict` ошибочно трактуется как найденный ID дубликата).
2) **Разрозненность форматов**: Возврат ошибок из внутренних слоев порождает параллельные несовместимые структуры ответов об ошибках.

### Решение / Выводы
1) **Строгое разделение слоев**: Слой хранилища (`storage.py`) никогда не возвращает словари с ошибками. При сбоях БД выбрасываются доменные исключения (например, `StorageUnavailableError`), а при штатном отсутствии данных возвращается пустой список/`None`.
2) **Единая точка конвертации ошибок**: Формирование JSON-ответов об ошибках (`{"error": True, ...}`) выполняется исключительно на границе протокола в `server.py` через централизованные обработчики.

### Обоснование / Почему именно так (Rationale)
Изоляция слоя хранения сохраняет строгую типизацию внутренних сервисов и гарантирует консистентность формата ошибок, отдаваемых AI-агентам через протокол MCP.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
