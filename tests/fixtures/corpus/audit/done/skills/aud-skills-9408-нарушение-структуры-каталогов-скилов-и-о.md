---
id: aud-skills-9408
title: Нарушение структуры каталогов скилов и отсутствие поля stack
category: skills
priority: important
autonomy: high
complexity: L
status: completed
target_file: skills/atomic-file-writes/SKILL.md
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-9408: Нарушение структуры каталогов скилов и отсутствие поля stack

## ❌ Симптом
Нарушение двухуровневой структуры каталога skills/{category}/{name}/ и сбой фильтрации/автодетекта стека при сборке.

## 🔍 Техническая первопричина
Скилы atomic-file-writes, fastmcp-tool-safety, markdown-parser-boundaries лежат в корне skills/ вместо skills/backend-db/ и не имеют поля stack во frontmatter. validator-hygiene-review имеет пустой stack: []. verify_catalog.py не проверяет совпадение каталога со свойством category.

## 🛠 Предлагаемое решение
Перенести 3 скила в skills/backend-db/<name>/, прописать stack в их frontmatter и в validator-hygiene-review, добавить строгую проверку структуры каталога в scripts/verify_catalog.py.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: atomic-file-writes, fastmcp-tool-safety и markdown-parser-boundaries перемещены в skills/backend-db/; scripts/verify_catalog.py дополнен проверкой совпадения иерархии skills/<category>/<skill_name>/SKILL.md.
