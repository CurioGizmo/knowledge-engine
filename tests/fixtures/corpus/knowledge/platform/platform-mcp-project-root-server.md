---
id: platform-mcp-project-root-server
title: Привязка корня проекта (MCP_PROJECT_ROOT), канонический server_key skills-engine
  и устранение суффикса _tool в MCP-серверах
category: platform
type: decision
status: active
task_id: root-anchor-plan-v030
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-02'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to:
- backend-mcp-skills
- backend-tool-name-suffix-drift
- docs-audit-3-mcp
- platform-root-failfast-guard
- backend-installer-test-home-isolation
derived_from: null
aliases: []
tags: []
confidence: 0.75
---

### Симптом / Контекст
При запуске MCP-серверов в IDE Antigravity с cwd=$HOME карточки знаний оседали в ~/.agents/knowledge/ вместо проекта, а инструменты audit/skills имели расхождения между зарегистрированными именами (*_tool) и lazy-схемами.

### Первопричина (Root Cause)
Разнобой в определении корня проекта (cwd-only поиск), ключах серверов ('skills' vs 'skills-engine') и именах инструментов (*_tool vs схемы) приводил к десинку карточек при запуске из $HOME и ошибкам ToolError при Lazy-вызовах.

### Решение / Выводы
1. MCP_PROJECT_ROOT в paths.py с наивысшим приоритетом.
2. Флаг --project-root во всех 3 серверах и автоматическая запись в args инсталлятором.
3. 13 декораторов @server.tool(name=...) в audit/skills с каноническими именами.
4. Миграция server_key 'skills' -> 'skills-engine' с очисткой устаревших схем.
5. Расширение SKILLS_TOOLS_LIST до 8 инструментов и гармонизация флагов инсталляторов.

### Обоснование / Почему именно так (Rationale)
Изоляция корня проекта исключает попадание карточек знаний в ~/.agents/ при старте серверов в средах с cwd=$HOME (Antigravity, Cursor). Единый стиль именования инструментов и ключей серверов предотвращает сбои резолвинга при Lazy-вызовах.

### Нюансы и ограничения (Nuances)
- MCP_PROJECT_ROOT обрабатывается в _find_default_project_root() с валидацией .is_dir() и предупреждением в лог.
- server_config в mcp_config.json всегда снабжается args: ['--project-root', str(root)].
- Старый server_key 'skills' автоматически мигрируется на 'skills-engine' при повторном запуске инсталлятора, очищая устаревший каталог схем ~/.gemini/antigravity/mcp/skills.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
