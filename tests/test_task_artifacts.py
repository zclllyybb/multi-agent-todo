from core.task_artifacts import _skill_markdown, _skill_script


def test_skill_markdown_mentions_fork_and_final_text_block():
    content = _skill_markdown()
    assert "--fork" in content
    assert "final text block" in content
    assert "opencode-session-ask" in content


def test_skill_script_uses_fork_and_extracts_stop_step_text():
    script = _skill_script()
    assert "--fork" in script
    assert 'if step.get("reason") != "stop"' in script
    assert "No final text block found" in script
