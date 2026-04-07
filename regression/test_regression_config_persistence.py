"""Black-box regression coverage for runtime model config persistence."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.regression


def test_runtime_model_config_update_persists_across_restart(
    regression_harness_factory,
):
    harness = regression_harness_factory("tiny_python_app")

    before = harness.get_config()
    assert before["planner_model"] == "github-copilot/gpt-5.4", before
    assert before["explorer_model"] == "github-copilot/gpt-5.4", before
    assert before["map_model"] == "github-copilot/gpt-5.4", before

    update_result = harness.update_config(
        {
            "planner_model": "github-copilot/gpt-5.4",
            "coder_model_default": "github-copilot/gpt-5.4",
            "coder_model_by_complexity": {
                "simple": "github-copilot/gpt-5.4",
                "medium": "github-copilot/gpt-5.4",
                "complex": "github-copilot/gpt-5.4",
                "very_complex": "github-copilot/gpt-5.4",
            },
            "reviewer_models": ["github-copilot/gpt-5.4"],
            "explorer_model": "github-copilot/gpt-5.4",
            "map_model": "github-copilot/gpt-5.4",
        }
    )
    assert update_result["ok"] is True, update_result

    after_update = harness.get_config()
    assert after_update["planner_model"] == "github-copilot/gpt-5.4", after_update
    assert after_update["coder_model_default"] == "github-copilot/gpt-5.4", after_update
    assert after_update["reviewer_models"] == ["github-copilot/gpt-5.4"], after_update
    assert after_update["explorer_model"] == "github-copilot/gpt-5.4", after_update
    assert after_update["map_model"] == "github-copilot/gpt-5.4", after_update

    harness.restart_preserving_runtime_config()

    after_restart = harness.get_config()
    assert after_restart["planner_model"] == "github-copilot/gpt-5.4", after_restart
    assert after_restart["coder_model_default"] == "github-copilot/gpt-5.4", (
        after_restart
    )
    assert after_restart["reviewer_models"] == ["github-copilot/gpt-5.4"], after_restart
    assert after_restart["explorer_model"] == "github-copilot/gpt-5.4", after_restart
    assert after_restart["map_model"] == "github-copilot/gpt-5.4", after_restart
