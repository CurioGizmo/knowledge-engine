---
id: security-slotcompiler-resolve-slot-content
title: 'SlotCompiler._resolve_slot_content: path traversal через параметр touches
  (чтение произвольных *.md вне blocks_dir)'
category: security
type: lesson
status: active
task_id: skills-engine-final-review-2
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: antigravity-final-review
  created_at: '2026-10-01'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to:
- security-sigalrm-plan-linter-validate
derived_from: null
aliases:
- skills-slot-compiler-touches-traversal
tags:
- mcp_skills
- slot_compiler
- path_traversal
- arbitrary_file_read
- touches
confidence: 0.75
---

### Симптом / Контекст
MCP-инструмент load_skill с touches=['/абсолютный/путь'] или touches=['../../secrets'] читает и возвращает содержимое произвольных *.md файлов (default.md / <tech>.md) вне директории блоков скилла. Воспроизведено в沙箱: touches=[str(outside_dir)] → 'OUTSIDE_LEAKED' попал в собранный контент скилла; touches=['../../../secrets'] → 'TOP_SECRET_CONTENT_LEAKED'.

### Первопричина (Root Cause)
SlotCompiler._resolve_slot_content строит slot_dir = blocks_dir / slot без валидации slot: (1) абсолютный путь в `Path / abs` полностью заменяет базу (pathlib-семантика); (2) `..`-компоненты поднимаются выше blocks_dir. Ни is_safe_name, ни resolve().is_relative_to(blocks_dir) к slot не применяются, хотя для overlay_path и propose_update такие проверки в ветке есть.

### Решение / Выводы
В _inject_slots/_resolve_slot_content валидировать slot через is_safe_name() (или минимум отклонять значения с '/', '\\', '..', абсолютные пути), плюс containment-проверку: (blocks_dir / slot).resolve().is_relative_to(blocks_dir.resolve()). Добавить негативные тесты: touches=['../../secrets'], touches=['/abs/path'], touches=['..'].

### Обоснование / Почему именно так (Rationale)
touches приходит из MCP-инструмента load_skill (вызов от LLM-агента, полу-доверенный контекст: prompt injection в плане/скилле может заставить агента передать вредоносный touches). Контент утекает прямо в ответ инструмента и далее в контекст модели. Защита от traversal есть для overlay_path и propose_update, но слоты блоков остались без containment-проверки — классический пропуск одного из трёх мест.

### Журнал сбоев и наблюдений (Log)
- [2026-10-01] NOTE: Опечатка в поле symptom: 'в沙箱' читать как 'в песочнице' (sandbox, изолированный tmpdir). Воспроизведение: SlotCompiler.assemble_skill(touches=[str(вне blocks_dir)]) и touches=['../../../secrets'] → содержимое внешнего default.md попадает в собранный скилл; абсолютный путь опаснее относительного (Path / abs заменяет базу целиком).
