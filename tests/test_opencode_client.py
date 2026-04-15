"""Tests for core/opencode_client.py: parsing and subprocess env handling."""

import os
import subprocess
from unittest.mock import patch

import pytest

from core.opencode_client import OpenCodeClient


@pytest.fixture
def client():
    return OpenCodeClient(timeout=10)


class TestParseJsonOutput:
    def test_valid_ndjson(self, client):
        output = '{"type":"text","part":{"text":"hello"}}\n{"type":"step_start"}\n'
        events = client.parse_json_output(output)
        assert len(events) == 2
        assert events[0]["type"] == "text"

    def test_skips_invalid_lines(self, client):
        output = '{"valid":true}\nthis is not json\n{"also":"valid"}\n'
        events = client.parse_json_output(output)
        assert len(events) == 2

    def test_empty_input(self, client):
        assert client.parse_json_output("") == []
        assert client.parse_json_output("  \n  \n") == []

    def test_single_event(self, client):
        output = '{"sessionID":"ses_abc123"}'
        events = client.parse_json_output(output)
        assert len(events) == 1
        assert events[0]["sessionID"] == "ses_abc123"


class TestExtractSessionId:
    def test_finds_session_id(self, client):
        output = '{"sessionID":"ses_xyz","type":"start"}\n{"type":"text","part":{"text":"hi"}}\n'
        assert client.extract_session_id(output) == "ses_xyz"

    def test_no_session_id(self, client):
        output = '{"type":"text","part":{"text":"hello"}}\n'
        assert client.extract_session_id(output) == ""

    def test_empty_output(self, client):
        assert client.extract_session_id("") == ""

    def test_first_event_with_session(self, client):
        output = '{"type":"other"}\n{"sessionID":"ses_second","type":"start"}\n'
        assert client.extract_session_id(output) == "ses_second"


class TestExtractTextResponse:
    def test_extracts_text_parts(self, client):
        output = (
            '{"type":"step_start"}\n'
            '{"type":"text","part":{"text":"Hello "}}\n'
            '{"type":"text","part":{"text":"world"}}\n'
            '{"type":"step_finish","part":{"reason":"done"}}\n'
        )
        assert client.extract_text_response(output) == "Hello world"

    def test_no_text_events_returns_raw(self, client):
        output = '{"type":"step_start"}\n{"type":"step_finish"}\n'
        assert client.extract_text_response(output) == output

    def test_empty_output(self, client):
        assert client.extract_text_response("") == ""

    def test_non_dict_events_skipped(self, client):
        output = '"just a string"\n{"type":"text","part":{"text":"ok"}}\n'
        assert client.extract_text_response(output) == "ok"


class TestFormatReadableText:
    def test_formats_multi_step(self, client):
        output = (
            '{"sessionID":"ses_1","type":"init"}\n'
            '{"type":"step_start"}\n'
            '{"type":"text","part":{"text":"Analyzing..."}}\n'
            '{"type":"step_finish","part":{"reason":"end_turn"}}\n'
        )
        text = client.format_readable_text(output)
        assert "Session: ses_1" in text
        assert "Step 1" in text
        assert "Analyzing..." in text
        assert "end_turn" in text

    def test_empty_output(self, client):
        text = client.format_readable_text("")
        assert isinstance(text, str)

    def test_summary_line(self, client):
        output = (
            '{"type":"step_start"}\n'
            '{"type":"text","part":{"text":"hello"}}\n'
            '{"type":"tool_use","part":{"tool":"read_file","state":{"input":{"path":"a.py"},"output":"ok","status":"done"}}}\n'
            '{"type":"step_finish","part":{"reason":"done"}}\n'
        )
        text = client.format_readable_text(output)
        assert "1 steps" in text
        assert "1 text segments" in text
        assert "1 tool calls" in text


