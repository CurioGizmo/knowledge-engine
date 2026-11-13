"""Тесты парсера и сериализатора планов и ревью (PlanArtifact, ReviewArtifact)."""

from pathlib import Path
import pytest

from mcp_audit.models import (
    PlanArtifact,
    PlanStatus,
    PlanStep,
    ReviewArtifact,
    ReviewComment,
    ReviewVerdict,
)
from mcp_audit.parser import (
    mutate_plan_step_in_markdown,
    parse_plan_markdown,
    parse_review_markdown,
    serialize_plan_markdown,
    serialize_review_markdown,
)


def test_plan_serialize_and_parse_roundtrip():
    plan = PlanArtifact(
        id="plan-backend-PROJ-142",
        schema_version=1,
        task_id="PROJ-142",
        title="OAuth2 Integration",
        category="backend",
        status=PlanStatus.IN_PROGRESS,
        pr_url="https://github.com/org/repo/pull/85",
        target_files=["src/auth.py", "tests/test_auth.py"],
        related_debts=["aud-backend-1234"],
        steps=[
            PlanStep(
                index=1,
                title="Создать TokenProvider",
                completed=True,
                note="2026-10-11: Юнит-тесты пройдены",
            ),
            PlanStep(index=2, title="Обновить middleware", completed=False),
        ],
        content="Внедрить поддержку OAuth2 авторизации для внешних клиентов.",
    )

    md = serialize_plan_markdown(plan)
    assert "# План: PROJ-142 OAuth2 Integration" in md
    assert (
        "> 🔗 **Pull Request:** [#85 (https://github.com/org/repo/pull/85)](https://github.com/org/repo/pull/85)"
        in md
    )
    assert "- [x] 1. Создать TokenProvider" in md
    assert "> 📝 2026-10-11: Юнит-тесты пройдены" in md
    assert "- [ ] 2. Обновить middleware" in md

    parsed = parse_plan_markdown(md)
    assert parsed.id == plan.id
    assert parsed.task_id == plan.task_id
    assert parsed.category == plan.category
    assert parsed.status == PlanStatus.IN_PROGRESS
    assert parsed.pr_url == plan.pr_url
    assert parsed.target_files == plan.target_files
    assert len(parsed.steps) == 2
    assert parsed.steps[0].completed is True
    assert parsed.steps[0].note == "2026-10-11: Юнит-тесты пройдены"
    assert parsed.steps[1].completed is False


def test_mutate_plan_step_in_markdown():
    initial_md = """---
id: plan-backend-PROJ-142
task_id: PROJ-142
title: OAuth2 Integration
category: backend
status: in_progress
---

# План: PROJ-142 OAuth2 Integration

## 📋 Этапы и шаги
- [ ] 1. Создать TokenProvider
- [ ] 2. Обновить middleware

```python
# Это блок кода с чекбоксом в примере - не должен измениться!
# - [ ] фейковый чекбокс
```
"""
    # 1. Отмечаем шаг 1 как выполненный
    new_md, changed, new_status = mutate_plan_step_in_markdown(
        initial_md,
        step_index=1,
        status="done",
        note="Токены создаются успешно",
        today_date="2026-10-11",
    )
    assert "- [x] 1. Создать TokenProvider" in new_md
    assert "> 📝 2026-10-11: Токены создаются успешно" in new_md
    assert "- [ ] 2. Обновить middleware" in new_md
    # фейковый чекбокс в коде остался неизменным
    assert "# - [ ] фейковый чекбокс" in new_md
    assert changed is False  # еще не все шаги закрыты

    # 2. Отмечаем шаг 2 -> теперь все закрыты, статус должен стать DONE
    final_md, changed2, new_status2 = mutate_plan_step_in_markdown(
        new_md,
        step_index=2,
        status="done",
        note="Middleware протестирован",
        today_date="2026-10-11",
    )
    assert "- [x] 2. Обновить middleware" in final_md
    assert changed2 is True
    assert new_status2 == PlanStatus.DONE
    assert "status: done" in final_md

    # 3. Откат шага 1 -> статус возвращается в IN_PROGRESS
    rollback_md, changed3, new_status3 = mutate_plan_step_in_markdown(
        final_md,
        step_index=1,
        status="open",
        today_date="2026-10-11",
    )
    assert "- [ ] 1. Создать TokenProvider" in rollback_md
    assert changed3 is True
    assert new_status3 == PlanStatus.IN_PROGRESS
    assert "status: in_progress" in rollback_md


