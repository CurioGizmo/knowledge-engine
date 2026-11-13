---
id: platform-e2-skills-type-all
title: 'E2: воспроизведения дефектов поиска skills до фикса (флоу в type=all, реранк
  до LIMIT, fuzzy+стек, intent_group, is_ambiguous)'
category: platform
type: lesson
status: draft
task_id: null
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
confidence: 0.5
---

### Симптом / Контекст
search_skills(query='...', type='all') не возвращает флоу; fuzzy-поиск с project_stack возвращает 0 релевантных результатов; is_ambiguous срабатывает на флоу из одной intent-группы

### Первопричина (Root Cause)
E2 из docs/plans/2026-10-15-review-fixes-plan.md (батч E: поиск skills). Дефекты воспроизведены на живом каталоге /home/user/projects/mcp_skills: query+type='all' не возвращает флоу (mode=search без ключа flows); fuzzy-набор игнорирует requires_stack при наборе кандидатов; is_ambiguous top−second ≤ 5 не учитывает intent_group.

### Решение / Выводы
Воспроизведения (uv run python): (1) search_skills(query='план реализации фичи', type='all', skills_dir=/home/user/projects/mcp_skills) → mode='search', ключа flows нет; type='flow' → 7 флоу, is_ambiguous=True. (2) fuzzy: карточки py-utils (requires_stack=[python]) и ml-utilites (requires_stack=[pytorch]), query='utilites', limit=1, project_stack=['python'] → [] (fuzzy-кандидат ml-utilites занимает слот, final-фильтр его удаляет; совместимый py-utils с jaccard 0.444 теряется); без стека → [ml-utilites, py-utils]. (3) LIMIT-до-реранка: SQL top-3 по bm25 для 'plan' = plan-review, writing-plans, github-plan-publishing — после реранка порядок тот же, но кандидаты за пределами SQL LIMIT недоступны rerank (storage.py:457-489). (4) intent_group в _score_flows не участвует — только пробрасывается в arbitration_options (server.py:384).

### Обоснование / Почему именно так (Rationale)
Правило контракта для субагентов: писать промежуточные результаты на диск после каждого микрошага (прокси падал и убивал несохранённую работу агентов).

### Журнал сбоев и наблюдений (Log)
- [2026-10-16] VERIFIED: Все 5 дефектов E2 закрыты: (1) type='all' с запросом возвращает flows + flows_count + hint (server.py, ветка 4 FTS-поиска); (2) RERANK_POOL_FACTOR=4 — SQL LIMIT применяется к пулу, усечение после Python-реранка (storage.py, обе ветки search_skills); (3) fuzzy-набор и acronym-fallback фильтруются по стеку на этапе набора (_rerank_and_fuzzy_skills/_collect_short_acronym_skills с project_stack); (4) intent_group скорится: фраза +8, слово +3 (_score_one_flow); (5) is_ambiguous интент-aware: NOT ambiguous при явном одинаковом intent_group у топ-2, отсутствующая группа сохраняет legacy-поведение. Итог: 723 passed (база 715 + 8 новых), ruff clean, golden-бенчмарк 16/16 без пересборки кэша (Top-1 не сместился).

### Нюансы и ограничения (Nuances)
Все 5 дефектов E2 закрыты: (1) type='all' с запросом возвращает flows + flows_count + hint (server.py, ветка 4 FTS-поиска); (2) RERANK_POOL_FACTOR=4 — SQL LIMIT применяется к пулу, усечение после Python-реранка (storage.py, обе ветки search_skills); (3) fuzzy-набор и acronym-fallback фильтруются по стеку на этапе набора (_rerank_and_fuzzy_skills/_collect_short_acronym_skills с project_stack); (4) intent_group скорится: фраза +8, слово +3 (_score_one_flow); (5) is_ambiguous интент-aware: NOT ambiguous при явном одинаковом intent_group у топ-2, отсутствующая группа сохраняет legacy-поведение. Итог: 723 passed (база 715 + 8 новых), ruff clean, golden-бенчмарк 16/16 без пересборки кэша (Top-1 не сместился).
