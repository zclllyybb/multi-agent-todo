"""Centralized prompt templates for all agents.

Edit this file to tune agent behavior without touching agent logic.
"""


# ─────────────────────────────────────────────────────────────────────────────
# ANALYZER AGENT  (scores scanned TODO items before user review)
# ─────────────────────────────────────────────────────────────────────────────

def analyzer_todo(file_path: str, line_number: int, raw_text: str,
                  description: str, repo_path: str) -> str:
    """Prompt asking the model to evaluate a TODO on feasibility and implementation difficulty.

    Output must be a single JSON object with exactly these keys:
      feasibility_score   float 0-10  (should/can this be done now by an automated agent?)
      difficulty_score    float 0-10  (how hard to implement correctly? higher = harder)
      note                str         (two sentences: one for feasibility, one for difficulty)

    feasibility_score rubric:
      0-3  : Not actionable — already fixed elsewhere, depends on unresolved external
             prerequisites, conceptually wrong/outdated, or requires human decisions
             outside an automated agent's scope.
      4-6  : Doable but with caveats — some design uncertainty, minor blockers, or unclear
             whether the original author's intent is still valid.
      7-10 : Ready to implement now — clear goal, self-contained, no known blockers.

    difficulty_score rubric:
      0-3  : Trivial — one-liner or obvious rename, zero risk of breaking anything.
      4-6  : Moderate — touches a few files, straightforward logic, limited blast radius.
      7-9  : Hard — involves multiple subsystems, non-trivial algorithm, or carries
             meaningful risk of introducing regressions.
      10   : Extremely hard — deep architectural change across the whole codebase.
    """
    return f"""You are a code analysis agent. Evaluate the following TODO comment.

Repository: {repo_path}
File: {file_path}:{line_number}
Raw comment: {raw_text}
Extracted description: {description}

Your job:
1. Read the file at the given path and examine the surrounding context (at least 40 lines
   around line {line_number}) to understand exactly what the TODO requires.
2. Check whether the described functionality already exists elsewhere in the codebase or
   whether there are unresolved prerequisites that would block an automated agent.
3. Score feasibility: how much does this TODO make sense to tackle right now, automatically?
4. Score difficulty: how hard would it be for a skilled coding agent to implement it
   correctly (including tests passing, no regressions)?

Output ONLY a valid JSON object — no markdown fences, no preamble.
Example:
{{"feasibility_score": 7.5, "difficulty_score": 4.0, "note": "The cache eviction path is absent and clearly needed; the fix is localised to one file so difficulty is moderate."}}"""


# ─────────────────────────────────────────────────────────────────────────────
# PLANNER AGENT
# ─────────────────────────────────────────────────────────────────────────────

def planner_plan_task(title: str, description: str, file_path: str,
                      line_number: int, repo_path: str) -> str:
    """Prompt for analyzing a single task and producing an implementation plan."""
    return f"""You are a planning agent. Analyze the following task and create a \
concise implementation plan. Output ONLY the plan as a numbered list of steps.

Task: {title}
Description: {description}
File: {file_path}:{line_number}
Repository: {repo_path}

Requirements:
1. Identify which files need to be modified
2. Describe the specific changes needed
3. Note any potential risks or dependencies
4. Keep the plan actionable and specific

Directly output your plan, including a clear and explicit statement of the overall objectives and implementation strategies. The specific implementation plan must be listed clearly item by item."""


