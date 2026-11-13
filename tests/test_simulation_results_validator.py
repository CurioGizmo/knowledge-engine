from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _validator() -> ModuleType:
    path = Path(__file__).parents[1] / "docs" / "testing" / "tools" / "validate_results.py"
    if not path.is_file():
        pytest.skip("simulation results validator tool not present")
    spec = importlib.util.spec_from_file_location("simulation_results_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_results() -> dict:
    return {
        "results_schema_version": "simulation-results-v1",
        "passport": {
            "pilot": "audit-engine",
            "owner_id": "audit",
            "series_id": "audit-20261028T060000Z-abcd1234",
            "created_utc": "2026-10-28T06:00:00Z",
            "evaluation_scope": "mcp_integration",
            "evaluation_policy": "integration-v1",
            "llm_quality": "NOT_EVALUATED",
        },
        "engine_checks": {},
        "agent_process_checks": {},
        "engine_smoke": {},
        "live_diagnostic": {},
        "product_baseline": {"integration_verdict": "PASS"},
        "attempts": [
            {
                "attempt_id": "a01-r1",
                "scenario_id": "A01",
                "depends_on": [],
                "accounting": "baseline",
                "session_id": "ses_1",
                "integration_verdict": "PASS",
                "integration_checks": {"required_call": {"status": "PASS"}},
                "execution_status": "COMPLETED",
                "environment_validity": "VALID",
                "llm_quality": "NOT_EVALUATED",
            },
            {
                "attempt_id": "a08-r1",
                "scenario_id": "A08",
                "depends_on": ["a01-r1"],
                "accounting": "baseline",
                "session_id": "ses_2",
                "integration_verdict": "PASS",
                "integration_checks": {"review_link": "PASS - linked"},
                "execution_status": "COMPLETED",
                "environment_validity": "VALID",
                "llm_quality": "NOT_EVALUATED",
            },
        ],
        "blockers": [],
        "agent_errors": [],
        "uncovered_branches": [],
        "measurements": {"availability": "unknown", "reason": "test fixture"},
        "report_path": str(Path.cwd() / "example-report.md"),
    }


def test_valid_summary_passes() -> None:
    assert _validator().validate_results(_valid_results()) == []


def test_pass_cannot_hide_failed_required_check() -> None:
    data = _valid_results()
    data["attempts"][0]["integration_checks"]["required_call"]["status"] = "FAIL"
    errors = _validator().validate_results(data)
    assert any("PASS contradicts required checks" in error for error in errors)
    assert any("product_baseline" in error for error in errors)


def test_dependent_pass_requires_strict_pass() -> None:
    data = _valid_results()
    data["attempts"][0]["integration_verdict"] = "FAIL"
    data["product_baseline"]["integration_verdict"] = "FAIL"
    errors = _validator().validate_results(data)
    assert any("depends on non-strict-PASS" in error for error in errors)


def test_a08_must_reference_a01() -> None:
    data = _valid_results()
    data["attempts"][1]["depends_on"] = []
    errors = _validator().validate_results(data)
    assert any("A08 must declare" in error for error in errors)


def test_required_sections_must_not_be_null() -> None:
    data = _valid_results()
    data["measurements"] = None
    errors = _validator().validate_results(data)
    assert any("measurements: must not be null" in error for error in errors)
