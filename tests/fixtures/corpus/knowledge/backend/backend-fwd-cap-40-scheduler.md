---
id: backend-fwd-cap-40-scheduler
title: Широкая fwd-таблица (cap=40) НЕ пишет форму «шедулер» для scheduler — 8/10
  держится на машинных формах
category: backend
type: lesson
status: verified
task_id: translit-research-recheck
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-13'
verified:
- by: agent
  at: '2026-10-13'
  note: 'Подтверждено независимым повторным замером (6 верификатор-кластеров A–F,
    задача translit-research-recheck): все числа пересчитаны с нуля, расхождений с
    карточкой нет.'
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags: []
confidence: 0.85
---

### Симптом / Контекст
Исходник §3.5: широкая таблица чинит scheduler для dual-write через sh→ш. Review: при cap=40 ветка sch→ш обрезается комбинаторикой, «шедулер» в колонку не попадает, а 8/10 достигается за счёт второй (машинной) формы «счедулер».

### Рабочая гипотеза (Hypothesis)
forward_translit('scheduler', cap=40) не содержит форму, которую trigram-MATCH поймает запрос «шедулер»; прирост широкой таблицы в holdout живёт на формах, которые реальный пользователь не пишет.

### Журнал сбоев и наблюдений (Log)
- [2026-10-13] CONFIRMED: Hypothesis confirmed as active lesson
- [2026-10-13] VERIFIED: Подтверждено независимым повторным замером (6 верификатор-кластеров A–F, задача translit-research-recheck): все числа пересчитаны с нуля, расхождений с карточкой нет.

### Решение / Выводы
Широкая fwd-таблица при cap=40 НЕ порождает реальную пользовательскую форму «шедулер» для scheduler: ветвь sch→ш существует, но стоит на позиции 54 из 81 и обрезается cap (порог выживания cap≥55). Прирост holdout у cap=40 (8/10 any-form) держится на машинной форме «счедулер» (позиция 27), которую пользователь не пишет; first-form счёт — 5/10 при cap=40 и 6/10 при cap=100. Любую цифру recall широкой таблицы обязательно сопровождать значением cap: «5/10 first-form» — cap=40-специфична.

### Обоснование / Почему именно так (Rationale)
Кластер A5 (/tmp/opencode/A/a5_wide_fwd_scheduler.py): перебор форм fwd('scheduler') при cap=6/40/100 + FTS5 MATCH по каждой форме.

### Нюансы и ограничения (Nuances)
Подтверждено независимым повторным замером (6 верификатор-кластеров A–F, задача translit-research-recheck): все числа пересчитаны с нуля, расхождений с карточкой нет.
