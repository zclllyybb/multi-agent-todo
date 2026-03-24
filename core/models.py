"""Data models for the multi-agent system."""

import enum
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


class ModelOutputError(Exception):
    """Raised when a model's output cannot be parsed into the expected format.

    The orchestrator catches this to retry the model call once, then fails the
    task if the second attempt also produces unparseable output.
    """


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PLANNING = "planning"
    CODING = "coding"
    REVIEWING = "reviewing"
    REVIEW_FAILED = "review_failed"
    NEEDS_ARBITRATION = "needs_arbitration"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskSource(str, enum.Enum):
    TODO_SCAN = "todo_scan"
    MANUAL = "manual"
    PLANNER = "planner"


class TodoItemStatus(str, enum.Enum):
    PENDING_ANALYSIS = "pending_analysis"
    ANALYZING = "analyzing"     # analyzer agent is currently running
    ANALYZED = "analyzed"
    DISPATCHED = "dispatched"   # sent to planner → became a task
    DELETED = "deleted"


@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    source: TaskSource = TaskSource.MANUAL

    # File location (for TODO-sourced tasks)
    file_path: str = ""
    line_number: int = 0

    # Git worktree info
    worktree_path: str = ""
    branch_name: str = ""

    # Complexity assessed by planner (very_complex / complex / medium / simple)
    complexity: str = ""

    # Agent outputs
    plan_output: str = ""
    code_output: str = ""
    review_output: str = ""      # concatenated summary of all reviewer outputs
    review_pass: bool = False
    # Manual feedback submitted by the user via Revise Task
    user_feedback: str = ""
    # Per-reviewer verdicts: [{"model": ..., "passed": bool, "output": str}]
    reviewer_results: List[dict] = field(default_factory=list)

    # Retry
    retry_count: int = 0
    max_retries: int = 4

    # Timestamps
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    published_at: float = 0.0

    # Parent task (for sub-tasks created by planner)
    parent_id: Optional[str] = None

    # IDs of sibling tasks that must complete before this task can start
    depends_on: List[str] = field(default_factory=list)

    # Session IDs per agent phase: {"planner": "ses_xxx", "coder": ["ses_xxx", ...], "reviewer": ["ses_xxx", ...]}
    session_ids: Dict[str, list] = field(default_factory=dict)

    # Files to copy from main workspace into the worktree (relative to repo root)
    copy_files: List[str] = field(default_factory=list)

    # Task mode: 'develop' (plan→code→review) or 'review' (reviewer-only)
    task_mode: str = "develop"
    # For review-only tasks: the patch content / URL / description to review
    review_input: str = ""

    # Error info
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["priority"] = self.priority.value
        d["source"] = self.source.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["status"] = TaskStatus(d["status"])
        d["priority"] = TaskPriority(d["priority"])
        d["source"] = TaskSource(d["source"])
        d.setdefault("session_ids", {})
        d.setdefault("complexity", "")
        d.setdefault("reviewer_results", [])
        d.setdefault("published_at", 0.0)
        d.setdefault("copy_files", [])
        d.setdefault("task_mode", "develop")
        d.setdefault("review_input", "")
        d.setdefault("user_feedback", "")
        d.setdefault("depends_on", [])
        return cls(**d)


@dataclass
class TodoItem:
    """A raw scanned TODO comment waiting for user review."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    file_path: str = ""
    line_number: int = 0
    raw_text: str = ""        # original comment line
    description: str = ""    # stripped TODO text
    status: TodoItemStatus = TodoItemStatus.PENDING_ANALYSIS

    # Analyzer scores (0-10 each; -1.0 = not yet scored)
    feasibility_score: float = -1.0   # 0-10: can/should be done now?
    difficulty_score: float = -1.0    # 0-10: how hard to implement correctly? (higher = harder)
    analysis_note: str = ""           # two-sentence explanation from analyzer
    analyze_output: str = ""          # raw model output for progress display

    task_id: str = ""         # set once dispatched to planner
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TodoItem":
        d = dict(d)
        d["status"] = TodoItemStatus(d.get("status", "pending_analysis"))
        # Backward compat: old records had relevance_score instead of difficulty_score
        if "relevance_score" in d and "difficulty_score" not in d:
            d["difficulty_score"] = d.pop("relevance_score")
        d.pop("relevance_score", None)
        d.setdefault("difficulty_score", -1.0)
        d.setdefault("analyze_output", "")
        return cls(**d)


@dataclass
class AgentRun:
    """Record of a single agent invocation."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = ""
    agent_type: str = ""  # planner / coder / reviewer
    model: str = ""
    prompt: str = ""
    output: str = ""
    exit_code: int = -1
    duration_sec: float = 0.0
    session_id: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentRun":
        d = dict(d)
        d.setdefault("session_id", "")
        return cls(**d)
