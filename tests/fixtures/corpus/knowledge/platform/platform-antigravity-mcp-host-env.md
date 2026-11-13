---
id: platform-antigravity-mcp-host-env
title: Antigravity MCP host не инжектит блок "env" из mcp_config.json в stdio-процессы;
  эффективный конфиг — ~/.gemini/config/mcp_config.json
category: platform
type: lesson
status: draft
confidence: 0.5
task_id: mcp-skills-repo-wiring
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-12'
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
skills-engine после добавления `"env": {"MCP_SKILLS_PATH": ...}` в mcp_config.json и рестарта MCP-серверов по-прежнему видел 0 каталожных флоу (только встроенный feature-delivery), хотя `search_skills` возвращал 25 скиллов. Рестарт IDE не помогал.

### Первопричина (Root Cause)
Antigravity спавнит stdio-серверы только с command+args: блок env из mcp_config.json в окружение процесса не пробрасывается (проверено /proc/PID/environ после рестарта — MCP_SKILLS_PATH отсутствует, хотя в конфиге есть). При этом скиллы продолжают находиться (FTS5-кэш skills_fts.db переживает рестарты), а флоу пропадают — это маскировало проблему. Дополнительно: эффективный конфиг берётся из ~/.gemini/config/mcp_config.json, а не ~/.gemini/antigravity/mcp_config.json.

### Решение / Выводы
Все настройки путей передавать только через args (CLI-флаги): для skills-engine — `--skills-dir /path/to/skills-repo` (приоритет №1 в resolve_skills_repo_dir, выше MCP_SKILLS_PATH). Править ОБА конфига (~/.gemini/config/mcp_config.json и ~/.gemini/antigravity/mcp_config.json), т.к. какой слой эффективен — неочевидно. Ground truth проверки — /proc/<pid>/cmdline и /proc/<pid>/environ запущенного процесса, а не содержимое конфига. Диагностический маркер: скиллы видны (кэш маскирует), флоу пропали — значит repo_dir не подцепился.

### Обоснование / Почему именно так (Rationale)
Ловушка внешнего рантайма (Antigravity MCP host), не видна из кода серверов: resolve_skills_repo_dir корректно читает env, но env не доходит до процесса. Стоила 3 циклов рестарта и диагностики /proc. Диагностический приём (cmdline/environ живого процесса как ground truth вместо чтения конфига) переиспользуем для любого MCP-сервера в Antigravity, принимающего env-конфигурацию.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
