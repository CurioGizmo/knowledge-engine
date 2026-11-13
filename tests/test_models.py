"""Тесты для моделей данных OKF v0.2 и нормализации CardFrontmatter."""

from mcp_knowledge.models import (
    CardFrontmatter,
    GeneratedBy,
    GeneratedMetadata,
    KnowledgeCategory,
    KnowledgeStatus,
    KnowledgeType,
    VerificationRecord,
)


def test_okf_frontmatter_serialization():
    """Проверка сериализации CardFrontmatter со структурой OKF v0.2.

    Вход: CardFrontmatter с полями generated, verified, stale_after.
    Выход: dict со структурой OKF v0.2.
    Проверка: Ключи generated, verified, stale_after присутствуют и содержат переданные значения.
    """
    fm = CardFrontmatter(
        id="platform-vulkan-sidecar",
        title="Vulkan Sidecar",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.LESSON,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(
            by=GeneratedBy.PROCESS,
            agent="antigravity",
            created_at="2026-09-20",
        ),
        verified=[
            VerificationRecord(
                by="human",
                at="2026-09-20",
                note="Verified on Linux",
            )
        ],
        stale_after="2027-01-01",
    )

    data = fm.model_dump()
    assert data["generated"]["by"] == "process"
    assert data["generated"]["agent"] == "antigravity"
    assert data["generated"]["created_at"] == "2026-09-20"
    assert len(data["verified"]) == 1
    assert data["verified"][0]["by"] == "human"
    assert data["verified"][0]["note"] == "Verified on Linux"
    assert data["stale_after"] == "2027-01-01"


def test_legacy_frontmatter_normalization():
    """Проверка автоматической нормализации устаревших полей в OKF v0.2.

    Вход: Словарь с legacy-полями source: 'user_confirmed', expires_at: '2026-12-31', status: 'verified'.
    Выход: Экземпляр CardFrontmatter стандарта OKF v0.2.
    Проверка: generated.by == 'human', stale_after == '2026-12-31', verified содержит 1 запись.
    """
    legacy_dict = {
        "id": "ml-directml-fallback",
        "title": "DirectML Fallback",
        "category": "ml",
        "type": "decision",
        "status": "verified",
        "source": "user_confirmed",
        "created_at": "2026-09-20",
        "expires_at": "2026-12-31",
        "tags": ["directml", "fallback"],
    }

    fm = CardFrontmatter.model_validate(legacy_dict)
    assert fm.generated.by == GeneratedBy.HUMAN
    assert fm.generated.created_at == "2026-09-20"
    assert fm.stale_after == "2026-12-31"
    assert len(fm.verified) == 1
    assert fm.verified[0]["by"] == "human"
    assert fm.status == KnowledgeStatus.VERIFIED


def test_verification_record_mapping_methods():
    """Проверка работы методов mapping (get, keys, in, []) в VerificationRecord."""
    import pytest

    rec = VerificationRecord(by="human", at="2026-09-20", note="Verified on Linux")
    assert rec["by"] == "human"
    assert rec.get("by") == "human"
    assert rec.get("non_existent", "default") == "default"
    assert rec.get("model_dump") is None  # Не возвращает встроенные методы BaseModel
    assert "by" in rec
    assert "non_existent" not in rec
    assert "by" in rec.keys()
    assert "note" in rec.keys()

    with pytest.raises(KeyError):
        _ = rec["non_existent"]


def test_symptom_min_length_validation():
    """Проверка, что symptom требует минимум 10 символов при отсутствии context."""
    import pytest
    from pydantic import ValidationError
    from mcp_knowledge.models import CardRecordRequest

    with pytest.raises(ValidationError):
        CardRecordRequest(
            title="Тест короткого симптома",
            category=KnowledgeCategory.AUDIO,
            type=KnowledgeType.LESSON,
            symptom="Коротко",  # 7 символов < 10
            root_cause="Достаточно длинная первопричина длиной более 40 символов для теста",
            solution="Достаточно длинное решение проблемы длиной более 50 символов для валидации",
            rationale="Обоснование правильности решения более 30 символов",
        )


def test_context_fallback_and_normalization():
    """Проверка, что передача context позволяет опустить symptom и root_cause."""
    from mcp_knowledge.models import CardRecordRequest

    req = CardRecordRequest(
        title="Архитектурное решение с контекстом",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        context="Контекст архитектурного выбора для новой системы хранения данных в SQLite",
        solution="Использовать WAL-режим и изолированные соединения с таймаутом занятости",
        rationale="Обоснование правильности решения более 30 символов для прохождения Anti-Spam Gate",
    )
    assert req.context is not None
    assert "Контекст архитектурного выбора" in req.symptom
    assert "Контекст архитектурного выбора" in req.root_cause


def test_card_record_request_omitted_symptom_and_context_fails():
    """Проверка, что опущенные symptom и context вызывают ошибку валидации (Anti-Spam Gate)."""
    import pytest
    from pydantic import ValidationError
    from mcp_knowledge.models import CardRecordRequest

    with pytest.raises(ValidationError):
        CardRecordRequest(
            title="Без симптома и контекста",
            category=KnowledgeCategory.BACKEND,
            type=KnowledgeType.LESSON,
            solution="Достаточно длинное решение проблемы длиной более 50 символов для валидации",
            rationale="Обоснование правильности решения более 30 символов",
        )


def test_context_too_short_fails():
    """Проверка, что context короче 40 символов отклоняется Anti-Spam Gate."""
    import pytest
    from pydantic import ValidationError
    from mcp_knowledge.models import CardRecordRequest

    with pytest.raises(ValidationError, match="не менее 40 символов"):
        CardRecordRequest(
            title="Короткий контекст",
            category=KnowledgeCategory.BACKEND,
            type=KnowledgeType.DECISION,
            context="Короткий контекст меньше сорока",
            solution="Достаточно длинное решение проблемы длиной более 50 символов для валидации",
            rationale="Обоснование правильности решения более 30 символов",
        )


def test_context_with_short_explicit_symptom_fails():
    """Проверка, что при передаче context явный симптом все равно валидируется на длину >= 10."""
    import pytest
    from pydantic import ValidationError
    from mcp_knowledge.models import CardRecordRequest

    with pytest.raises(ValidationError, match="не менее 10 символов"):
        CardRecordRequest(
            title="Контекст и короткий симптом",
            category=KnowledgeCategory.BACKEND,
            type=KnowledgeType.DECISION,
            context="Достаточно длинный контекст архитектурного решения более сорока символов",
            symptom="Коротко",
            solution="Достаточно длинное решение проблемы длиной более 50 символов для валидации",
            rationale="Обоснование правильности решения более 30 символов",
        )


def test_context_with_short_explicit_root_cause_fails():
    """Проверка, что при передаче context явная root_cause все равно валидируется на длину >= 40."""
    import pytest
    from pydantic import ValidationError
    from mcp_knowledge.models import CardRecordRequest

    with pytest.raises(ValidationError, match="не менее 40 символов"):
        CardRecordRequest(
            title="Контекст и короткая причина",
            category=KnowledgeCategory.BACKEND,
            type=KnowledgeType.DECISION,
            context="Достаточно длинный контекст архитектурного решения более сорока символов",
            root_cause="Короткая причина",
            solution="Достаточно длинное решение проблемы длиной более 50 символов для валидации",
            rationale="Обоснование правильности решения более 30 символов",
        )
