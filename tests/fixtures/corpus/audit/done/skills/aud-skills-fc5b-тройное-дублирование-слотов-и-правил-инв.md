---
id: aud-skills-fc5b
title: Тройное дублирование слотов и правил инвариантов внутри code-review
category: skills
priority: important
autonomy: high
complexity: M
status: completed
target_file: skills/quality-review/code-review/SKILL.md
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-fc5b: Тройное дублирование слотов и правил инвариантов внутри code-review

## ❌ Симптом
Троекратное раздувание собранного промпта code-review одними и теми же правилами и неполное покрытие стеков для 4 платформ.

## 🔍 Техническая первопричина
skills/quality-review/code-review/SKILL.md:64, 68-69 объявляет 3 параллельных SLOT+ (stack-invariants, subsystem-audit, invariants), что при сборке со стеком python трижды дублирует правила про CancelledError, блокирующий I/O, BackgroundTask и лимиты. При этом локальные блоки покрывают только 5 технологий из 9.

## 🛠 Предлагаемое решение
Устранить избыточный слот invariants. Оставить общий stack-invariants и специализированный subsystem-audit. Дописать недостающие блоки subsystem-audit для go, rust, postgres, fastapi.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Удален избыточный слот invariants в code-review/SKILL.md; созданы блоки subsystem-audit для go, rust, postgres, fastapi; в live-qa-verification подключен evidence-integrity; удалены локальные заглушки default.md в plan-review и writing-plans; в python-design-patterns слот переведен на single-slot SLOT.
