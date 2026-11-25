---
id: aud-docs-922b
title: 'resolve_hypotheses требует refutation_reason (>=15 символов), остальные инструменты
  — note: первый вызов всегда падает из-за несогласованности API'
category: docs
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_knowledge/models.py
symbol: HypothesisResolution
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-24'
---
# aud-docs-922b: resolve_hypotheses требует refutation_reason (>=15 символов), остальные инструменты — note: первый вызов всегда падает из-за несогласованности API

## ❌ Симптом
Симуляция K7/T10: resolve_hypotheses(resolutions=[{action:'refute', note:'...'}]) падает с валидационной ошибкой, потому что для refute обязательно поле refutation_reason длиной не менее 15 символов. При этом update_knowledge_status, review_knowledge и другие инструменты того же сервера принимают note — агент по аналогии передаёт note и получает отказ на первом же вызове.

## 🔍 Техническая первопричина
Валидатор в src/mcp_knowledge/models.py:607-611 жёстко требует self.refutation_reason для action='refute' и не принимает альтернативное имя поля; в схеме инструмента (tools/list) это требование не отражено явно, а help-тексты (help_docs.py:74, 206) описывают его только в справке get_help, до которой агент доходит не всегда.

## 🛠 Предлагаемое решение
Либо принять note как алиас refutation_reason в валидаторе (model_validator: если refutation_reason пуст и note задан — использовать note), либо вернуть в ошибке явный hint с точным именем поля и минимальной длиной. Дополнительно: отразить обязательность refutation_reason в description инструмента resolve_hypotheses и в JSON-схеме. Тест на оба имени поля.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Добавлено поле note в HypothesisResolution как fallback для refutation_reason с валидацией длины >= 15 символов с учётом strip. Обновлены docstrings и документация help_docs.py. Покрыто тестами.
