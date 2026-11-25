---
id: aud-skills-a9c3
title: 'slot_compiler в fallback печатает неверное имя: ''[Спецификация для {project_stack[0]}
  отсутствует]'' вместо запрошенного слота — вводит исполнителя в заблуждение'
category: skills
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/slot_compiler.py
symbol: _resolve_slot_content
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-25'
---
# aud-skills-a9c3: slot_compiler в fallback печатает неверное имя: '[Спецификация для {project_stack[0]} отсутствует]' вместо запрошенного слота — вводит исполнителя в заблуждение

## ❌ Симптом
Симуляция S6/load_skill(name=..., stack=['fastapi','react']): при отсутствии блока для слота react текст фолбэка сообщает '[Спецификация для fastapi отсутствует]', т.е. называет ПЕРВУЮ технологию стека вместо той, для которой блок не найден. Исполнитель идёт проверять не тот слот.

## 🔍 Техническая первопричина
В src/mcp_skills/slot_compiler.py:130-135 tech_hint берётся как project_stack[0] if project_stack else 'stack', тогда как контекст вызова — конкретный slot, для которого ищется блок. Переменная slot доступна в той же области и уже проверена через is_safe_name (L129), но в сообщение не попадает.

## 🛠 Предлагаемое решение
Подставить в сообщение имя слота (и, опционально, стек как дополнительную подсказку): f'> ⚠️ [Спецификация для слота {slot} отсутствует (стек: {", ".join(project_stack) or "не определён")}]'. То же для except-ветки L136-137. Тест: при stack=['fastapi','react'] и отсутствующем блоке react сообщение содержит 'react', а не 'fastapi'. Смежно с aud-backend-fe8e (семантика stack как имён слотов).

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: Исправлено ранее: _resolve_slot_content_unified в src/mcp_skills/slot_compiler.py:707-712,810-818 удалила подстановку project_stack[0] из fallback-сообщений.
