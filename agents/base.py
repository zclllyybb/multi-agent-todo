"""Base agent class wrapping opencode invocations."""

import logging
from core.opencode_client import OpenCodeClient
from core.models import AgentRun

log = logging.getLogger(__name__)


class BaseAgent:
    agent_type: str = "base"
    default_max_continues: int = OpenCodeClient.DEFAULT_MAX_CONTINUES

    def __init__(
        self,
        model: str,
        client: OpenCodeClient,
        variant: str = "",
        agent: str = "",
    ):
        self.model = model
        self.variant = variant
        self.agent = agent
        self.client = client

    def run(
        self,
        prompt: str,
        work_dir: str,
        task_id: str = "",
        session_id: str = "",
        variant: str = "",
        max_continues: int | None = None,
    ) -> AgentRun:
        return self.client.run(
            message=prompt,
            work_dir=work_dir,
            model=self.model,
            agent_type=self.agent_type,
            task_id=task_id,
            session_id=session_id,
            variant=variant or self.variant,
            agent=self.agent,
            max_continues=(
                self.default_max_continues if max_continues is None else max_continues
            ),
        )

    def get_response_text(self, run: AgentRun) -> str:
        return self.client.extract_text_response(run.output)

    def get_final_text(self, run: AgentRun) -> str:
        return self.client.extract_last_text_block_or_raw(run.output)

    def get_text(self, run: AgentRun) -> str:
        """Backward-compatible alias for response text extraction."""
        return self.get_response_text(run)
