---
id: platform-mcp-skills-sot-architecture
title: Архитектура Single Source of Truth для MCP Skills Engine и инструмент create_skill
category: platform
type: decision
status: active
confidence: 0.75
task_id: skills-engine-sot-create-skill
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-09'
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
Исторически mcp_skills сканировал и проектный каталог (.agents/skills), и локальный клон (~/.cache/mcp-skills/repo), а SlotCompiler накладывал локальные оверлеи *.overlay.md из проекта. Это приводило к замусориванию рабочих проектов, рассинхронизации версий скиллов и отсутствию канонического инструмента создания скиллов.

### Первопричина (Root Cause)
Исторически mcp_skills сканировал и проектный каталог (.agents/skills), и локальный клон (~/.cache/mcp-skills/repo), а SlotCompiler накладывал локальные оверлеи *.overlay.md из проекта. Это приводило к замусориванию целевых рабочих проектов файлами агента, десинку версий и индекса FTS5. Кроме того, отсутствовал инструмент для создания канонических скиллов напрямую в репозитории.

### Решение / Выводы
1. Единый источник правды: все скиллы хранятся исключительно в Git-репозитории скиллов (директория skills/). Рабочие проекты разработки не сканируются синхронизатором и не содержат оверлеев.
2. Резолвер путей: resolve_skills_repo_dir(cli_dir) с приоритетом явный --skills-dir > MCP_SKILLS_PATH > MCP_SKILLS_CACHE_DIR (с разовым warning) > ~/.cache/mcp-skills/repo. get_skills_cache_dir сделан прямым делегатом.
3. Очистка SlotCompiler: удален метод _apply_overlay, чтение *.overlay.md отключено с разовым warning в лог при наличии устаревших оверлеев.
4. Новый инструмент create_skill:
   - Вход: name, title, description, content, category, stack, available_slots, overwrite.
   - Валидация: безопасное имя и слоты (kebab-case), SpamGate (секреты и размер), валидная категория (CATEGORIES или БД).
   - Защита от случайной перезаписи без overwrite=True (SkillAlreadyExistsError).
   - Атомарная запись через .tmp_<name>.<pid>_<uuid> -> replace в target_file.
   - Мгновенная переиндексация через synchronizer.sync().
   - Carve-out: выведен из-под ROOT_NOT_ANCHORED, так как пишет в репозиторий скиллов, а не в целевой проект.
5. Инструмент зарегистрирован в MCPServer skills-engine (4 инструмента: search_skills, load_skill, create_skill, step_flow), обновлены плагины Antigravity, схемы и документация.

### Обоснование / Почему именно так (Rationale)
Single Source of Truth гарантирует абсолютную изоляцию рабочих проектов от агентских файлов скиллов. Резолвер resolve_skills_repo_dir устраняет split-brain и централизует доступ. Инструмент create_skill с валидацией через SpamGate и атомарной записью исключает повреждение файлов и гарантирует немедленную доступность в FTS5.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
