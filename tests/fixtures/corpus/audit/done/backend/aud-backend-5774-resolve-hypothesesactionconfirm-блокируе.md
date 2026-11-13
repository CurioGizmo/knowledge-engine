---
id: aud-backend-5774
title: 'resolve_hypotheses(action=confirm) блокируется дублем без обходного пути:
  нет force/supersedes — семантически близкую гипотезу невозможно подтвердить никогда'
category: backend
priority: critical
autonomy: high
complexity: M
status: completed
target_file: src/mcp_knowledge/models.py
symbol: HypothesisResolution
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-09'
---
# aud-backend-5774: resolve_hypotheses(action=confirm) блокируется дублем без обходного пути: нет force/supersedes — семантически близкую гипотезу невозможно подтвердить никогда

## ❌ Симптом
Симуляция T10/K7: propose_hypothesis → resolve_hypotheses(action='confirm', solution=...) возвращает DUPLICATE_FOUND, если в базе уже есть семантически близкая карточка. Передача supersedes игнорируется, параметра force нет → карточка-подтверждение не создаётся, в песочнице остаётся сиротский черновик (backend-card-9808eb09). Цикл «гипотеза → боевой урок» обрывается без возможности завершить его программно.

## 🔍 Техническая первопричина
В record_knowledge двухфазный протокол дублей реализован честно (DUPLICATE_FOUND → force → confirmed_force/supersedes/append_note, src/mcp_knowledge/server.py:109), а путь resolve_hypotheses при action='confirm' вызывает запись карточки БЕЗ проброса force/confirmed_force из HypothesisResolution. Модель HypothesisResolution (models.py:588-612) валидирует только refutation_reason для refute и solution для confirm — полей force/supersedes в схеме резолюции нет, поэтому агент физически не может разрешить коллизию.

## 🛠 Предлагаемое решение
Добавить в HypothesisResolution опциональные force: bool = False и supersedes: list[str] | None, пробросить их в вызов записи карточки на ветке confirm; при отказе возвращать тот же двухфазный DUPLICATE_FOUND с явным hint «повторите с force=true или supersedes=[...]». Альтернатива (дешевле): при дубле на confirm не падать, а возвращать status='merged' + id существующей карточки с appended note. Регресс-тесты: confirm при близкой карточке → merged/force-путь; сиротских черновиков не остаётся.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-09: K-HYP: реализована дифференциация по root_cause при confirm гипотез (_classify_confirm_candidate), добавлено действие action='duplicate' с duplicate_of без принудительного force.
