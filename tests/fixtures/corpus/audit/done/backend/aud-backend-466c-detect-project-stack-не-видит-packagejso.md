---
id: aud-backend-466c
title: detect_project_stack не видит package.json в подкаталогах монорепо — react/nextjs/tailwind
  не определяются
category: backend
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/stack_detector.py
symbol: detect_project_stack
task_id: mcp-simulation-skills
created_at: '2026-10-12'
updated_at: '2026-11-09'
---
# aud-backend-466c: detect_project_stack не видит package.json в подкаталогах монорепо — react/nextjs/tailwind не определяются

## ❌ Симптом
Симуляция S5: песочница с frontend/package.json (react) → detect_project_stack вернул только [fastapi,pydantic,python,sqlalchemy]; react отсутствует, слоты под UI-стек не заполняются.

## 🔍 Техническая первопричина
_detect_node ищет только <root>/package.json; в монорепо фронтенд лежит в подкаталоге (frontend/, ui/, web/).

## 🛠 Предлагаемое решение
Сканировать ограниченную глубину (root + 1-2 уровня подкаталогов, исключая node_modules/.venv/dist) для package.json; аналогично для других маркеров стека; тест на монорепо-структуре.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-09: S-STACK: ограниченное обнаружение package.json в подкаталогах монорепозитория (frontend, apps/*, packages/*, services/*, modules/*) без сканирования node_modules/dist/hidden.
