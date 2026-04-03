"""Explorer agent: autonomously explores code modules for quality issues."""

import json
import logging
import re
from typing import Callable, List, Optional, Tuple

from agents.base import BaseAgent
from core.models import AgentRun, ExploreModule, ModelOutputError

log = logging.getLogger(__name__)


class ExplorerAgent(BaseAgent):
    agent_type: str = "explorer"

    @staticmethod
    def _clamp_score(value, default: float = -1.0) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return default
        if score < 0.0:
            return 0.0
        if score > 10.0:
            return 10.0
        return score

    @staticmethod
    def _build_explore_prompt(
        module: ExploreModule,
        category: str,
        personality_focus: str,
        personality_name: str,
        repo_path: str,
        focus_point: str = "",
        prior_note: str = "",
    ) -> str:
        from agents.prompts import explorer_prompt
        return explorer_prompt(
            module_name=module.name,
            module_path=module.path,
            module_description=module.description,
            category=category,
            personality_name=personality_name,
            personality_focus=personality_focus,
            repo_path=repo_path,
            focus_point=focus_point,
            prior_note=prior_note,
        )

    def explore_module(
        self,
        module: ExploreModule,
        category: str,
        personality_focus: str,
        personality_name: str,
        repo_path: str,
        focus_point: str = "",
        prior_note: str = "",
        agent_variant: str = "",
    ) -> Tuple[AgentRun, List[dict], str]:
        """Explore a module for a specific category of issues.

        Returns ``(agent_run, findings_list, summary_text)``.
        *findings_list* items have keys: severity, title, description,
        file_path, line_number, suggested_fix.
        """
        prompt = self._build_explore_prompt(
            module=module,
            category=category,
            personality_focus=personality_focus,
            personality_name=personality_name,
            repo_path=repo_path,
            focus_point=focus_point,
            prior_note=prior_note,
        )
        run = self.run(prompt, repo_path, agent_variant=agent_variant)
        text = self.get_text(run)
        findings, summary = self._parse_output(text)
        return run, findings, summary

    @staticmethod
    def parse_output_metadata(text: str) -> dict:
        """Extract exploration metadata from explorer JSON output."""
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

        completion_status = str(data.get("completion_status", "complete")).strip().lower()
        if completion_status not in {"complete", "partial"}:
            completion_status = "complete"

        return {
            "summary": str(data.get("summary", "")),
            "focus_point": str(data.get("focus_point", "")).strip(),
            "actionability_score": ExplorerAgent._clamp_score(
                data.get("actionability_score", -1.0)
            ),
            "reliability_score": ExplorerAgent._clamp_score(
                data.get("reliability_score", -1.0)
            ),
            "explored_scope": str(data.get("explored_scope", "")).strip(),
            "completion_status": completion_status,
            "supplemental_note": str(data.get("supplemental_note", "")).strip(),
            "map_review_required": bool(data.get("map_review_required", False)),
            "map_review_reason": str(data.get("map_review_reason", "")).strip(),
        }

    def explore_module_streaming(
        self,
        module: ExploreModule,
        category: str,
        personality_focus: str,
        personality_name: str,
        repo_path: str,
        focus_point: str = "",
        prior_note: str = "",
        task_id: str = "",
        session_id: str = "",
        message_override: Optional[str] = None,
        on_output: Optional[Callable[[str, str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        agent_variant: str = "",
    ) -> Tuple[AgentRun, List[dict], str]:
        prompt = message_override or self._build_explore_prompt(
            module=module,
            category=category,
            personality_focus=personality_focus,
            personality_name=personality_name,
            repo_path=repo_path,
            focus_point=focus_point,
            prior_note=prior_note,
        )
        run = self.client.run_streaming(
            message=prompt,
            work_dir=repo_path,
            model=self.model,
            agent_type=self.agent_type,
            task_id=task_id,
            session_id=session_id,
            on_output=on_output,
            should_cancel=should_cancel,
            agent_variant=agent_variant,
        )
        if run.exit_code == -2:
            return run, [], ""
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

    def init_map(self, repo_path: str, max_depth: int = 2, agent_variant: str = "") -> Tuple[AgentRun, List[dict]]:
        """Run map initialization agent to discover the project module structure.

        Returns ``(agent_run, modules_list)`` where each module dict has keys:
        name, path, description, children (recursive).
        """
        from agents.prompts import map_init_prompt

        prompt = map_init_prompt(repo_path=repo_path, max_depth=max_depth)
        run = self.run(prompt, repo_path, agent_variant=agent_variant)
        text = self.get_text(run)
        modules = self._parse_map_output(text)
        return run, modules

    def init_map_streaming(
        self,
        repo_path: str,
        max_depth: int = 2,
        task_id: str = "",
        session_id: str = "",
        message_override: Optional[str] = None,
        on_output: Optional[Callable[[str, str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        agent_variant: str = "",
    ) -> Tuple[AgentRun, List[dict]]:
        from agents.prompts import map_init_prompt

        prompt = message_override or map_init_prompt(repo_path=repo_path, max_depth=max_depth)
        run = self.client.run_streaming(
            message=prompt,
            work_dir=repo_path,
            model=self.model,
            agent_type="explorer_map_init",
            task_id=task_id,
            session_id=session_id,
            on_output=on_output,
            should_cancel=should_cancel,
            agent_variant=agent_variant,
        )
        if run.exit_code == -2:
            raise RuntimeError("map init cancelled")
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