def planner_analyze_and_split(title: str, description: str, repo_path: str) -> str:
    """Unified prompt: assess complexity, decide whether to split, produce plan or sub-tasks.

    Output JSON keys:
      complexity   str   one of: very_complex / complex / medium / simple
      split        bool  whether to decompose into sub-tasks
      reason       str   one-sentence justification for both decisions
      plan         str   (only when split=false) numbered implementation steps
      sub_tasks    list  (only when split=true) [{title, description, priority, depends_on}, ...]
    """
    return f"""You are a planning agent. Analyze the following task and produce a structured plan.

Task title: {title}
Task description: {description}
Repository: {repo_path}

Step 1 — Assess complexity. Choose ONE label:
  very_complex : Requires deep understanding of multiple subsystems, likely touches >10 files,
                 high risk of breaking existing behaviour, needs the most capable model.
  complex      : Touches several modules or requires careful design, moderate risk.
  medium       : Clear scope, a few files, straightforward logic.
  simple       : Trivial fix or one-liner, low risk, small model is sufficient.

Step 2 — Decide splitting. Split into sub-tasks ONLY if:
  - The task clearly contains multiple separable concerns in different modules/files AND
  - Parallelising (where possible) would save meaningful time.
  Prefer NOT splitting. The tasks of making minor modifications to several files should not be split up; what should be split are large tasks, with each subtask being sufficiently complete and substantial.

Step 3 — For each sub-task, determine ordering dependencies:
  - Sub-tasks that can run in parallel have an empty depends_on list.
  - If sub-task B must wait for sub-task A to complete first, add A's 0-based index to B's depends_on.
  - Only add a dependency when there is a real reason (e.g. B modifies an interface that A defines).
  - Prefer parallelism: only add dependencies that are strictly required.

Step 4 - Clarify the complete purpose and description of each subtask.
  - The description of each subtask must fully and measurably specify the output it needs to present.
  - Sub-tasks are not isolated; it is necessary to explain their role in the entire task.
  - The description should be suitable for an agent to understand what needs to be done and why.

Step 5 — Produce the output. Output ONLY valid JSON (no markdown fences).

If single task:
{{"complexity": "medium", "split": false, "reason": "...", "plan": "Overall objective: ...\\n1. ...\\n2. ..."}}

If split:
{{"complexity": "complex", "split": true, "reason": "...", "sub_tasks": [
  {{"title": "Define new interface in module A", "description": "...", "priority": "high", "depends_on": []}},
  {{"title": "Migrate callers to new interface", "description": "...", "priority": "medium", "depends_on": [0, 2, 3]}}
]}}"""


def planner_decompose_task(description: str, repo_path: str) -> str:
    """Prompt for breaking a complex task into parallel sub-tasks."""
    return f"""You are a planning agent. Break down the following complex task into \
independent sub-tasks that can be worked on in parallel.

Task: {description}
Repository: {repo_path}

Output a JSON array of sub-tasks. Each sub-task should have:
- "title": short title
- "description": detailed description including which files to modify
- "priority": "high", "medium", or "low"

Output ONLY valid JSON, no other text. Example:
[{{"title": "Fix X", "description": "Modify file Y to ...", "priority": "medium"}}]"""


# ─────────────────────────────────────────────────────────────────────────────
# CODER AGENT
# ─────────────────────────────────────────────────────────────────────────────

def coder_implement(title: str, description: str, file_path: str,
                    line_number: int, plan_output: str,
                    dep_context: str = "") -> str:
    """Prompt for implementing a task in the worktree."""
    parts = [
        "You are a coding agent. Implement the following task completely. You must first read the AGENTS.md within the project to understand the relevant specifications and strictly enforce them.",
        "",
        f"## Task: {title}",
        "",
        "## Description",
        description,
        "",
        "## Delivery Requirements",
        "The set of modifications for this task needs to form a correct git commit, so that it can directly be used as a qualified Pull Request. The commit should not include irrelevant changes such as environment setup.",
        "",
    ]
    if dep_context:
        parts += [
            "",
            dep_context,
        ]
    if file_path:
        parts += [
            "",
            "## Target File",
            f"{file_path}:{line_number}",
        ]
    if plan_output:
        parts += [
            "",
            "## Implementation Plan",
            plan_output,
        ]
    parts += [
        "",
        "## Requirements",
        "1. Make the minimal necessary changes to resolve this task",
        "2. Follow existing code style and conventions",
        "3. You must actually execute compilation and testing (if any) to verify the correctness of the code",
        "4. Do not introduce new TODOs",
        "5. Use ONLY relative file paths (never absolute paths)",
        "6. Ensure the commit content is correct, the final commit(s) submitted by you will be reviewed by the reviewer",
    ]
    return "\n".join(parts)


def coder_retry_feedback(review_feedback: str, attempt: int) -> str:
    """Concise prompt for continued coder sessions — only sends the review
    feedback since the session already has full task context."""
    return (
        f"## Review Feedback (attempt {attempt})\n"
        f"{review_feedback}\n\n"
        "Please confirm whether the issues/optimization suggestions mentioned in the review are present/feasible, and if there are no issues, modify the code according to the suggestions."
        "You still need to follow the instructions in AGENTS.md, but the environment part should already be ready as you just used it. Make sure the tests pass after the modifications and that the code is organized to be basically the clearest."
    )


