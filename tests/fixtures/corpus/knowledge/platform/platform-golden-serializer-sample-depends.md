---
id: platform-golden-serializer-sample-depends
title: Golden serializer sample depends on local audit cards
category: platform
type: lesson
status: draft
task_id: unified-review-execute-flows
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-29'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags: []
confidence: 0.9
---
### Симптом / Контекст
Golden serializer test passes in main checkout but fails in a clean Git worktree.

### Рабочая гипотеза (Hypothesis)
Selecting sorted first eight audit files makes the snapshot depend on ignored local debt cards; a fixed tracked sample will pass in both checkouts.

### План проверки (Test Plan)
Restore tracked fixture keys, select cards by fixture, run test in current checkout and a clean worktree.

### Журнал сбоев и наблюдений (Log)
- [2026-10-29] CONFIRMED: Hypothesis confirmed as active lesson

### Решение / Выводы
Pin the serializer sample to tracked paths listed in golden-serializers.json, then run against tracked audit cards in a clean checkout.

### Обоснование / Почему именно так (Rationale)
The original first-eight glob changed when a local ignored debt card was added; clean archive and local checkout now both pass the golden suite.