class TestParseReadableOutput:
    def test_multi_step_structure(self, client):
        output = (
            '{"sessionID":"ses_1","type":"init"}\n'
            '{"type":"step_start"}\n'
            '{"type":"text","part":{"text":"Analyzing code..."}}\n'
            '{"type":"tool_use","part":{"tool":"read_file","state":{"input":{"path":"a.py"},"output":"content","status":"done"}}}\n'
            '{"type":"step_finish","part":{"reason":"end_turn"}}\n'
            '{"type":"step_start"}\n'
            '{"type":"text","part":{"text":"Making changes..."}}\n'
            '{"type":"step_finish","part":{"reason":"end_turn"}}\n'
        )
        parsed = client.parse_readable_output(output)
        assert parsed["session_id"] == "ses_1"
        assert len(parsed["steps"]) == 2
        assert parsed["summary"]["total_steps"] == 2
        assert parsed["summary"]["text_segments"] == 2
        assert parsed["summary"]["tool_calls"] == 1

        # Step 1 has a text + tool event
        step1_events = parsed["steps"][0]["events"]
        assert step1_events[0]["type"] == "text"
        assert step1_events[0]["content"] == "Analyzing code..."
        assert step1_events[1]["type"] == "tool"
        assert step1_events[1]["tool"] == "read_file"

    def test_empty_output_returns_fallback(self, client):
        parsed = client.parse_readable_output("")
        assert parsed["session_id"] == ""
        assert parsed["steps"] == []

    def test_non_dict_part_handled(self, client):
        output = '{"type":"step_start"}\n{"type":"tool_use","part":"not_a_dict"}\n'
        parsed = client.parse_readable_output(output)
        assert len(parsed["steps"]) == 1


# ── Helper to build NDJSON events quickly ──────────────────────────────


def _step(texts=(), tools=(), finish_reason=None):
    """Return a list of NDJSON lines representing one step."""
    import json

    lines = ['{"type":"step_start"}']
    for t in texts:
        lines.append(json.dumps({"type": "text", "part": {"text": t}}))
    for t in tools:
        lines.append(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": t,
                        "state": {"input": {}, "output": "", "status": "completed"},
                    },
                }
            )
        )
    if finish_reason:
        lines.append(
            json.dumps({"type": "step_finish", "part": {"reason": finish_reason}})
        )
    return lines


def _build_output(*step_specs):
    """Build NDJSON output from step specs: [(texts, tools, finish_reason), ...]"""
    lines = []
    for texts, tools, reason in step_specs:
        lines.extend(_step(texts, tools, reason))
    return "\n".join(lines) + "\n"


class TestIsOutputComplete:
    def test_proper_stop(self, client):
        output = _build_output(
            (["hello"], ["read_file"], "tool-calls"),
            (["done"], [], "stop"),
        )
        assert client.is_output_complete(output) is True

    def test_missing_stop(self, client):
        output = _build_output(
            (["hello"], ["read_file"], "tool-calls"),
            ([], [], None),  # empty step, no finish_reason
        )
        assert client.is_output_complete(output) is False

    def test_last_step_tool_calls_not_stop(self, client):
        output = _build_output(
            (["hello"], ["read_file"], "tool-calls"),
        )
        assert client.is_output_complete(output) is False

    def test_empty_output(self, client):
        assert client.is_output_complete("") is False

    def test_single_step_stop(self, client):
        output = _build_output(
            (["All done."], [], "stop"),
        )
        assert client.is_output_complete(output) is True


class TestExtractLastTextBlock:
    def test_returns_last_stop_step_text(self, client):
        output = _build_output(
            (["Starting analysis..."], ["read_file"], "tool-calls"),
            (["Mid work"], ["write_file"], "tool-calls"),
            (["Final summary here"], [], "stop"),
        )
        assert client.extract_last_text_block(output) == "Final summary here"


