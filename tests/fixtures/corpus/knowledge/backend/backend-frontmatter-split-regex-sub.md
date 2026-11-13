---
id: backend-frontmatter-split-regex-sub
title: 'FRONTMATTER_SPLIT_REGEX: .sub() с жадной группой 2 съедает всё тело markdown'
category: backend
type: lesson
status: draft
confidence: 0.5
task_id: d76e5b26
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-14'
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
При обрезке дублирующего frontmatter через regex.sub у контента пропадает всё тело: .sub(«», text, count=1) с группой (.*) в конце паттерна съедает документ целиком — результат пуст или содержит только вставку-замену.

### Первопричина (Root Cause)
Парсер markdown frontmatter в mcp_core/markdown.py: FRONTMATTER_SPLIT_REGEX = ^---\s*\n(.*?)\n---\s*\n(.*)$. При использовании как sub-паттерн для .sub() жадная группа 2 захватывает весь остаток текста, и замена съедает тело документа целиком.

### Решение / Выводы
Не использовать re.sub с этим паттерном. Использовать FRONTMATTER_SPLIT_REGEX.match(text) и брать .group(2) — тело документа после закрывающего '---'. В mcp_skills/creator.py сделано именно так: match := FRONTMATTER_SPLIT_REGEX.match(clean_body); clean_body = match.group(2).strip().

### Обоснование / Почему именно так (Rationale)
Дважды ловился в mcp-knowledge-engine: в creator.py (create_skill) и в скриптах обработки скилл/флоу-файлов. Баг молчаливый — текст «исчезает» без ошибки.

### Нюансы и ограничения (Nuances)
Оба случая: creator.py (create_skill) и ручные скрипки сплит-обработки флоу/скилл-файлов. Если нужен re.sub с этим паттерном — сначала покрыть тестом на сохранение тела.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
