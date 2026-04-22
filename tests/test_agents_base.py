from unittest.mock import MagicMock

from agents.base import BaseAgent


class _TestAgent(BaseAgent):
    agent_type = "test"


def test_base_agent_run_requires_stop_by_default():
    client = MagicMock()
    client.run.return_value = MagicMock()
    agent = _TestAgent(model="m", client=client)

    agent.run("hello", "/repo")

    assert "require_stop" not in client.run.call_args.kwargs
