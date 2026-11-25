---
id: platform-code-fences-regex-markdown
title: Изоляция code-fences при regex-валидации относительных ссылок в Markdown-каталоге
category: platform
type: lesson
status: active
task_id: null
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-11-17'
verified:
- by: human
  at: '2026-11-17'
  note: 'Promoted to active: Подтверждено пользователем: ловушка с ложными ссылками
    в code-fences проверена на практике в verify_catalog.py'
  action: promote
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
Парсер ссылок re.findall(r'\]\(([^)]+)\)') падает с broken link на валидных Markdown-файлах, содержащих фрагменты кода с дженериками или типами

### Первопричина (Root Cause)
Сигнатуры кода вида `[T](err)` или `[Type](val)` внутри тройных кодовых блоков (code fences) синтаксически неотличимы от Markdown-ссылок `[text](url)` для линейного regex

### Решение / Выводы
Предварительно вырезать кодовые блоки через fence_re = re.compile(r'```.*?```', re.DOTALL); text = fence_re.sub('', text) перед regex-извлечением ссылок

### Обоснование / Почему именно так (Rationale)
Гарантирует проверку только реальных навигационных гиперссылок документации без ложных срабатываний на листинги кода

### Журнал сбоев и наблюдений (Log)
- [2026-11-17] PROMOTED: Подтверждено пользователем: ловушка с ложными ссылками в code-fences проверена на практике в verify_catalog.py
