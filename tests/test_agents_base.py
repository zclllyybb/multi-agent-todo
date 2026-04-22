from unittest.mock import MagicMock

from agents.base import BaseAgent
from core.opencode_client import OpenCodeClient
from core.models import AgentRun


class _TestAgent(BaseAgent):
    agent_type = "test"


def test_base_agent_run_requires_stop_by_default():
    client = MagicMock()
    client.run.return_value = MagicMock()
    agent = _TestAgent(model="m", client=client)

    agent.run("hello", "/repo")

    assert "require_stop" not in client.run.call_args.kwargs


def test_base_agent_run_uses_shared_default_max_continues():
    client = MagicMock()
    client.run.return_value = MagicMock()
    agent = _TestAgent(model="m", client=client)

    agent.run("hello", "/repo")

    assert client.run.call_args.kwargs["max_continues"] == agent.default_max_continues


def test_base_agent_get_response_text_uses_full_text_response():
    client = MagicMock()
    client.extract_text_response.return_value = "all text"
    agent = _TestAgent(model="m", client=client)
    run = AgentRun(output="raw")

    assert agent.get_response_text(run) == "all text"


def test_base_agent_get_final_text_uses_final_block_extractor():
    client = MagicMock()
    client.extract_last_text_block_or_raw.return_value = "final text"
    agent = _TestAgent(model="m", client=client)
    run = AgentRun(output="raw")

    assert agent.get_final_text(run) == "final text"


def test_base_agent_get_text_alias_uses_response_text():
    client = MagicMock()
    client.extract_text_response.return_value = "all text"
    agent = _TestAgent(model="m", client=client)
    run = AgentRun(output="raw")

    assert agent.get_text(run) == "all text"


def test_base_agent_default_max_continues_matches_client_constant():
    assert _TestAgent.default_max_continues == OpenCodeClient.DEFAULT_MAX_CONTINUES
