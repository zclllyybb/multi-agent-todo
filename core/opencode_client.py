"""Wrapper around the opencode CLI tool."""

import datetime
import json
import logging
import os
import select
import signal
import subprocess
import threading
import time
from typing import Callable, Optional, Set, Tuple

from core.models import AgentRun

log = logging.getLogger(__name__)


def _ts_fmt(ts_ms: int) -> str:
    """Format a millisecond timestamp to HH:MM:SS."""
    if not ts_ms:
        return ""
    return datetime.datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S")


class OpenCodeClient:
    DEFAULT_MAX_CONTINUES = 8

    def __init__(self, timeout: int = 600, config_path: str = ""):
        self.timeout = timeout
        self.config_path = self._resolve_config_path(config_path)
        self._active_procs: Set[subprocess.Popen] = set()
        self._task_procs: dict = {}  # task_id -> Popen
        self._proc_lock = threading.Lock()

    @staticmethod
    def _default_config_path() -> str:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "opencode.json",
        )

    def _resolve_config_path(self, config_path: str = "") -> str:
        candidate = str(config_path or "").strip() or self._default_config_path()
        if not os.path.isabs(candidate):
            candidate = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                candidate,
            )
        return os.path.abspath(candidate)

    def _build_proc_env(self, env: Optional[dict[str, str]] = None) -> dict[str, str]:
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        proc_env["OPENCODE_CONFIG"] = self.config_path
        return proc_env

    @staticmethod
    def _normalize_cli_option(value: str) -> str:
        return str(value or "").strip()

    def _exec(
        self,
        cmd: list,
        work_dir: str,
        task_id: str = "",
        env: Optional[dict[str, str]] = None,
    ) -> Tuple[str, int, float]:
        """Execute an opencode command and return (stdout, exit_code, duration)."""
        start = time.time()
        proc_env = self._build_proc_env(env)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=work_dir,
            env=proc_env,
            start_new_session=False,
        )
        with self._proc_lock:
            self._active_procs.add(proc)
            if task_id:
                self._task_procs[task_id] = proc
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
            duration = time.time() - start
            exit_code = proc.returncode

            if stderr:
                if exit_code != 0:
                    log.warning(
                        "opencode stderr (exit=%d): %s", exit_code, stderr.strip()[:500]
                    )
                else:
                    log.debug("opencode stderr: %s", stderr[:500])

            return stdout, exit_code, duration

        except subprocess.TimeoutExpired as timeout_err:
            partial_stdout = timeout_err.stdout or ""
            partial_stderr = timeout_err.stderr or ""
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode("utf-8", errors="replace")
            if isinstance(partial_stderr, bytes):
                partial_stderr = partial_stderr.decode("utf-8", errors="replace")
            proc.kill()
            proc.wait()
            if proc.stdout is not None:
                tail = proc.stdout.read() or ""
                partial_stdout += tail
            if proc.stderr is not None:
                tail_err = proc.stderr.read() or ""
                partial_stderr += tail_err
            duration = time.time() - start
            if partial_stderr:
                log.warning("opencode timeout stderr: %s", partial_stderr.strip()[:500])
            log.error("opencode timed out after %ds", self.timeout)
            timeout_marker = f"TIMEOUT after {self.timeout}s"
            merged_output = partial_stdout
            if merged_output and not merged_output.endswith("\n"):
                merged_output += "\n"
            merged_output += timeout_marker
            return merged_output, -1, duration
        finally:
            with self._proc_lock:
                self._active_procs.discard(proc)
                if task_id and self._task_procs.get(task_id) is proc:
                    del self._task_procs[task_id]

    def _terminate_proc(self, proc: subprocess.Popen):
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except OSError:
            pass

    def _exec_streaming(
        self,
        cmd: list,
        work_dir: str,
        task_id: str = "",
        env: Optional[dict[str, str]] = None,
        on_output: Optional[Callable[[str, str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Tuple[str, int, float, str, bool]:
        """Execute an opencode command while streaming output chunks.

        Returns (output, exit_code, duration, session_id, was_cancelled).
        """
        start = time.time()
        proc_env = self._build_proc_env(env)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=work_dir,
            env=proc_env,
            start_new_session=False,
            bufsize=1,
        )
        with self._proc_lock:
            self._active_procs.add(proc)
            if task_id:
                self._task_procs[task_id] = proc

        chunks: list[str] = []
        sid = ""
        cancelled = False
        deadline = start + self.timeout

        try:
            stdout = proc.stdout
            if stdout is None:
                duration = time.time() - start
                return "", -1, duration, sid, False

            while True:
                if should_cancel and should_cancel():
                    cancelled = True
                    self._terminate_proc(proc)
                    break

                if time.time() > deadline:
                    log.error("opencode timed out after %ds", self.timeout)
                    self._terminate_proc(proc)
                    duration = time.time() - start
                    timeout_marker = f"TIMEOUT after {self.timeout}s"
                    timed_out_output = "".join(chunks)
                    if timed_out_output and not timed_out_output.endswith("\n"):
                        timed_out_output += "\n"
                    timed_out_output += timeout_marker
                    return timed_out_output, -1, duration, sid, False

                ready, _, _ = select.select([stdout], [], [], 0.2)
                if ready:
                    line = stdout.readline()
                    if line:
                        chunks.append(line)
                        maybe_sid = self.extract_session_id(line)
                        if maybe_sid:
                            sid = maybe_sid
                        if on_output:
                            on_output(line, sid)
                    elif proc.poll() is not None:
                        break
                elif proc.poll() is not None:
                    break

            remainder = stdout.read() or ""
            if remainder:
                chunks.append(remainder)
                maybe_sid = self.extract_session_id(remainder)
                if maybe_sid:
                    sid = maybe_sid
                if on_output:
                    on_output(remainder, sid)

            proc.wait()
            duration = time.time() - start
            return "".join(chunks), proc.returncode, duration, sid, cancelled

        finally:
            with self._proc_lock:
                self._active_procs.discard(proc)
                if task_id and self._task_procs.get(task_id) is proc:
                    del self._task_procs[task_id]

    def run(
        self,
        message: str,
        work_dir: str,
        model: str = "opencode/gpt-5-nano",
        agent_type: str = "coder",
        task_id: str = "",
        session_id: str = "",
        max_continues: int = DEFAULT_MAX_CONTINUES,
        env: Optional[dict[str, str]] = None,
        variant: str = "",
        agent: str = "",
    ) -> AgentRun:
        """Run opencode with a message in a specific directory.

        If session_id is provided, continues that existing session via
        ``opencode run --session <id>`` so the model retains full context.

        When opencode exits with a non-zero code and a session ID is available,
        automatically sends "Continue" to resume the session up to
        *max_continues* times.

        Returns an AgentRun record.
        """
        cmd = [
            "opencode",
            "run",
            "--model",
            model,
            "--dir",
            work_dir,
            "--format",
            "json",
        ]
        variant = self._normalize_cli_option(variant)
        agent = self._normalize_cli_option(agent)
        if variant:
            cmd.extend(["--variant", variant])
        if agent:
            cmd.extend(["--agent", agent])
        if session_id:
            cmd.extend(["--session", session_id])
        cmd.append(message)

        log.info(
            "Running opencode [%s] model=%s dir=%s variant=%s agent=%s",
            agent_type,
            model,
            work_dir,
            variant or "-",
            agent or "-",
        )
        log.debug("Prompt: %s", message)

        output, exit_code, duration = self._exec(cmd, work_dir, task_id, env=env)

        # Extract session ID from output
        sid = self.extract_session_id(output) or session_id

        # Auto-continue: if the session failed and we have a session ID,
        # send "Continue" to resume the session.
        continue_count = 0
        while sid and continue_count < max_continues:
            needs_continue = exit_code != 0 or not self.is_output_complete(output)
            if not needs_continue:
                break
            continue_count += 1
            log.warning(
                "opencode [%s] needs continue (exit=%d), session %s (%d/%d)",
                agent_type,
                exit_code,
                sid,
                continue_count,
                max_continues,
            )
            cont_cmd = [
                "opencode",
                "run",
                "--model",
                model,
                "--dir",
                work_dir,
                "--format",
                "json",
            ]
            if variant:
                cont_cmd.extend(["--variant", variant])
            if agent:
                cont_cmd.extend(["--agent", agent])
            cont_cmd.extend(["--session", sid, "Continue"])
            cont_output, exit_code, cont_duration = self._exec(
                cont_cmd, work_dir, task_id, env=env
            )
            output += cont_output
            duration += cont_duration
            sid = self.extract_session_id(cont_output) or sid

        run = AgentRun(
            task_id=task_id,
            agent_type=agent_type,
            model=model,
            prompt=message,
            output=output,
            exit_code=exit_code,
            duration_sec=duration,
            session_id=sid,
        )
        log.info(
            "opencode [%s] finished: exit=%d duration=%.1fs session=%s output_len=%d continues=%d",
            agent_type,
            exit_code,
            duration,
            sid,
            len(output),
            continue_count,
        )
        return run

    def run_streaming(
        self,
        message: str,
        work_dir: str,
        model: str = "opencode/gpt-5-nano",
        agent_type: str = "coder",
        task_id: str = "",
        session_id: str = "",
        max_continues: int = DEFAULT_MAX_CONTINUES,
        env: Optional[dict[str, str]] = None,
        on_output: Optional[Callable[[str, str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        variant: str = "",
        agent: str = "",
    ) -> AgentRun:
        """Run opencode with streaming output and optional cancellation."""
        total_duration = 0.0
        output = ""
        sid = session_id
        continue_count = 0
        current_message = message

        while True:
            variant = self._normalize_cli_option(variant)
            agent = self._normalize_cli_option(agent)
            cmd = [
                "opencode",
                "run",
                "--model",
                model,
                "--dir",
                work_dir,
                "--format",
                "json",
            ]
            if variant:
                cmd.extend(["--variant", variant])
            if agent:
                cmd.extend(["--agent", agent])
            if sid:
                cmd.extend(["--session", sid])
            cmd.append(current_message)

            log.info(
                "Running opencode(stream) [%s] model=%s dir=%s session=%s variant=%s agent=%s",
                agent_type,
                model,
                work_dir,
                sid,
                variant or "-",
                agent or "-",
            )
            chunk_out, exit_code, duration, observed_sid, cancelled = (
                self._exec_streaming(
                    cmd=cmd,
                    work_dir=work_dir,
                    task_id=task_id,
                    env=env,
                    on_output=on_output,
                    should_cancel=should_cancel,
                )
            )
            total_duration += duration
            output += chunk_out
            sid = observed_sid or self.extract_session_id(output) or sid

            if cancelled:
                exit_code = -2
                break

            needs_continue = exit_code != 0 or not self.is_output_complete(output)
            if not needs_continue:
                break

            if not sid or continue_count >= max_continues:
                break

            continue_count += 1
            log.warning(
                "opencode(stream) [%s] needs continue (exit=%d), session %s (%d/%d)",
                agent_type,
                exit_code,
                sid,
                continue_count,
                max_continues,
            )
            current_message = "Continue"

        run = AgentRun(
            task_id=task_id,
            agent_type=agent_type,
            model=model,
            prompt=message,
            output=output,
            exit_code=exit_code,
            duration_sec=total_duration,
            session_id=sid,
        )
        log.info(
            "opencode(stream) [%s] finished: exit=%d duration=%.1fs session=%s output_len=%d continues=%d",
            agent_type,
            exit_code,
            total_duration,
            sid,
            len(output),
            continue_count,
        )
        return run

    def kill_task(self, task_id: str):
        """Kill the opencode process currently running for a specific task."""
        with self._proc_lock:
            proc = self._task_procs.get(task_id)
        if not proc:
            log.debug("kill_task: no active process for task %s", task_id)
            return
        log.info("Killing opencode process pid=%d for task %s", proc.pid, task_id)
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("Force-killing opencode pid=%d", proc.pid)
            proc.kill()
            proc.wait()
        except OSError:
            pass

    def kill_all(self):
        """Kill all active opencode processes (called on daemon shutdown)."""
        with self._proc_lock:
            procs = list(self._active_procs)
        if not procs:
            return
        log.info("Killing %d active opencode process(es)...", len(procs))
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        # Give them a moment to exit gracefully, then force-kill
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("Force-killing opencode pid=%d", proc.pid)
                proc.kill()
                proc.wait()
            except OSError:
                pass
        with self._proc_lock:
            self._active_procs.clear()
        log.info("All opencode processes terminated")

    def parse_json_output(self, output: str) -> list:
        """Parse the JSON-format output from opencode run.
        opencode --format json outputs newline-delimited JSON events.
        """
        events = []
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def extract_session_id(self, output: str) -> str:
        """Extract the session ID from the first event in the output."""
        events = self.parse_json_output(output)
        for ev in events:
            if isinstance(ev, dict) and "sessionID" in ev:
                return ev["sessionID"]
        return ""

    def extract_text_response(self, output: str) -> str:
        """Extract just the text portions from opencode output."""
        events = self.parse_json_output(output)
        texts = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "text":
                part = ev.get("part", {})
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        texts.append(text)
        if texts:
            return "".join(texts)
        return output

    def parse_readable_output(self, output: str) -> dict:
        """Parse opencode JSON events into a structured, human-readable format.

        Returns a dict with:
          session_id: str
          steps: list of step dicts, each containing:
            step_num: int
            events: list of {type, time, content} dicts
          summary: {text_count, tool_count, total_steps}
        """
        events = self.parse_json_output(output)
        if not events:
            return {
                "session_id": "",
                "steps": [],
                "summary": {},
                "raw_fallback": output[:2000],
            }

        session_id = ""
        steps = []
        current_step = None
        step_num = 0

        for ev in events:
            if not isinstance(ev, dict):
                continue

            if not session_id and "sessionID" in ev:
                session_id = ev["sessionID"]

            ev_type = ev.get("type", "")
            ts = _ts_fmt(ev.get("timestamp", 0))
            part = ev.get("part", {})
            if not isinstance(part, dict):
                part = {}

            if ev_type == "step_start":
                step_num += 1
                current_step = {"step_num": step_num, "events": []}
                steps.append(current_step)

            elif ev_type == "text":
                text = part.get("text", "")
                if text and current_step is not None:
                    current_step["events"].append(
                        {
                            "type": "text",
                            "time": ts,
                            "content": text,
                        }
                    )

            elif ev_type == "tool_use":
                tool_name = part.get("tool", "") or part.get("name", "")
                state = part.get("state", {})
                inp = state.get("input", {}) if isinstance(state, dict) else {}
                out = state.get("output", "") if isinstance(state, dict) else ""
                status = state.get("status", "") if isinstance(state, dict) else ""
                # Build concise tool summary
                inp_summary = ""
                if isinstance(inp, dict):
                    # Show first few meaningful keys
                    for k in (
                        "pattern",
                        "path",
                        "filePath",
                        "file_path",
                        "command",
                        "query",
                        "content",
                        "regex",
                    ):
                        if k in inp:
                            v = str(inp[k])
                            inp_summary = f"{k}={v[:120]}"
                            break
                    if not inp_summary:
                        items = list(inp.items())[:2]
                        inp_summary = ", ".join(f"{k}={str(v)[:80]}" for k, v in items)
                elif isinstance(inp, str):
                    inp_summary = inp[:120]

                out_summary = ""
                if isinstance(out, str) and out:
                    out_summary = out[:200]
                    if len(out) > 200:
                        out_summary += f"... ({len(out)} chars)"

                if current_step is not None:
                    current_step["events"].append(
                        {
                            "type": "tool",
                            "time": ts,
                            "tool": tool_name,
                            "status": status,
                            "input": inp_summary,
                            "output": out_summary,
                        }
                    )

            elif ev_type == "step_finish":
                reason = part.get("reason", "")
                if current_step is not None:
                    current_step["finish_reason"] = reason

        # Build summary
        text_count = sum(
            1 for s in steps for e in s.get("events", []) if e["type"] == "text"
        )
        tool_count = sum(
            1 for s in steps for e in s.get("events", []) if e["type"] == "tool"
        )

        return {
            "session_id": session_id,
            "steps": steps,
            "summary": {
                "total_steps": len(steps),
                "text_segments": text_count,
                "tool_calls": tool_count,
            },
        }

    def is_output_complete(self, output: str) -> bool:
        """Check whether the model output ended with a proper ``stop``.

        Returns False if the last step has no ``step_finish`` event with
        ``reason='stop'``, which indicates the output was truncated or the
        model failed to produce a complete response.
        """
        parsed = self.parse_readable_output(output)
        steps = parsed.get("steps", [])
        if not steps:
            return False
        last_step = steps[-1]
        return last_step.get("finish_reason") == "stop"

    def has_readable_steps(self, output: str) -> bool:
        """Return whether the output contains at least one structured step.

        This distinguishes malformed / wrong-format output from a valid opencode
        transcript that is merely incomplete. Outputs that would render as
        "No events" in the UI return False here.
        """
        parsed = self.parse_readable_output(output)
        return bool(parsed.get("steps"))

    def extract_last_text_block(self, output: str) -> str:
        """Extract only the text from the final step that ends with ``stop``.

        Used to build the ``coder_response`` for reviewers — we only want
        the coder's concluding summary, not the entire session transcript.
        Returns empty string if the last stop-step has no text events.
        """
        parsed = self.parse_readable_output(output)
        steps = parsed.get("steps", [])
        # Walk backwards to find the last step with finish_reason='stop'
        for step in reversed(steps):
            if step.get("finish_reason") != "stop":
                continue
            texts = [
                ev["content"]
                for ev in step.get("events", [])
                if ev.get("type") == "text" and ev.get("content")
            ]
            if texts:
                return "".join(texts)
        return ""

    def extract_last_text_block_or_raw(self, output: str) -> str:
        """Return the final stop-step text, or the raw output when unavailable.

        This is useful for single-agent workflows where the model may either
        emit plain text or structured NDJSON text events. It avoids forcing the
        caller to separately handle both forms.
        """
        text = self.extract_last_text_block(output)
        if text:
            return text
        return self.extract_text_response(output)

    def format_readable_text(self, output: str) -> str:
        """Convert opencode JSON output into a plain-text readable log."""
        parsed = self.parse_readable_output(output)
        lines = []
        if parsed.get("session_id"):
            lines.append(f"Session: {parsed['session_id']}")
            lines.append(f"  opencode --session {parsed['session_id']}")
            lines.append("")

        for step in parsed.get("steps", []):
            lines.append(f"=== Step {step['step_num']} ===")
            for ev in step.get("events", []):
                if ev["type"] == "text":
                    lines.append(f"  [{ev['time']}] {ev['content']}")
                elif ev["type"] == "tool":
                    lines.append(
                        f"  [{ev['time']}] TOOL {ev['tool']} ({ev.get('status', '')})"
                    )
                    if ev.get("input"):
                        lines.append(f"    input: {ev['input']}")
                    if ev.get("output"):
                        lines.append(f"    output: {ev['output']}")
            reason = step.get("finish_reason", "")
            if reason:
                lines.append(f"  -> {reason}")
            lines.append("")

        s = parsed.get("summary", {})
        if s:
            lines.append(
                f"Summary: {s.get('total_steps', 0)} steps, "
                f"{s.get('text_segments', 0)} text segments, "
                f"{s.get('tool_calls', 0)} tool calls"
            )

        if parsed.get("raw_fallback"):
            lines.append(parsed["raw_fallback"])

        return "\n".join(lines)