def test_mutate_plan_step_errors():
    # Ошибка если шаг не существует
    simple_md = """---
id: plan-backend-PROJ-142
status: in_progress
---
## 📋 Этапы и шаги
- [ ] 1. Первый шаг
"""
    with pytest.raises(ValueError, match="STEP_NOT_FOUND"):
        mutate_plan_step_in_markdown(simple_md, step_index=5)

    # Ошибка если frontmatter поврежден
    broken_md = """---
id: [broken yaml: {
---
# Текст
"""
    with pytest.raises(ValueError, match="PLAN_FILE_CORRUPTED"):
        mutate_plan_step_in_markdown(broken_md, step_index=1)


def test_review_serialize_and_parse_roundtrip():
    rev = ReviewArtifact(
        id="rev-backend-PROJ-142-pr85-r1",
        task_id="PROJ-142",
        category="backend",
        round=1,
        verdict=ReviewVerdict.CHANGES_REQUESTED,
        summary="Необходимо обработать истечение времени жизни токена.",
        comments=[
            ReviewComment(
                file_path="src/auth.py", line=45, symptom="Пустой sub не проверяется", status="open"
            ),
            ReviewComment(
                file_path="src/jwt.py", line=12, symptom="Легаси декоратор", status="deferred"
            ),
        ],
        pr_url="https://github.com/org/repo/pull/85",
        reviewer="code-reviewer",
        spawned_debts=["aud-backend-5555"],
        created_at="2026-10-11",
    )

    md = serialize_review_markdown(rev)
    assert "# Code Review: PROJ-142 (Раунд 1)" in md
    assert "> **Вердикт:** 🔴 Changes Requested" in md
    assert "- **[OPEN]** `src/auth.py:45`: Пустой sub не проверяется" in md
    assert "- **[DEFERRED]** `src/jwt.py:12`: Легаси декоратор" in md

    parsed = parse_review_markdown(md)
    assert parsed.id == rev.id
    assert parsed.task_id == "PROJ-142"
    assert parsed.round == 1
    assert parsed.verdict == ReviewVerdict.CHANGES_REQUESTED
    assert len(parsed.comments) == 2
    assert parsed.comments[0].file_path == "src/auth.py"
    assert parsed.comments[0].line == 45
    assert parsed.comments[0].status == "open"
    assert parsed.spawned_debts == ["aud-backend-5555"]


def test_mutate_plan_step_rejects_invalid_status() -> None:
    """Проверяет, что mutate_plan_step_in_markdown отклоняет некорректный статус."""
    plan_md = """---
id: plan-backend-PROJ-1
task_id: PROJ-1
title: Sample
category: backend
status: discussed
---
# План
## 📋 Этапы
- [ ] 1. Шаг 1
"""
    with pytest.raises(ValueError, match="Недопустимый статус шага"):
        mutate_plan_step_in_markdown(plan_md, step_index=1, status="SOMETHING_WEIRD")


def test_update_plan_preserves_custom_sections(tmp_path: Path) -> None:
    """Проверяет, что update_plan_details не затирает пользовательские H2 секции."""
    from mcp_audit.storage import AuditStorage
    from mcp_audit.synchronizer import AuditSynchronizer

    db_path = tmp_path / "test_audit.db"
    storage = AuditStorage(db_path=db_path)
    storage._init_db()
    sync = AuditSynchronizer(storage=storage, audit_dir=tmp_path / ".agents" / "audit")

    plan_file = tmp_path / ".agents" / "plans" / "active" / "backend" / "plan-backend-P1.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        """---
id: plan-backend-P1
task_id: P1
title: Original Title
category: backend
status: discussed
---
# План: P1 Original Title
> **Статус:** ⚪ Обсуждено (`discussed`)
> 📌 **Задача:** P1

## 🎯 Цель и критерии приёмки
Важная цель

## 📋 Этапы и шаги
- [ ] 1. Шаг один

