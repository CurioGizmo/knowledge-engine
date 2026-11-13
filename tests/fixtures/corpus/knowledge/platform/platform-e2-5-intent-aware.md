---
id: platform-e2-5-intent-aware
title: 'E2-5: согласование intent-aware is_ambiguous с legacy-тестом на флоу без intent_group'
category: platform
type: research
status: disputed
task_id: review-fixes-2026-10
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-16'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags: []
confidence: 0.4
---

### Симптом / Контекст
test_search_skills_overview_and_types падает (KeyError is_ambiguous): два флоу без intent_group с одинаковыми алиасами больше не ambiguous после введения intent-условия

### Рабочая гипотеза (Hypothesis)
Сохранить и legacy-тест (два флоу без intent_group, aliases 'выполни план', ожидание is_ambiguous=True), и новую семантику E2-5 можно так: NOT ambiguous только когда ОБЕ группы непустые И равные. Если хотя бы одна группа отсутствует (None) — старое поведение (ambiguous при top−second ≤ 5), поскольку неизвестная группа не доказывает «один интент». Формула: same_group = top_group and second_group and top_group == second_group; ambiguous = (top−second ≤ 5 and top>0 and not same_group).

### Журнал сбоев и наблюдений (Log)
- [2026-10-16] VERIFIED: Подтверждено кодом и тестами: same_group = bool(top_group) and top_group == second_group; is_ambiguous = (top−second ≤ 5) AND (top > 0) AND NOT same_group. Явно одинаковый intent_group (авто/ручной вариант одного интента) не требует арбитража; отсутствующая группа сохраняет legacy-поведение. Прогон: 21 passed (E2 + step_flow).
- [2026-10-16] DISPUTED: Сессионный шум: деталь реализации 5 строк кода, покрытая unit-тестом

### Нюансы и ограничения (Nuances)
Подтверждено кодом и тестами: same_group = bool(top_group) and top_group == second_group; is_ambiguous = (top−second ≤ 5) AND (top > 0) AND NOT same_group. Явно одинаковый intent_group (авто/ручной вариант одного интента) не требует арбитража; отсутствующая группа сохраняет legacy-поведение. Прогон: 21 passed (E2 + step_flow).
