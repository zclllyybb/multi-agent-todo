import json
from types import SimpleNamespace
from unittest.mock import patch

import cli


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_cmd_add_uses_daemon_api(capsys):
    args = SimpleNamespace(
        config=None,
        title="Fix bug",
        description="Do the thing",
        priority="high",
        force_no_split=False,
    )
    config = {"web": {"host": "0.0.0.0", "port": 8778}}

    def _urlopen(req, timeout):
        assert req.full_url == "http://127.0.0.1:8778/api/tasks"
        assert req.get_method() == "POST"
        payload = json.loads(req.data.decode("utf-8"))
        assert payload == {
            "title": "Fix bug",
            "description": "Do the thing",
            "priority": "high",
            "force_no_split": False,
        }
        return _FakeResponse({"id": "task123", "title": "Fix bug"})

    with (
        patch("cli.load_config", return_value=config),
        patch("cli.request.urlopen", side_effect=_urlopen),
    ):
        cli.cmd_add(args)

    out = capsys.readouterr().out
    assert "Submitted task: [task123] Fix bug" in out


def test_cmd_add_can_force_no_split(capsys):
    args = SimpleNamespace(
        config=None,
        title="Fix bug",
        description="Do the thing",
        priority="high",
        force_no_split=True,
    )
    config = {"web": {"host": "0.0.0.0", "port": 8778}}

    def _urlopen(req, timeout):
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["force_no_split"] is True
        return _FakeResponse({"id": "task123", "title": "Fix bug"})

    with (
        patch("cli.load_config", return_value=config),
        patch("cli.request.urlopen", side_effect=_urlopen),
    ):
        cli.cmd_add(args)

    out = capsys.readouterr().out
    assert "Submitted task: [task123] Fix bug" in out


def test_cmd_start_initializes_skill_before_starting_daemon(capsys):
    args = SimpleNamespace(config=None, foreground=False)

    with (
        patch("cli.ensure_skill_initialized", return_value=["/tmp/SKILL.md", "/tmp/ask.sh"]),
        patch("cli.daemon_mod.start") as start_mock,
    ):
        cli.cmd_start(args)

    start_mock.assert_called_once_with(config_path=None, foreground=False)
    out = capsys.readouterr().out
    assert "Initialized Claude skill asset: /tmp/SKILL.md" in out
    assert "Initialized Claude skill asset: /tmp/ask.sh" in out


def test_cmd_start_reports_existing_skill_when_already_present(capsys):
    args = SimpleNamespace(config=None, foreground=True)

    with (
        patch("cli.ensure_skill_initialized", return_value=[]),
        patch("cli.daemon_mod.start") as start_mock,
    ):
        cli.cmd_start(args)

    start_mock.assert_called_once_with(config_path=None, foreground=True)
    out = capsys.readouterr().out
    assert "Claude skill already initialized: opencode-session-ask" in out


def test_cmd_dispatch_uses_daemon_api_and_reports_queue(capsys):
    args = SimpleNamespace(config=None, task_id="task123")
    config = {"web": {"host": "127.0.0.1", "port": 8778}}

    def _urlopen(req, timeout):
        assert req.full_url == "http://127.0.0.1:8778/api/tasks/task123/dispatch"
        assert req.get_method() == "POST"
        return _FakeResponse({"dispatched": False, "queued": True})

    with (
        patch("cli.load_config", return_value=config),
        patch("cli.request.urlopen", side_effect=_urlopen),
    ):
        cli.cmd_dispatch(args)

    out = capsys.readouterr().out
    assert "Dispatched: False" in out
    assert "Queued: True" in out


def test_cmd_dispatch_all_uses_daemon_api(capsys):
    args = SimpleNamespace(config=None, task_id="all")
    config = {"web": {"host": "127.0.0.1", "port": 8778}}

    def _urlopen(req, timeout):
        assert req.full_url == "http://127.0.0.1:8778/api/dispatch-all"
        assert req.get_method() == "POST"
        return _FakeResponse({"dispatched": 2, "total_pending": 5})

    with (
        patch("cli.load_config", return_value=config),
        patch("cli.request.urlopen", side_effect=_urlopen),
    ):
        cli.cmd_dispatch(args)

    out = capsys.readouterr().out
    assert "Dispatched 2/5 tasks" in out
