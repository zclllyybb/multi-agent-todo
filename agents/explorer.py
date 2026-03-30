"""Explorer agent: autonomously explores code modules for quality issues."""

import json
import logging
import re
from typing import List, Tuple

from agents.base import BaseAgent
from core.models import AgentRun, ExploreModule, ModelOutputError

log = logging.getLogger(__name__)


class ExplorerAgent(BaseAgent):
    agent_type: str = "explorer"

    def explore_module(
        self,
        module: ExploreModule,
        category: str,
        personality_focus: str,
        personality_name: str,
        repo_path: str,
    ) -> Tuple[AgentRun, List[dict], str]:
        """Explore a module for a specific category of issues.

        Returns ``(agent_run, findings_list, summary_text)``.
        *findings_list* items have keys: severity, title, description,
        file_path, line_number, suggested_fix.
        """
        from agents.prompts import explorer_prompt

        prompt = explorer_prompt(
            module_name=module.name,
            module_path=module.path,
            module_description=module.description,
            category=category,
            personality_name=personality_name,
            personality_focus=personality_focus,
            repo_path=repo_path,
        )
        run = self.run(prompt, repo_path)
        text = self.get_text(run)
        findings, summary = self._parse_output(text)
        return run, findings, summary

    @staticmethod
    def _parse_output(text: str) -> Tuple[List[dict], str]:
        """Extract findings and summary from explorer JSON output."""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ModelOutputError(
                f"explorer: no JSON object found in model output "
                f"(first 200 chars: {text[:200]!r})"
            )
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as e:
            raise ModelOutputError(
                f"explorer: invalid JSON in model output: {e}"
            ) from e

        summary = str(data.get("summary", ""))
        raw_findings = data.get("findings", [])
        findings = []
        for f in raw_findings:
            findings.append({
                "severity": str(f.get("severity", "info")),
                "title": str(f.get("title", "")),
                "description": str(f.get("description", "")),
                "file_path": str(f.get("file_path", "")),
                "line_number": int(f.get("line_number", 0)),
                "suggested_fix": str(f.get("suggested_fix", "")),
            })
        return findings, summary

    def init_map(self, repo_path: str, max_depth: int = 2) -> Tuple[AgentRun, List[dict]]:
        """Run map initialization agent to discover the project module structure.

        Returns ``(agent_run, modules_list)`` where each module dict has keys:
        name, path, description, children (recursive).
        """
        from agents.prompts import map_init_prompt

        prompt = map_init_prompt(repo_path=repo_path, max_depth=max_depth)
        run = self.run(prompt, repo_path)
        text = self.get_text(run)
        modules = self._parse_map_output(text)
        return run, modules

    @staticmethod
    def _parse_map_output(text: str) -> List[dict]:
        """Extract module tree from map init JSON output."""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ModelOutputError(
                f"map_init: no JSON object found in model output "
                f"(first 200 chars: {text[:200]!r})"
            )
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as e:
            raise ModelOutputError(
                f"map_init: invalid JSON in model output: {e}"
            ) from e
        modules = data.get("modules", [])
        if not modules:
            raise ModelOutputError("map_init: no modules found in output")
        return modules