## ⚠️ Архитектурные риски и инварианты
Кастомная секция, которую нельзя удалять!
- Инвариант 1: Zero-alloc
""",
        encoding="utf-8",
    )

    sync.update_plan_details(task_id="P1", title="New Title", status=PlanStatus.APPROVED)

    updated_text = plan_file.read_text(encoding="utf-8")
    assert "## ⚠️ Архитектурные риски и инварианты" in updated_text
    assert "Кастомная секция, которую нельзя удалять!" in updated_text
    assert "title: New Title" in updated_text
    assert "status: approved" in updated_text


def test_details_with_h2_does_not_hijack_steps() -> None:
    """Проверяет, что H2 в details не перехватывает шаги и не сбивает step_plan."""
    raw_md = """---
id: plan-backend-P2
task_id: P2
title: Sample
category: backend
status: discussed
---
# План: P2 Sample

## 🎯 Цель и критерии приёмки
Контекст задачи с вложенными заголовками:
### Этапы в прошлом
- [ ] Старый чекбокс из истории

## 📋 Этапы и шаги
- [ ] 1. Настоящий шаг один
- [ ] 2. Настоящий шаг два

## 💡 Следующие шаги
- [ ] Пользовательская заметка на будущее
"""
    plan = parse_plan_markdown(raw_md)
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "Настоящий шаг один"
    assert plan.steps[1].title == "Настоящий шаг два"

    # Мутация шага 1
    mutated_md, changed, new_status = mutate_plan_step_in_markdown(
        raw_md, step_index=1, status="done"
    )
    assert "- [x] 1. Настоящий шаг один" in mutated_md
    assert "- [ ] 2. Настоящий шаг два" in mutated_md
    assert "- [ ] Пользовательская заметка на будущее" in mutated_md
    assert changed is True
    assert new_status == PlanStatus.IN_PROGRESS


def test_fallback_steps_section_when_first_matching_empty() -> None:
    """Проверяет fallback при отсутствии канонического заголовка:
    первая matching H2 пуста или без чекбоксов, вторая matching H2 содержит чекбоксы.
    READ и WRITE должны согласованно выбирать вторую секцию.
    """
    raw_md = """---
id: plan-backend-P3
task_id: P3
title: Fallback Test
category: backend
status: discussed
---
# План: P3 Fallback Test

## Этапы подготовки
Только вводный текст без единого чекбокса.

## Этапы реализации
- [ ] 1. Шаг реальной работы
- [ ] 2. Шаг тестирования
"""
    plan = parse_plan_markdown(raw_md)
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "Шаг реальной работы"
    assert plan.steps[1].title == "Шаг тестирования"

    # Мутация первого шага через mutate_plan_step_in_markdown (WRITE path)
    mutated_md, changed, new_status = mutate_plan_step_in_markdown(
        raw_md, step_index=1, status="done"
    )
    assert "- [x] 1. Шаг реальной работы" in mutated_md
    assert "- [ ] 2. Шаг тестирования" in mutated_md
    assert changed is True
    assert new_status == PlanStatus.IN_PROGRESS


def test_fenced_h2_and_checkboxes_do_not_truncate_steps_section() -> None:
    """Проверяет, что '## ' и чекбоксы внутри кодового блока (code fence)
    не обрезают секцию шагов и не учитываются как шаги плана.
    """
    raw_md = """---
id: plan-backend-P4
task_id: P4
title: Fenced H2 Test
category: backend
status: discussed
---
# План: P4 Fenced H2 Test

## 📋 Этапы и шаги
- [ ] 1. Первый реальный шаг

```markdown
## Пример подзаголовка документации
- [ ] Пример чекбокса в документации
```

```bash
## Комментарий скрипта
echo "hello"
```

- [ ] 2. Второй реальный шаг

