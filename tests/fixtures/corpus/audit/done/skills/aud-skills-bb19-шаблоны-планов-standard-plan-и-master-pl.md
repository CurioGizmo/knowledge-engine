---
id: aud-skills-bb19
title: Шаблоны планов standard-plan и master-plan не содержат YAML frontmatter — падение
  parse_plan_markdown
category: skills
priority: critical
autonomy: high
complexity: M
status: completed
target_file: templates/standard-plan.template.md
symbol: parse_plan_markdown
created_at: '2026-11-14'
updated_at: '2026-11-15'
---
# aud-skills-bb19: Шаблоны планов standard-plan и master-plan не содержат YAML frontmatter — падение parse_plan_markdown

## ❌ Симптом
parse_plan_markdown падает с PLAN_FILE_CORRUPTED: Отсутствует корректный YAML frontmatter в плане при попытке разобрать план, созданный по стандартному шаблону.

## 🔍 Техническая первопричина
В templates/standard-plan.template.md и master-plan.template.md отсутствует YAML frontmatter и секция шагов не содержит ключевых слов 'шаги/этапы', из-за чего parse_plan_markdown падает с PLAN_FILE_CORRUPTED.

## 🛠 Предлагаемое решение
Добавить канонический YAML frontmatter (task_id, title, category, status, target_files) в standard-plan.template.md и master-plan.template.md, а также переименовать 'Слой 3 — Атомарные задачи' в '📋 Этапы и шаги реализации'.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-15: Добавлен YAML frontmatter и секция этапов/шагов в standard-plan.template.md и master-plan.template.md
