"""Base agent class wrapping opencode invocations."""

import logging
from core.opencode_client import OpenCodeClient
from core.models import AgentRun

log = logging.getLogger(__name__)


class BaseAgent:
    agent_type: str = "base"

    def __init__(self, model: str, client: OpenCodeClient):
        self.model = model
        self.client = client

    def run(
        self,
        prompt: str,
        work_dir: str,
        task_id: str = "",
        session_id: str = "",
        agent_variant: str = "",
    ) -> AgentRun:
        return self.client.run(
            message=prompt,
            work_dir=work_dir,
            model=self.model,
            agent_type=self.agent_type,
            task_id=task_id,
            session_id=session_id,
            agent_variant=agent_variant,
        )

    def get_text(self, run: AgentRun) -> str:
        return self.client.extract_text_response(run.output)
