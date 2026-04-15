"""Base agent class wrapping opencode invocations."""

import logging
from core.opencode_client import OpenCodeClient
from core.models import AgentRun

log = logging.getLogger(__name__)


class BaseAgent:
    agent_type: str = "base"

    def __init__(self, model: str, client: OpenCodeClient, variant: str = ""):
        self.model = model
        self.variant = variant
        self.client = client

    def run(
        self,
        prompt: str,
        work_dir: str,
        task_id: str = "",
        session_id: str = "",
        variant: str = "",
        max_continues: int = 1,
        require_stop: bool = False,
    ) -> AgentRun:
        return self.client.run(
            message=prompt,
            work_dir=work_dir,
            model=self.model,
            agent_type=self.agent_type,
            task_id=task_id,
            session_id=session_id,
            variant=variant or self.variant,
            max_continues=max_continues,
            require_stop=require_stop,
        )

    def get_text(self, run: AgentRun) -> str:
        return self.client.extract_text_response(run.output)
