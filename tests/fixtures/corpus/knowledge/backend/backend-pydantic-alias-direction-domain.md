---
id: backend-pydantic-alias-direction-domain
title: 'Pydantic alias direction: `domain: str = Field(..., alias="category")` leaks
  forbidden term via model_dump()'
category: backend
type: lesson
status: active
task_id: audit-lifecycle-review-2026-10-11
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-11'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to:
- backend-tool-name-suffix-drift
- review-error-contract-layers-mcp
derived_from: null
aliases: []
tags: []
confidence: 0.75
---

### Симптом / Контекст
Запрещённый термин `domain` присутствует в JSON-ответах 6 из 11 MCP-тулов audit-engine (record_debt, update_debt_status, get_ready_backlog, search_debt, check_known_debt, get_task_context), несмотря на то что все имена параметров тулов уже переименованы в `category`.

### Первопричина (Root Cause)
Направление алиаса инвертировано: запрещённый термин сделан именем поля, а разрешённый — алиасом. `model_dump()` без `by_alias=True` сериализует имя поля. Никто в кодовой базе `by_alias=True` не вызывает (grep — 0 вхождений). Дополнительно `populate_by_name=True` позволяет конструкторам принимать `domain=` в runtime, но типовой контракт Pydantic их отвергает → mypy падает.

### Решение / Выводы
Инвертировать: `category: str = Field(..., alias="domain")` и все `model_dump()` → `model_dump(by_alias=True)`. Переименовать FTS-колонку `domain` (storage.py:54,200,226,243,247,279,396,725) — кэш эфемерный, `_init_db` уже умеет drift-check (storage.py:174-180) через `REQUIRED_AUDIT_FTS_COLUMNS`. Переименовать внутренние параметры `list_all_issues(domain=)`, `search_issues(domain=)`, `check_known_debt(domain=)`, `get_ready_issues(domain=)` (dag.py:60), `generate_audit_id(domain:)` (models.py:86). Убрать импорт алиаса `normalize_domain_slug` (server.py:53) — использовать `normalize_category_slug`.

### Обоснование / Почему именно так (Rationale)
Запрещённый термин прошёл ревью имён параметров (там чисто), но утекает в payload — уровень, который обычно не проверяют. Инверсия алиаса — не косметика: она определяет, какое имя видит потребитель API. Подтверждено эмпирически: `mypy src/` даёт 3× `Unexpected keyword argument "domain" for "AuditIssue"` (parser.py:154,243; server.py:257) — ровно потому, что `domain` — имя поля, а не алиас. 26 ошибок `ruff check .` и 5 `mypy` означают, что ветка не проходит CI-гейты из .github/workflows/ci.yml:31,50.

### Журнал сбоев и наблюдений (Log)
- [2026-10-11] NOTE: РЕШЕНО в текущем коде (ре-верификация 2026-10-11 20:26): AuditIssue теперь объявлен как `category: str = Field(..., alias="domain")` + `@property def domain` — ровно направление, рекомендованное этой карточкой. model_dump() эмитит "category", утечка запрещённого термина через JSON-ответы закрыта: probe по 6 MCP-тулам (record_debt, update_debt_status, get_ready_backlog, search_debt, check_known_debt, get_task_context) возвращает domain=False для всех. Карточка остаётся как валидный прецедент-паттерн (направление alias в Pydantic), симптом больше не воспроизводится.
