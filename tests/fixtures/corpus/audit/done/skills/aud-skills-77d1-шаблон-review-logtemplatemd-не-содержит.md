---
id: aud-skills-77d1
title: Шаблон review-log.template.md не содержит YAML frontmatter — падение parse_review_markdown
category: skills
priority: critical
autonomy: high
complexity: M
status: completed
target_file: templates/review-log.template.md
symbol: parse_review_markdown
created_at: '2026-11-14'
updated_at: '2026-11-15'
---
# aud-skills-77d1: Шаблон review-log.template.md не содержит YAML frontmatter — падение parse_review_markdown

## ❌ Симптом
parse_review_markdown падает с REVIEW_FILE_CORRUPTED: Отсутствует YAML frontmatter в файле ревью.

## 🔍 Техническая первопричина
review-log.template.md оформлен в виде произвольного Markdown без YAML frontmatter и с нестандартными колонками таблицы, что несовместимо с моделью ReviewArtifact и парсером mcp_audit.

## 🛠 Предлагаемое решение
Актуализировать review-log.template.md: добавить YAML frontmatter (id, category, round, verdict, task_id) и секцию замечаний ReviewComment.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-15: Добавлен YAML frontmatter и секция замечаний ReviewComment в review-log.template.md
