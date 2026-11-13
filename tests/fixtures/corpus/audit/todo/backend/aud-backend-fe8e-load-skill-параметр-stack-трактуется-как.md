---
id: aud-backend-fe8e
title: 'load_skill: параметр stack трактуется как имена слотов, а не технологии —
  тихая деградация и утечка сырых SLOT-маркеров в контент'
category: backend
priority: important
autonomy: medium
complexity: M
status: planned
target_file: src/mcp_skills/server.py
symbol: load_skill
task_id: mcp-simulation-skills
created_at: '2026-10-12'
updated_at: '2026-10-12'
---

# aud-backend-fe8e: load_skill: параметр stack трактуется как имена слотов, а не технологии — тихая деградация и утечка сырых SLOT-маркеров в контент

## ❌ Симптом
Симуляция S4: load_skill(name=writing-plans, stack=[fastapi,react]) не заполнил слоты mode/integration/stack-invariants, а добавил в конец мусорные секции «Спецификация реализации: fastapi отсутствует». S5/S6: без stack сырые маркеры <!-- SLOT: ... --> остаются в content.

## 🔍 Техническая первопричина
В server.py load_skill: effective_slots = stack if stack is not None else touches → стек-технологии подменяют имена слотов; описание инструмента и SKILL.md дают пример stack=[ui,backend], которого в available_slots нет. При stack=None _inject_slots не вызывается и маркеры не зачищаются. Fallback-сообщение в _resolve_slot_content печатает project_stack[0] вместо имени слота.

## 🛠 Предлагаемое решение
Валидировать stack/touches против card.available_slots с явной ошибкой+списком допустимых слотов; при stack=None заполнять все available_slots из detected_stack+default.md; исправить fallback-сообщение на имя слота; добавить в ответ detected_stack/slots_filled для наблюдаемости; синхронизировать описание инструмента.
