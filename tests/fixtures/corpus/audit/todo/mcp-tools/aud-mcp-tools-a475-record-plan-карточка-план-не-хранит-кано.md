---
id: aud-mcp-tools-a475
title: 'record_plan: карточка-план не хранит канонический полный документ и связь
  с файлом в ветке'
category: mcp-tools
priority: suggestion
autonomy: high
complexity: M
status: planned
target_file: src/mcp_audit/server.py
symbol: record_plan
task_id: AUDIT-PLAN-VERSIONS
created_at: '2026-11-11'
updated_at: '2026-11-25'
---

# aud-mcp-tools-a475: record_plan: карточка-план не хранит канонический полный документ и связь с файлом в ветке

## ❌ Симптом
Планы-документы (26 КБ: таблицы слотов, инварианты MERGE, критерии приёмки) не переживают сессию в карточке: агенты передают в details компактный summary, get_task_context возвращает summary; полная редакция восстановима только из файла в ветке или артефакта другой беседы. Карточка не хранит связь «редакция N ↔ канонический файл в ветке» — ветка самодостаточна только благодаря ручному копированию файла в репо.

## 🔍 Техническая первопричина
Контракт record_plan не проговаривает роль details: технически он принимает полный текст плана (файлы планов в репо уже хранят многострочные «Слой 1/2/3»-документы), но description представляет его как строку деталей, и агенты по умолчанию кладут summary. get_task_context имеет view=full, но параметр малозаметен — на практике возвращается только summary. Связки «карточка ↔ канонический файл в ветке» (document_path) в frontmatter нет. Большая часть потребности (полные immutable-снимки редакций v000N.md в репо, вне FTS-индекса, diff/restore) уже спроектирована планом AUDIT-PLAN-VERSIONS — но он версирует определение самой карточки, а не внешние документы.

## 🛠 Предлагаемое решение
1) В docstring/help record_plan и update_plan явно зафиксировать: details — место канонического полного текста плана, summary-режим не предусмотрен; 2) подсветить view=full в description get_task_context (пример в Trigger); 3) рассмотреть опциональное поле frontmatter document_path — ссылка на канонический файл в ветке, выводимую в get_task_context без копирования содержимого; 4) НЕ создавать параллельный механизм версий — базы взять из AUDIT-PLAN-VERSIONS (снимки/ETag/restore). Референс практики: skill-migration-plan v4.1 (26 КБ) жил только в артефакте беседы и требовал ручного копирования в репо.

## 📝 Актуализация триажа (2026-11-25)
- **Частично закрыто (PR #30 `AUDIT-PLAN-VERSIONS`):** реализованы `get_plan_versions`, `restore_plan_version`, `replace_details`, `replace_steps`, `expected_version`.
- **Осталось:** описать `details` как канонический полный текст плана в `record_plan_tool`/`update_plan_tool`/`help_docs.py`, подсветить `view="full"` в `get_task_context_tool`, и решить по опциональному полю `document_path`.
