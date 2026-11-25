---
id: aud-skills-5bc0
title: 'Ранжирование флоу бесполезно для выбора намерения: все 12 канонических flows/*.md
  имеют aliases: [] — ветка alias-скоринга в _score_flows мертва, plan-флоу tie''ятся'
category: skills
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/server.py
symbol: _score_flows
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-25'
---
# aud-skills-5bc0: Ранжирование флоу бесполезно для выбора намерения: все 12 канонических flows/*.md имеют aliases: [] — ветка alias-скоринга в _score_flows мертва, plan-флоу tie'ятся

## ❌ Симптом
Симуляция S5/F2: search_skills(query='составь план', type='flow') и query='выполни план' возвращают plan-drafting-flow и ВСЕ ТРИ исполнительских флоу (plan-execution-auto-flow, parallel-execute-flow, plan-execution-flow) с одинаковым score=6 — топ неразличим. is_ambiguous=true и директива «спросите пользователя» при этом работают корректно, но выбор варианта остаётся полностью на стороне агента/пользователя.

## 🔍 Техническая первопричина
Скоринг в src/mcp_skills/server.py:225-290 опирается на подстроку в title (+5) и description (+1) плюс alias-ветку (~L256-263); порог неоднозначности top-second <= 5 (L286). При этом grep -c aliases /home/anton/projects/mcp_skills/flows/*.md = 0 для всех 12 файлов — поле aliases пусто, поэтому alias-ветка никогда не срабатывает и разговорные формулировки ('составь план', 'выполни план', 'отладь', 'зарелизь') не имеют якоря.

## 🛠 Предлагаемое решение
Дешёвое исправление на стороне каталога: заполнить YAML frontmatter aliases у всех flows/*.md разговорными синонимами намерения (plan-drafting-flow: ['составь план','напиши план','план реализации','draft plan']; plan-execution-flow: ['выполни план пошагово','выполнить план','execute plan']; parallel-execute-flow: ['параллельно выполни план','волны субагентов']; systematic-debugging-flow: ['отладь','дебаг','почему падает'] и т.д.). Дополнительно: поднять вес alias-совпадения выше title-подстроки, чтобы точный алиас однозначно побеждал. Тест: по каждому алиасу top-1 — ожидаемый флоу, is_ambiguous=false.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: Исправлено ранее: все 14 файлов mcp_skills/flows/*.md имеют заполненные aliases, а _score_alias_matches в src/mcp_skills/server.py:340-447 корректно ранжирует интенты без tie.