# ─────────────────────────────────────────────────────────────────────────────
# REVIEWER AGENT
# ─────────────────────────────────────────────────────────────────────────────
REVIEW_REQUIREMENTS="""
## Instructions
The coding agent has already committed its changes to this repository's git history. You should only focus on the content of the commits; the content in the working area that has not been committed is NOT part of the submission and does not require review.
Use the available tools to inspect the work:
  - Run `git log --oneline -5` to see recent commits.
  - Run `git show HEAD` (may with appropriate offset) to view the content of the commits. DON'T use `git diff` because it shows the diff between the working area.
  - Read any modified files to check correctness and style.

When reviewing, you must strictly follow AGENTS.md and the related skills. In addition, you can perform any desired review operations to observe suspicious code and details in order to identify issues as much as possible.

## Output Format
Start your review with either APPROVE or REQUEST_CHANGES on the first line. nothing else in this line.
Then provide specific feedback. Review comments should concisely point out the issues and provide relevant examples or context to explain the current problem.
"""


def reviewer_review(
    title: str,
    description: str,
    revision_context: str = "",
    prior_rejections: str = "",
    coder_response: str = "",
) -> str:
    """Prompt for reviewing code changes produced by the coder agent.

    The reviewer runs as a full opencode agent inside the worktree where the
    coder has already committed its work.  It is free to use git log, git diff,
    read files, etc. to form its judgement.

    *prior_rejections*: concatenated rejection feedback from previous review
    rounds (after the coder already attempted to address them).  Included so
    the reviewer can verify those issues were resolved, but must not blindly
    trust them.

    *coder_response*: the coder's textual response from the latest coding
    round, which may contain reasoning about design decisions or arguments
    about why certain reviewer suggestions are not applicable.  The reviewer
    should consider these arguments on their merits.
    """
    revision_block = ""
    if revision_context:
        revision_block = (
            f"## Revision Context"
            f"The user provided the following manual feedback to the coder. "
            f"Verify that the coder has addressed it:\n{revision_context}"
        )
    prior_block = ""
    if prior_rejections:
        prior_block = (
            f"## Previous Review Rejections (for reference only)\n"
            f"The following issues were raised by reviewers in earlier round(s). "
            f"The coder has since made further changes, so these complaints may "
            f"already be resolved — or may have been incorrect in the first place. "
            f"Use them as hints to guide your inspection, but reach your own "
            f"independent conclusion.\n\n"
            f"{prior_rejections}\n"
        )
    coder_block = ""
    if coder_response:
        coder_block = (
            f"## Coder's Response (from latest round)\n"
            f"The coding agent provided the following explanation alongside its "
            f"changes. Consider these arguments on their merits — the coder may "
            f"have valid reasons for certain design choices, or may be mistaken. "
            f"Evaluate the actual code, not just the coder's claims.\n\n"
            f"{coder_response}\n"
        )
    return f"""You are a code review agent.

## Task that was implemented
Title: {title}
Description: {description}

{revision_block}

{coder_block}
{prior_block}
{REVIEW_REQUIREMENTS}
"""


def reviewer_review_patch(title: str, review_input: str,
                          revision_context: str = "") -> str:
    """Prompt for reviewing a user-supplied patch, PR link, or code snippet.

    Unlike reviewer_review(), this is used for review-only tasks where no
    coder agent was involved.  The reviewer should fetch / read the provided
    material and produce a thorough code review.
    """
    revision_block = ""
    if revision_context:
        revision_block = f"""## Additional Review Instructions (from user)
The user has provided additional review guidance. Pay special attention to these points:
{revision_context}
"""
    return f"""You are a code review agent.

## Review Request
Title: {title}
{revision_block}
## Material to Review
{review_input}

## Instructions
The user has provided the above material for you to review. It may be:
  - A patch / diff pasted inline
  - A GitHub PR or commit URL (use `curl` or the available tools to fetch it)
  - A description of changes with file references
Note that the content of this PR may not be present in the current codebase, so the newly added content cannot be found locally. Please combine the codebase and the actual content of the patch/PR for review.

Depending on the material type:
  - For inline patches/diffs: analyze the diff directly.
  - For GitHub URLs: fetch the diff with `curl -sL <url>.diff` (append .diff to PR URLs) and review it.
  - For file references: read the files in the repository to understand the changes.
  - You can also use `git log`, `git show`, `git diff` etc. as needed.

{REVIEW_REQUIREMENTS}
"""


