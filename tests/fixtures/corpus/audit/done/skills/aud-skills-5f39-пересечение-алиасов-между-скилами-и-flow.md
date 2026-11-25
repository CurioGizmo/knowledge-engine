---
id: aud-skills-5f39
title: Пересечение алиасов между скилами и flow и слепая проверка валидатора
category: skills
priority: important
autonomy: high
complexity: M
status: completed
target_file: scripts/check_aliases.py
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-5f39: Пересечение алиасов между скилами и flow и слепая проверка валидатора

## ❌ Симптом
Неоднозначность маршрутизации при search_skills(type='all'): одновременное совпадение скила и сценария с максимальным скором; взвод флага is_ambiguous.

## 🔍 Техническая первопричина
Одинаковые разговорные фразы назначены и скиллам, и сценариям (5+ пар). Валидатор scripts/check_aliases.py:234 слеп к этому, проверяя коллизии только внутри одной категории (len(cats) == 1).

## 🛠 Предлагаемое решение
Развести интенты: для flows использовать глаголы запуска процессов, для скилов - интенты доктрины/чеклистов. В check_aliases.py проверять межсущностные коллизии между skills и flows.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Разведены все коллизии между флоу и скилами (brainstorming, live-qa, plan-drafting, changelog-automation/release-flow, review-flow); scripts/check_aliases.py проверяет любые межкарточные пересечения.
