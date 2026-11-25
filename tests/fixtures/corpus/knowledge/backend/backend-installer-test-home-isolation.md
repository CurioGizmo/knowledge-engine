---
id: backend-installer-test-home-isolation
title: 'Инсталлер-тесты обязаны изолировать HOME/--no-antigravity: pytest отравил
  глобальный mcp_config.json живого клиента (двукратно, 5 карточек утеряно)'
category: backend
type: lesson
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
- platform-root-failfast-guard
- platform-mcp-project-root-server
derived_from: null
aliases: []
tags: []
---

### Симптом / Контекст
После прогона pytest «пустой радар» в живой сессии: MCP-сервер knowledge писал карточки в /tmp/pytest-of-anton/pytest-666/..., оба реальных mcp_config.json перезаписаны на pytest-668; 5 карточек утеряны безвозвратно.

### Первопричина (Root Cause)
tests/test_faceted_search.py вызывал run_installer([...]) без --no-antigravity и без подмены HOME; инсталлер по умолчанию (--antigravity=True) писал --project-root /tmp/pytest-of-anton/pytest-N/... в реальные ~/.gemini/config/mcp_config.json и ~/.gemini/antigravity/mcp_config.json. Уже запущенный MCP-сервер сессии при этом продолжал обслуживаться со старым отравленным корнем (pytest-666), т.е. конфиги и рантайм рассинхронизировались двукратно.

### Решение / Выводы
Любой тест, вызывающий run_installer, ОБЯЗАН: (1) изолировать HOME (monkeypatch HOME на tmp_path) или передавать явные пути конфигов; (2) использовать --no-antigravity, если не тестирует именно запись конфигов Antigravity. Фикс P0 (commit 64e6f2c): HOME-изоляция в installer-тесте. Регресс-гард: тесты инсталлера не должны трогать ~/.gemini — проверка через tmp HOME. Диагностику подобных десинков делать немедленно: /proc/<pid>/cmdline живого сервера + mtime mcp_config.json; сиротские карточки в /tmp/pytest-* мигрировать до ротации tmp.

### Обоснование / Почему именно так (Rationale)
Инсталлер — единственный компонент с побочным эффектом на ГЛОБАЛЬНУЮ конфигурацию живого MCP-клиента пользователя; любой тест, зовущий run_installer без изоляции, мутирует реальную среду. Двойное отравление показало, что поражаются оба конфига и что уже запущенный сервер сессии продолжает обслуживать мусорный корень даже после починки конфигов (нужен рестарт IDE). Временные каталоги pytest не переживают ротацию — знание, записанное туда, теряется навсегда.

### Нюансы и ограничения (Nuances)
Полный прогон pytest до P0-фикса воспроизводил отравление — верификацию делать точечно (после фикса прогон безопасен). Ротация pytest tmp (pytest-of-anton/pytest-N хранит ~3 последних) удалила сиротские карточки до того, как их успели мигрировать — цена инцидента: 5 карточек утеряны безвозвратно (DreamN byok, tabs-2-14, antigravity, antigravity-hub-2-21, mcp-knowledge-engine-3). После P1-гварда отравление больше не тихо: banner + root_source + is_stale_tmp_root warning + ROOT_NOT_ANCHORED на запись в незаякоренный корень.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
