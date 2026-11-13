---
id: backend-use-case-protocol-messages-isolation
title: Изоляция протокольных текстов и промптов агентов в модуль messages.py вне Use
  Case логики
category: backend
type: decision
status: active
confidence: 1.0
task_id: feat-flow-selection
refuted_by: null
refutation_reason: null
generated:
  by: human
  agent: null
  created_at: '2026-10-12'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- clean-architecture
- protocol
- messages
- use-case
---

### Симптом / Контекст
При реализации интерактивных сценариев MCP-сервера (карантин черновиков draft quarantine, интерактивные подтверждения Anti-Spam Gate, Content Gate защиты от отчётов) требуется вывод многострочных инструкций, подсказок и шаблонов ответов для агентов и пользователей.

### Первопричина (Root Cause)
Размещение длинных текстов и промптов непосредственно в методах бизнес-логики Use Case классов приводит к загромождению кода, смешению инфраструктурного форматирования с доменной логикой и сложностям поддержки.

### Решение / Выводы
Выносить все протокольные сообщения, интерактивные подсказки и шаблоны ответов в централизованный модуль messages.py пакета (например, src/mcp_knowledge/messages.py) в виде констант-шаблонов (*_TEMPLATE) и типизированных функций форматирования (format_*). Методы Use Case оперируют только вызовами этих форматеров.

### Обоснование / Почему именно так (Rationale)
Разделение ответственности по Clean Architecture: Use Case управляет бизнес-правилами и состоянием, а модуль messages инкапсулирует тексты протокола и форматирование. Это исключает дублирование инструкций и облегчает их аудит.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
