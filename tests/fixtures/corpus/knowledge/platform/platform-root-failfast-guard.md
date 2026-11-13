---
id: platform-root-failfast-guard
title: Fail-fast пишущих MCP-тулзов при незаякоренном корне проекта (ROOT_NOT_ANCHORED)
  + поля видимости root_source/project_root
category: platform
type: decision
status: active
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
- platform-mcp-project-root-server
- platform-mcp-mcp-core-mcp
- backend-installer-test-home-isolation
derived_from: null
aliases: []
tags: []
confidence: 0.75
---

### Симптом / Контекст
Живой инцидент 2026-10-07: MCP-серверы knowledge/audit/skills при незаякоренном корне молча писали карточки и debt не в проект, а в $HOME/pytest-tmp; пользователь видел «пустой радар» и терял данные без всякой ошибки.

### Первопричина (Root Cause)
Отсутствие fail-fast: при незаякоренном корне (нет MCP_PROJECT_ROOT, нет явного пути, cwd не содержит анкеров AGENTS.md/pyproject.toml/.git) серверы молча писали в fallback-корень (обычно $HOME или pytest-tmp) вместо отказа.

### Решение / Выводы
В src/mcp_core/paths.py: resolve_project_root() возвращает ResolvedRoot(root, source); write_ok = service-инъекция ИЛИ явный knowledge_dir/audit_dir/storage/synchronizer/project_root ИЛИ resolved.anchored (source начинается с env:/anchor:/explicit:). Все пишущие тулзы трёх серверов при write_ok=False бросают RootNotAnchoredError → envelope reason=ROOT_NOT_ANCHORED с инструкцией (export MCP_PROJECT_ROOT или --project-root). log_root_banner печатает INFO-баннер + warning про /tmp-корень (is_stale_tmp_root). Видимость: search_knowledge/search_debt отдают project_root, root_source, root_warning; успешные write-ответы содержат project_root. Явные пути (db_path/central cache-only тулзы) не считаются якорем для записи в проект.

### Обоснование / Почему именно так (Rationale)
Тихая запись в неверный корень — худший режим отказа: данные теряются, пользователь узнаёт постфактум. Fail-fast с actionable-инструкцией дешевле, чем миграция сиротских карточек. Видимость (project_root/root_source в ответах поиска) позволяет диагностировать десинк сразу, а не по «пустому радару» через дни. Carve-out явных путей сохраняет работоспособность service-режима и тестов без env-переменных.

### Нюансы и ограничения (Nuances)
RootNotAnchoredError — подкласс ValueError; в ERROR_RULES его ветка должна стоять ДО общей ветки ValueError, иначе маппинг схлопнется в INVALID_ARGUMENTS. is_stale_tmp_root() — эвристика по /tmp: помогает диагностировать десинк, когда сервер уже стартовал с отравленным корнем (инцидент P0 2026-10-07). db_path сам по себе НЕ заякоривает skills-сервер: запись flow всё равно идёт в корень проекта, поэтому якорь — только project_root или env/anchor. Не заякориваются тулзы, пишущие только в центральный кэш вне проекта (save_agent_override, sync_skills_repo).

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
