"""Тесты для GitSkillsManager и управления оверрайдами агентов."""

from pathlib import Path

from mcp_skills.git_manager import GitSkillsManager


def test_git_manager_save_override(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    manager = GitSkillsManager(cache_dir=cache_dir)
    content = "base_agent: code-reviewer\nmodel: flash\n"
    res = manager.save_agent_override(
        name="code-reviewer",
        project="demo",
        target="antigravity",
        content=content,
    )
    assert res.success is True
    assert res.path is not None
    saved_file = Path(res.path)
    assert saved_file.is_file()
    assert "model: flash" in saved_file.read_text(encoding="utf-8")


def test_git_manager_uses_injected_spam_gate(tmp_path: Path) -> None:
    """Позитивный тест DI: переданный spam_gate используется вместо дефолтного."""
    from mcp_skills.spam_gate import SpamGate, SpamGateResult

    class _SelectiveSpamGate(SpamGate):
        """Кастомная политика: отклоняет только контент с 'sk-'."""

        def validate_content(self, skill_name: str, content: str) -> SpamGateResult:
            if "sk-" in content:
                return SpamGateResult(
                    is_valid=False,
                    issues=["Обнаружен потенциальный секрет (custom gate)"],
                )
            return SpamGateResult(is_valid=True)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    gate = _SelectiveSpamGate()
    manager = GitSkillsManager(cache_dir=cache_dir, spam_gate=gate)

    # 1. Разрешённый контент проходит через инжектированный гейт
    valid_content = "base_agent: code-reviewer\nmodel: flash\n"
    res_ok = manager.save_agent_override(
        name="code-reviewer",
        project="demo",
        target="antigravity",
        content=valid_content,
    )
    assert res_ok.success is True

    # 2. Секрет отклоняется инжектированным гейтом с маркерным сообщением
    secret_content = "base_agent: code-reviewer\ninstructions_suffix: key sk-123456\n"  # noqa: S105
    res_secret = manager.save_agent_override(
        name="code-reviewer",
        project="demo",
        target="antigravity",
        content=secret_content,
    )
    assert res_secret.success is False
    assert any("custom gate" in issue for issue in res_secret.issues)