class TestVariantCli:
    def test_run_passes_variant_flag(self, client):
        with patch.object(client, "_exec", return_value=("", 0, 0.1)) as exec_mock:
            client.run(
                message="explore",
                work_dir="/repo",
                model="test-model",
                agent_type="explorer",
                variant="deep-explorer",
            )

        cmd = exec_mock.call_args.args[0]
        assert "--variant" in cmd
        idx = cmd.index("--variant")
        assert cmd[idx + 1] == "deep-explorer"

    def test_run_omits_variant_flag_when_variant_empty(self, client):
        with patch.object(client, "_exec", return_value=("", 0, 0.1)) as exec_mock:
            client.run(
                message="explore",
                work_dir="/repo",
                model="test-model",
                agent_type="explorer",
                variant="",
            )

        cmd = exec_mock.call_args.args[0]
        assert "--variant" not in cmd

    def test_run_streaming_passes_variant_flag(self, client):
        with patch.object(
            client,
            "_exec_streaming",
            return_value=("", 0, 0.1, "", False),
        ) as exec_mock:
            client.run_streaming(
                message="explore",
                work_dir="/repo",
                model="test-model",
                agent_type="explorer",
                variant="deep-explorer",
            )

        cmd = exec_mock.call_args.kwargs["cmd"]
        assert "--variant" in cmd
        idx = cmd.index("--variant")
        assert cmd[idx + 1] == "deep-explorer"

    def test_run_streaming_omits_variant_flag_when_variant_empty(self, client):
        with patch.object(
            client,
            "_exec_streaming",
            return_value=("", 0, 0.1, "", False),
        ) as exec_mock:
            client.run_streaming(
                message="explore",
                work_dir="/repo",
                model="test-model",
                agent_type="explorer",
                variant="",
            )

        cmd = exec_mock.call_args.kwargs["cmd"]
        assert "--variant" not in cmd

    def test_run_streaming_continues_when_require_stop_and_output_incomplete(
        self, client
    ):
        first_output = _build_output((["partial"], [], "tool-calls"))
        second_output = _build_output((["final"], [], "stop"))

        with patch.object(
            client,
            "_exec_streaming",
            side_effect=[
                (first_output, 0, 0.1, "ses_incomplete", False),
                (second_output, 0, 0.2, "ses_incomplete", False),
            ],
        ) as exec_mock:
            run = client.run_streaming(
                message="explore",
                work_dir="/repo",
                model="test-model",
                agent_type="explorer",
                require_stop=True,
                max_continues=2,
            )

        assert exec_mock.call_count == 2
        second_cmd = exec_mock.call_args_list[1].kwargs["cmd"]
        assert second_cmd[-1] == "Continue"
        assert run.exit_code == 0
        assert client.is_output_complete(run.output) is True

    def test_multiple_text_segments_in_last_step(self, client):
        output = _build_output(
            (["intro"], [], "tool-calls"),
            (["Part A. ", "Part B."], [], "stop"),
        )
        assert client.extract_last_text_block(output) == "Part A. Part B."

    def test_no_text_in_last_stop_step(self, client):
        output = _build_output(
            (["some text"], [], "tool-calls"),
            ([], ["write_file"], "stop"),
        )
        assert client.extract_last_text_block(output) == ""

    def test_no_stop_at_all(self, client):
        output = _build_output(
            (["text"], ["tool"], "tool-calls"),
            ([], [], None),
        )
        assert client.extract_last_text_block(output) == ""

    def test_empty_output(self, client):
        assert client.extract_last_text_block("") == ""

    def test_skips_earlier_stop_returns_last(self, client):
        """If there are multiple stops (e.g. continue), return the last one."""
        output = _build_output(
            (["first stop text"], [], "stop"),
            (["resumed"], ["tool"], "tool-calls"),
            (["final answer"], [], "stop"),
        )
        assert client.extract_last_text_block(output) == "final answer"


