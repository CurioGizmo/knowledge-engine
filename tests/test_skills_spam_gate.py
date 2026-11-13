"""Тесты для Anti-Spam Gate и сканера безопасности скиллов."""

import pytest

from mcp_skills.spam_gate import SpamGate, SpamGateResult


def test_spam_gate_valid_content() -> None:
    gate = SpamGate()
    clean_markdown = """
# New React Guidelines
Use useEffect with strict dependencies array.
Always declare TypeScript interfaces for props.
"""
    result = gate.validate_content("writing-plans", clean_markdown)
    assert result.is_valid is True
    assert len(result.issues) == 0


def test_spam_gate_catches_openai_api_key() -> None:
    gate = SpamGate()
    fake_openai = "sk-" + "proj-1234567890abcdef1234567890abcdef"
    bad_content = f"Here is my key: {fake_openai}"
    result = gate.validate_content("test-skill", bad_content)
    assert result.is_valid is False
    assert any("секрет" in issue.lower() or "api key" in issue.lower() for issue in result.issues)


def test_spam_gate_catches_github_token() -> None:
    gate = SpamGate()
    fake_ghp = "gh" + "p_1234567890abcdef1234567890abcdef"
    bad_content = f"export GITHUB_TOKEN={fake_ghp}"
    result = gate.validate_content("test-skill", bad_content)
    assert result.is_valid is False
    assert any("секрет" in issue.lower() or "token" in issue.lower() for issue in result.issues)


def test_spam_gate_catches_private_key() -> None:
    gate = SpamGate()
    bad_content = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----"
    )
    result = gate.validate_content("test-skill", bad_content)
    assert result.is_valid is False
    assert any("private key" in issue.lower() for issue in result.issues)


def test_spam_gate_catches_path_traversal() -> None:
    gate = SpamGate()
    assert gate.is_safe_path("../../../etc/passwd") is False
    assert gate.is_safe_path("skills/test/../../secret") is False
    assert gate.is_safe_path("blocks/ui/react.md") is True
    assert gate.is_safe_path("SKELETON.md") is True


def test_spam_gate_catches_extended_secrets_and_invalid_names() -> None:
    gate = SpamGate()
    # Google API Key (constructed dynamically for secret scanner)
    fake_google = "AI" + "zaSyD1234567890abcdef1234567890abcdef"
    res_google = gate.validate_content("skill", f"key: {fake_google}")
    assert res_google.is_valid is False

    # Slack token (constructed dynamically for secret scanner)
    fake_slack = "xox" + "b-1234567890-abcdef123456"
    res_slack = gate.validate_content("skill", f"token: {fake_slack}")
    assert res_slack.is_valid is False

    # Stripe key (constructed dynamically for secret scanner)
    fake_stripe = "sk_" + "live_1234567890abcdef1234567890"
    res_stripe = gate.validate_content("skill", f"stripe: {fake_stripe}")
    assert res_stripe.is_valid is False

    # Generic password assignment
    res_pass = gate.validate_content("skill", "password = 'super_secret_password_123'")
    assert res_pass.is_valid is False

    # Dummy canonical placeholders should be allowed
    res_placeholder1 = gate.validate_content("skill", "password: 'REPLACE_ME'")
    assert res_placeholder1.is_valid is True

    res_placeholder2 = gate.validate_content("skill", "api_key = 'your_api_key_here'")
    assert res_placeholder2.is_valid is True

    res_placeholder3 = gate.validate_content("skill", "secret_key: '<secret_token>'")
    assert res_placeholder3.is_valid is True

    # Dangerous pseudo-placeholders with actual keys must be blocked
    res_pseudo1 = gate.validate_content("skill", "secret_key = 'testing_prod_key_1234'")
    assert res_pseudo1.is_valid is False

    res_pseudo2 = gate.validate_content("skill", "api_key = 'your_real_production_key'")
    assert res_pseudo2.is_valid is False

    res_pseudo3 = gate.validate_content("skill", "api_key = 'dummy_but_real_key_9876'")
    assert res_pseudo3.is_valid is False

    # Path traversal in skill name
    res_name = gate.validate_content("../../evil-skill", "Clean content")
    assert res_name.is_valid is False