## ⚠️ Риски
- Риск 1
"""
    plan = parse_plan_markdown(raw_md)
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "Первый реальный шаг"
    assert plan.steps[1].title == "Второй реальный шаг"

    # Мутируем шаг 1
    m1, ch1, st1 = mutate_plan_step_in_markdown(raw_md, step_index=1, status="done")
    assert "- [x] 1. Первый реальный шаг" in m1
    assert "- [ ] 2. Второй реальный шаг" in m1
    assert ch1 is True
    assert st1 == PlanStatus.IN_PROGRESS

    # Мутируем шаг 2 -> должен перейти в done, а не застрять или закрыться на шаге 1
    m2, ch2, st2 = mutate_plan_step_in_markdown(m1, step_index=2, status="done")
    assert "- [x] 1. Первый реальный шаг" in m2
    assert "- [x] 2. Второй реальный шаг" in m2
    assert ch2 is True
    assert st2 == PlanStatus.DONE


def test_indented_h2_in_details_and_multiline_title_sanitization(tmp_path: Path) -> None:
    """Проверяет экранирование отступных H2 (0-3 пробела, таб) и многострочного title."""
    from mcp_audit.server import record_plan
    from mcp_audit.storage import AuditStorage
    from mcp_audit.synchronizer import AuditSynchronizer

    # Создаём план с отступным H2 в details и многострочным title в песочнице tmp_path
    storage = AuditStorage(db_path=tmp_path / "test.db")
    audit_dir = tmp_path / "audit"
    plans_dir = tmp_path / "plans"
    sync = AuditSynchronizer(storage=storage, audit_dir=audit_dir, plans_dir=plans_dir)

    res = record_plan(
        task_id="P5",
        title="Многострочный\n  ## Заголовок",
        steps=["Шаг 1", "Шаг 2"],
        category="backend",
        details="Описание\n  ## Этапы и шаги\n- [ ] Ложный шаг",
        synchronizer=sync,
    )
    assert res["status"] == "created"
    plan_path = Path(res["file_path"])
    raw_md = plan_path.read_text(encoding="utf-8")

    plan = parse_plan_markdown(raw_md)
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "Шаг 1"
    assert plan.steps[1].title == "Шаг 2"
    assert "\n" not in plan.title


def test_frontmatter_block_scalar_with_h2_does_not_break_steps() -> None:
    """Проверяет, что строки во frontmatter (например block scalar notes: |)
    не перехватывают секцию шагов в body.
    """
    raw_md = """---
id: plan-backend-P6
task_id: P6
title: Notes Test
category: backend
status: discussed
notes: |
  ## 📋 Этапы и шаги
  - [ ] Фейковый шаг из yaml
---
# План: P6 Notes Test

## 📋 Этапы и шаги
- [ ] 1. Истинный шаг 1
- [ ] 2. Истинный шаг 2
"""
    plan = parse_plan_markdown(raw_md)
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "Истинный шаг 1"

    mutated_md, changed, new_status = mutate_plan_step_in_markdown(
        raw_md, step_index=1, status="done"
    )
    assert "- [x] 1. Истинный шаг 1" in mutated_md
    assert changed is True
    assert new_status == PlanStatus.IN_PROGRESS


def test_mixed_fence_markers_commonmark_compliance() -> None:
    """Проверяет, что ~~~ внутри ``` блока не закрывает блок кода."""
    raw_md = """---
id: plan-backend-P7
task_id: P7
title: Mixed Fence
category: backend
status: discussed
---
# План: P7 Mixed Fence

## 📋 Этапы и шаги
- [ ] 1. Шаг A

```python
~~~ inner tilde is not closing fence ~~~
## Ложный заголовок
- [ ] EXAMPLE
```

- [ ] 2. Шаг B
"""
    plan = parse_plan_markdown(raw_md)
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "Шаг A"
    assert plan.steps[1].title == "Шаг B"

    # Шаг 1 закрывается -> progress 1/2, не DONE
    m1, ch1, st1 = mutate_plan_step_in_markdown(raw_md, step_index=1, status="done")
    assert "- [x] 1. Шаг A" in m1
    assert "- [ ] 2. Шаг B" in m1
    assert ch1 is True
    assert st1 == PlanStatus.IN_PROGRESS


def test_thematic_break_in_body_does_not_blind_read_steps() -> None:
    """Проверяет случай M2: body начинается с thematic break '---' и имеет разделитель '---' ПОСЛЕ секции шагов.
    READ и WRITE должны согласованно видеть шаги плана.
    """
    raw_md = """---
id: plan-backend-P8
task_id: P8
title: Thematic Break
category: backend
status: discussed
---
---
# План: P8 Thematic Break

## 📋 Этапы и шаги
- [ ] 1. Шаг A
- [ ] 2. Шаг B

---
## Дополнительно
Текст после черты
"""
    plan = parse_plan_markdown(raw_md)
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "Шаг A"
    assert plan.steps[1].title == "Шаг B"

    mutated_md, changed, new_status = mutate_plan_step_in_markdown(
        raw_md, step_index=1, status="done"
    )
    assert "- [x] 1. Шаг A" in mutated_md
    assert changed is True
    assert new_status == PlanStatus.IN_PROGRESS
