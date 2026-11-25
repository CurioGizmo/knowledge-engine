---
id: aud-skills-16e6
title: Комплекс дефектов поиска сценариев, QA-валидации и стейт-машины флоу
category: skills
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/flow_engine.py
created_at: '2026-11-23'
updated_at: '2026-11-25'
---
# aud-skills-16e6: Комплекс дефектов поиска сценариев, QA-валидации и стейт-машины флоу

## ❌ Симптом
Ненахождение флоу по падежным формам и запросам с QA, ложные блокеры QA Gate на '0 tests FAILED', неявный advance стейт-машины при опечатке в action, искажение номеров итераций в истории флоу.

## 🔍 Техническая первопричина
Поиск флоу в server.py:379-473 отбрасывает 2-буквенный 'qa', не фильтрует стоп-слова, не имеет стемминга, отбрасывает FTS-бонус при нулевом лексическом скоре. QA-валидатор ложно падает на '0 tests FAILED'. step_flow выполняет advance на неизвестном action. Номера итераций в истории сбрасываются до записи.

## 🛠 Предлагаемое решение
Подключить стемминг и стоп-слова к поиску флоу, разрешить fts_bonus при score==0, исключить отрицания из регекса FAILED в QA-валидаторе, добавить валидацию action в step_flow, сохранять previous_iteration в истории.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: Закрыты все оставшиеся воспроизводимые дефекты: (1) step_flow валидирует action и падает с ValueError при опечатке (src/mcp_skills/flow_engine.py); (2) previous_iteration сохраняется до сброса в _advance_auto_stage, _advance_review_stage и resolve_flow_escalation; (3) QA-валидатор исключает отрицания вроде 0 tests FAILED в _has_unnegated_failure (src/mcp_skills/validators.py). Пункты по fts_bonus при score==0 и словарным стоп-словам отклонены архитектурой поиска (test_flow_fts_search.py::test_noise_queries_do_not_spray_catalog, src/mcp_core/text.py).