# ─────────────────────────────────────────────────────────────────────────────
# EXPLORER AGENT  (autonomous code quality exploration)
# ─────────────────────────────────────────────────────────────────────────────

EXPLORER_PERSONALITIES = {
    "perf_hunter": {
        "name": "Performance Hunter",
        "category": "performance",
        "focus": "performance bottlenecks, unnecessary copies, O(n²) algorithms, "
                 "hot path inefficiencies, missing caching opportunities",
        "model_preference": "very_complex",
    },
    "concurrency_auditor": {
        "name": "Concurrency Auditor",
        "category": "concurrency",
        "focus": "race conditions, deadlocks, missing locks, unsafe shared state, "
                 "lock ordering violations, atomic operation misuse",
        "model_preference": "very_complex",
    },
    "maintainability_critic": {
        "name": "Maintainability Critic",
        "category": "maintainability",
        "focus": "code smells, overly complex functions (>50 lines), god classes, "
                 "poor naming, missing abstractions, copy-paste duplication",
        "model_preference": "very_complex",
    },
    "error_handling_inspector": {
        "name": "Error Handling Inspector",
        "category": "error_handling",
        "focus": "unchecked return values, swallowed exceptions, missing error paths, "
                 "resource leaks on error, inconsistent error reporting",
        "model_preference": "very_complex",
    },
    "security_scout": {
        "name": "Security Scout",
        "category": "security",
        "focus": "injection vulnerabilities, unsafe deserialization, hardcoded secrets, "
                 "path traversal, buffer overflows, privilege escalation",
        "model_preference": "very_complex",
    },
}

DEFAULT_EXPLORE_CATEGORIES = [
    "performance", "concurrency", "error_handling", "maintainability", "security",
]


def explorer_prompt(
    module_name: str,
    module_path: str,
    module_description: str,
    category: str,
    personality_name: str,
    personality_focus: str,
    repo_path: str,
) -> str:
    """Prompt for a single exploration run on one module x one category."""
    return f"""You are a code exploration agent — a **{personality_name}**.

## Assignment
Explore the module "{module_name}" located at `{module_path}/` in the repository
at `{repo_path}`.

Module description: {module_description}

## Your Focus
You are specifically looking for **{category}** issues. Focus on:
{personality_focus}

## Instructions
1. List the files in `{module_path}/` and understand the module structure
2. Read key files — focus on implementation files, not just headers
3. Trace important code paths relevant to your focus area
4. For each issue found, provide:
   - severity: "critical" / "major" / "minor" / "info"
   - title: one-line summary
   - description: detailed explanation with evidence
   - file_path: relative path to the affected file
   - line_number: approximate line (0 if not specific)
   - suggested_fix: brief description of what should change
5. If you find NO issues, that's fine — say so explicitly

## Output Format
Output ONLY valid JSON (no markdown fences):
{{"summary": "One paragraph summarizing what you explored and found",
  "findings": [
    {{"severity": "major",
      "title": "Potential race condition in FooManager::update()",
      "description": "The method reads shared_map_ without holding mu_...",
      "file_path": "be/src/vec/exec/foo_manager.cpp",
      "line_number": 142,
      "suggested_fix": "Hold mu_ for the entire read-modify-write sequence"}}
  ]}}

If no issues found:
{{"summary": "Explored N files in module {module_name}, no {category} issues identified.", "findings": []}}
"""


def map_init_prompt(repo_path: str, max_depth: int = 2) -> str:
    """Prompt for analyzing the repo and producing a hierarchical module map."""
    return f"""You are a code architecture analysis agent.

## Task
Analyze the repository at `{repo_path}` and produce a hierarchical module map.

## Instructions
1. List the top-level directories
2. For each significant directory, explore its subdirectories (up to depth {max_depth})
3. Skip vendor, build output, test fixtures, and generated code directories
4. For each module, provide:
   - name: human-readable name
   - path: relative directory path
   - description: 1-2 sentence summary of what this module does
   - children: nested modules (same structure)

## Output Format
Output ONLY valid JSON (no markdown fences):
{{"modules": [
    {{"name": "Query Execution Engine",
      "path": "be/src/vec/exec",
      "description": "Vectorized query execution operators and expression evaluation",
      "children": [
        {{"name": "Aggregate Operators",
          "path": "be/src/vec/exec/agg",
          "description": "Hash and streaming aggregation implementations",
          "children": []}}
      ]}}
  ]}}
"""