class TestExtractLastTextBlockOrRaw:
    def test_returns_last_stop_text_when_available(self, client):
        output = _build_output((["final answer"], [], "stop"))
        assert client.extract_last_text_block_or_raw(output) == "final answer"

    def test_falls_back_to_raw_text_response(self, client):
        output = '{"type":"text","part":{"text":"plain text"}}\n'
        assert client.extract_last_text_block_or_raw(output) == "plain text"


class TestExecTimeout:
    def test_exec_timeout_keeps_partial_output_for_session_id(
        self, client, tmp_path, monkeypatch
    ):
        class _Tail:
            def __init__(self, text):
                self._text = text

            def read(self):
                return self._text

        class _FakeProc:
            def __init__(self):
                self.stdout = _Tail('{"type":"text","part":{"text":"tail"}}\n')
                self.stderr = _Tail("")
                self.returncode = None

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(
                    cmd=["opencode"],
                    timeout=timeout,
                    output='{"sessionID":"ses_timeout_1"}\n',
                    stderr="",
                )

            def kill(self):
                return None

            def wait(self):
                self.returncode = -1
                return -1

        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FakeProc())

        output, exit_code, _ = client._exec(["opencode", "run"], str(tmp_path))

        assert exit_code == -1
        assert "TIMEOUT after 10s" in output
        assert '{"sessionID":"ses_timeout_1"}' in output
        assert client.extract_session_id(output) == "ses_timeout_1"


class TestOpenCodeConfigEnv:
    def test_default_config_path_points_to_repo_opencode_json(self, client):
        expected = os.path.abspath(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "opencode.json")
        )
        assert client.config_path == expected

    def test_explicit_config_path_overrides_default(self):
        client = OpenCodeClient(timeout=10, config_path="custom/opencode-alt.json")
        expected = os.path.abspath(
            os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "custom/opencode-alt.json",
            )
        )
        assert client.config_path == expected

    def test_exec_injects_opencode_config_into_subprocess_env(
        self, client, tmp_path, monkeypatch
    ):
        captured_env = {}

        class _FakeProc:
            def __init__(self):
                self.returncode = 0

            def communicate(self, timeout=None):
                return ('{"sessionID":"ses_env"}\n', "")

        def _fake_popen(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _FakeProc()

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)

        output, exit_code, _ = client._exec(["opencode", "run"], str(tmp_path))

        assert exit_code == 0
        assert "ses_env" in output
        assert captured_env["OPENCODE_CONFIG"] == client.config_path

    def test_run_passes_opencode_config_to_all_continue_calls(self, client):
        first_output = '{"sessionID":"ses_continue"}\n'
        second_output = _build_output((["done"], [], "stop"))
        calls = []

        def _fake_exec(cmd, work_dir, task_id="", env=None):
            calls.append({"cmd": list(cmd), "env": client._build_proc_env(env)})
            if len(calls) == 1:
                return first_output, 1, 0.1
            return second_output, 0, 0.1

        with patch.object(client, "_exec", side_effect=_fake_exec):
            run = client.run(
                message="hello",
                work_dir="/repo",
                require_stop=True,
                max_continues=2,
            )

        assert run.exit_code == 0
        assert len(calls) == 2
        assert calls[0]["env"]["OPENCODE_CONFIG"] == client.config_path
        assert calls[1]["env"]["OPENCODE_CONFIG"] == client.config_path
        assert calls[1]["cmd"][-1] == "Continue"

    def test_run_streaming_passes_opencode_config_to_stream_exec(self, client):
        captured_envs = []

        def _fake_exec_streaming(**kwargs):
            captured_envs.append(client._build_proc_env(kwargs.get("env")))
            return _build_output((["done"], [], "stop")), 0, 0.1, "ses_stream", False

        with patch.object(client, "_exec_streaming", side_effect=_fake_exec_streaming):
            run = client.run_streaming(
                message="hello",
                work_dir="/repo",
                require_stop=True,
                max_continues=1,
            )

        assert run.exit_code == 0
        assert len(captured_envs) == 1
        assert captured_envs[0]["OPENCODE_CONFIG"] == client.config_path
