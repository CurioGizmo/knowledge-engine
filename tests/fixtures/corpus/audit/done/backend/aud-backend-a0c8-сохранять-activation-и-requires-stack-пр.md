---
id: aud-backend-a0c8
title: Сохранять activation и requires_stack при create_skill(overwrite=True)
category: backend
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/creator.py
symbol: create_skill
created_at: '2026-10-15'
updated_at: '2026-11-25'
---
# aud-backend-a0c8: Сохранять activation и requires_stack при create_skill(overwrite=True)

## ❌ Симптом
creator.py:225-236 молча теряет activation и requires_stack при create_skill(overwrite=True)

## 🔍 Техническая первопричина
creator.py собирает frontmatter из фиксированного набора полей без activation и requires_stack

## 🛠 Предлагаемое решение
Считывать существующие activation и requires_stack при overwrite=True или расширить сигнатуру create_skill

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: Исправлено ранее: create_skill(overwrite=True) в src/mcp_skills/creator.py:287-321 перечитывает существующий SKILL.md и сохраняет activation, requires_stack и др. поля (тесты tests/test_skills_creator.py:1256-1330).
