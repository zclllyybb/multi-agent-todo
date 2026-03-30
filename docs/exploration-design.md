# Code Exploration System — Detailed Design

## 1. Overview

A new subsystem alongside Tasks and TODOs: autonomous code quality exploration.
An agent generates a hierarchical **project map** of modules, then **explorers**
with different personalities (performance, concurrency, maintainability, …)
autonomously explore unexplored module×category cells, record findings, and
create Tasks for confirmed issues.

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Map Init     │────→│  Explorer     │────→│  Task         │
│  (agent)      │     │  (personality │     │  (existing    │
│               │     │   + assignment)│     │   pipeline)   │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                     writes findings
                            │
                     ┌──────▼───────┐
                     │  Module DB    │
                     │  (map + notes)│
                     └──────────────┘
```

### Lifecycle

1. User clicks **"Initialize Map"** → agent explores repo structure → hierarchical map saved to DB
2. User can manually edit map (rename modules, add/remove children, adjust categories)
3. User clicks **"Start Exploration"** → system picks TODO cells, spawns explorers
4. Explorer runs in repo (read-only, no worktree needed), writes findings back
5. Findings with `issue_found=true` become draft Tasks visible on the Explore page
6. User reviews draft Tasks → clicks **"Dispatch"** → enters existing Task pipeline

---

## 2. Data Models (`core/models.py`)

### 2.1 ExploreModule

Represents one node in the hierarchical project map.

```python
class ExploreStatus(str, enum.Enum):
    TODO = "todo"               # not yet explored for this category
    IN_PROGRESS = "in_progress" # explorer currently running
    DONE = "done"               # explored, findings recorded
    STALE = "stale"             # code changed since last exploration

@dataclass
class ExploreModule:
    id: str              # uuid hex[:12]
    name: str            # e.g. "be/src/vec/exec" or "Query Execution"
    path: str            # directory path relative to repo root, e.g. "be/src/vec/exec"
    parent_id: str       # "" for root modules (top-level)
    depth: int           # 0 = root, 1 = child, etc.
    description: str     # agent-generated module description

    # Per-category exploration status: {"performance": "todo", "concurrency": "done", ...}
    category_status: Dict[str, str]  # key=category name, value=ExploreStatus.value

    # Per-category findings/comments from explorers
    # {"performance": "No issues found", "concurrency": "Potential race in X::foo()"}
    category_notes: Dict[str, str]

    # Module-level metadata from static analysis
    file_count: int
    loc: int               # lines of code
    languages: List[str]   # ["C++", "Python"]

    # Ordering among siblings
    sort_order: int

    created_at: float
    updated_at: float
```

**Key design decisions:**
- `category_status` and `category_notes` are dicts on the module, NOT separate rows. This keeps the model simple and the map easy to serialize.
- The category list is **global** (stored in config), not per-module. Each module has an entry for every category.
- `parent_id` forms the tree. Root modules have `parent_id=""`.

### 2.2 ExploreRun

Records each explorer invocation (analogous to `AgentRun` but for exploration).

```python
@dataclass
class ExploreRun:
    id: str              # uuid hex[:12]
    module_id: str       # which module was explored
    category: str        # which category (e.g. "performance")
    personality: str     # personality key used (e.g. "perf_hunter")
    model: str           # LLM model used
    prompt: str          # full prompt sent
    output: str          # raw agent output
    session_id: str      # opencode session ID

    # Parsed results
    findings: List[dict] # [{severity, title, description, file_path, line_number}, ...]
    summary: str         # agent's summary of exploration
    issue_count: int     # number of issues found

    exit_code: int
    duration_sec: float
    created_at: float
```

### 2.3 TaskSource extension

```python
class TaskSource(str, enum.Enum):
    TODO_SCAN = "todo_scan"
    MANUAL = "manual"
    PLANNER = "planner"
    EXPLORE = "explore"       # ← NEW: created by exploration system
```

Tasks created from exploration findings will have `source=TaskSource.EXPLORE` and
store the originating `module_id` + `category` in the task description.

---

## 3. Database Layer (`core/database.py`)

New table:

```sql
CREATE TABLE IF NOT EXISTS explore_modules (
    id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_explore_modules_parent ON explore_modules(parent_id);

CREATE TABLE IF NOT EXISTS explore_runs (
    id TEXT PRIMARY KEY,
    module_id TEXT NOT NULL,
    category TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_explore_runs_module ON explore_runs(module_id);
```

New methods on `Database`:

```python
# ── ExploreModule CRUD ──
def save_explore_module(self, module: ExploreModule): ...
def get_explore_module(self, module_id: str) -> Optional[ExploreModule]: ...
def get_all_explore_modules(self) -> List[ExploreModule]: ...
def get_child_modules(self, parent_id: str) -> List[ExploreModule]: ...
def delete_explore_module(self, module_id: str): ...

# ── ExploreRun CRUD ──
def save_explore_run(self, run: ExploreRun): ...
def get_explore_runs_for_module(self, module_id: str) -> List[ExploreRun]: ...
```

---

## 4. Explorer Agent (`agents/explorer.py`)

### 4.1 Class

```python
class ExplorerAgent(BaseAgent):
    agent_type: str = "explorer"

    def explore_module(
        self,
        module: ExploreModule,
        category: str,
        personality_prompt: str,
        repo_path: str,
    ) -> Tuple[AgentRun, List[dict], str]:
        """Explore a module for a specific category.
        Returns (agent_run, findings_list, summary_text).
        """
        prompt = explorer_prompt(
            module_name=module.name,
            module_path=module.path,
            module_description=module.description,
            category=category,
            personality=personality_prompt,
            repo_path=repo_path,
        )
        run = self.run(prompt, repo_path)
        text = self.get_text(run)
        findings, summary = self._parse_output(text)
        return run, findings, summary
```

**Runs in the main repo (read-only)** — no worktree needed because explorers
only read code, they don't modify it.

### 4.2 Personalities (preset prompt fragments in `agents/prompts.py`)

```python
EXPLORER_PERSONALITIES = {
    "perf_hunter": {
        "name": "Performance Hunter",
        "focus": "performance bottlenecks, unnecessary copies, O(n²) algorithms, "
                 "hot path inefficiencies, missing caching opportunities",
        "model_preference": "very_complex",  # suggests which model tier to use
    },
    "concurrency_auditor": {
        "name": "Concurrency Auditor",
        "focus": "race conditions, deadlocks, missing locks, unsafe shared state, "
                 "lock ordering violations, atomic operation misuse",
        "model_preference": "very_complex",
    },
    "maintainability_critic": {
        "name": "Maintainability Critic",
        "focus": "code smells, overly complex functions (>50 lines), god classes, "
                 "poor naming, missing abstractions, copy-paste duplication",
        "model_preference": "very_complex",
    },
    "error_handling_inspector": {
        "name": "Error Handling Inspector",
        "focus": "unchecked return values, swallowed exceptions, missing error paths, "
                 "resource leaks on error, inconsistent error reporting",
        "model_preference": "very_complex",
    },
    "security_scout": {
        "name": "Security Scout",
        "focus": "injection vulnerabilities, unsafe deserialization, hardcoded secrets, "
                 "path traversal, buffer overflows, privilege escalation",
        "model_preference": "very_complex",
    },
}
```

Each personality maps to a category:
- `perf_hunter` → category `performance`
- `concurrency_auditor` → category `concurrency`
- etc.

The mapping is explicit in config so users can add custom personalities for
custom categories.

### 4.3 Prompt Template

```python
def explorer_prompt(
    module_name: str,
    module_path: str,
    module_description: str,
    category: str,
    personality: str,
    repo_path: str,
) -> str:
    return f"""You are a code exploration agent — a {personality}.

## Assignment
Explore the module "{module_name}" located at `{module_path}/` in the repository
at `{repo_path}`.

Module description: {module_description}

## Your Focus
You are specifically looking for **{category}** issues. Focus on:
{personality_focus_text}

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
{{
  "summary": "One paragraph summarizing what you explored and found",
  "findings": [
    {{
      "severity": "major",
      "title": "Potential race condition in FooManager::update()",
      "description": "The method reads shared_map_ without holding mu_...",
      "file_path": "be/src/vec/exec/foo_manager.cpp",
      "line_number": 142,
      "suggested_fix": "Hold mu_ for the entire read-modify-write sequence"
    }}
  ]
}}

If no issues found:
{{"summary": "Explored X files in module Y, no {category} issues identified.", "findings": []}}
"""
```

---

## 5. Map Initialization Agent

A separate prompt (NOT a personality — runs once) that scans the repo and
produces the initial hierarchical module map.

### 5.1 Prompt (`agents/prompts.py`)

```python
def map_init_prompt(repo_path: str, max_depth: int = 2) -> str:
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
{{
  "modules": [
    {{
      "name": "Query Execution Engine",
      "path": "be/src/vec/exec",
      "description": "Vectorized query execution operators and expression evaluation",
      "children": [
        {{
          "name": "Aggregate Operators",
          "path": "be/src/vec/exec/agg",
          "description": "Hash and streaming aggregation implementations",
          "children": []
        }}
      ]
    }}
  ]
}}
"""
```

### 5.2 Orchestrator method

```python
def init_explore_map(self) -> dict:
    """Run the map initialization agent and persist results."""
    prompt = map_init_prompt(repo_path)
    run = self.planner.run(prompt, repo_path)  # reuse planner agent for this
    text = self.planner.get_text(run)
    modules_data = json.loads(extract_json(text))

    # Recursively create ExploreModule objects
    categories = self.config.get("explore", {}).get("categories", DEFAULT_CATEGORIES)

    def _create_modules(items, parent_id="", depth=0):
        created = []
        for i, item in enumerate(items):
            mod = ExploreModule(
                name=item["name"],
                path=item["path"],
                parent_id=parent_id,
                depth=depth,
                description=item.get("description", ""),
                category_status={cat: "todo" for cat in categories},
                category_notes={cat: "" for cat in categories},
                file_count=0, loc=0, languages=[],
                sort_order=i,
            )
            self.db.save_explore_module(mod)
            created.append(mod)
            # Recurse into children
            children = item.get("children", [])
            if children:
                created.extend(_create_modules(children, mod.id, depth + 1))
        return created

    all_modules = _create_modules(modules_data["modules"])
    self.db.save_agent_run(run)
    return {"modules_created": len(all_modules)}
```

---

## 6. Exploration Scheduler (in `Orchestrator`)

### 6.1 Config

```yaml
# config.yaml additions
explore:
  # Categories to explore
  categories:
    - performance
    - concurrency
    - error_handling
    - maintainability
    - security
  # Max concurrent exploration agents
  max_parallel_explorers: 2
  # Model to use for exploration (or map from personality.model_preference)
  explorer_model: github-copilot/claude-sonnet-4.6
  # Model for map initialization
  map_model: github-copilot/claude-sonnet-4.6
```

### 6.2 Scheduling logic

```python
def start_exploration(self, module_ids: List[str] = None,
                      categories: List[str] = None,
                      personality_keys: List[str] = None) -> dict:
    """Start exploration on selected modules × categories.

    If module_ids is empty, picks all leaf modules with TODO cells.
    If categories is empty, uses all configured categories.
    If personality_keys is empty, randomly selects from available personalities.
    Returns {"started": N} with number of exploration runs queued.
    """
    all_modules = self.db.get_all_explore_modules()
    if module_ids:
        modules = [m for m in all_modules if m.id in module_ids]
    else:
        # Pick leaf modules (no children) that have TODO cells
        child_parent_ids = {m.parent_id for m in all_modules}
        modules = [m for m in all_modules if m.id not in child_parent_ids]

    cats = categories or self.config["explore"]["categories"]
    started = 0

    for mod in modules:
        for cat in cats:
            if mod.category_status.get(cat) != ExploreStatus.TODO.value:
                continue
            # Mark as in_progress
            mod.category_status[cat] = ExploreStatus.IN_PROGRESS.value
            mod.updated_at = time.time()
            self.db.save_explore_module(mod)
            # Pick personality for this category
            personality_key = self._pick_personality(cat, personality_keys)
            # Submit to thread pool
            self._pool.submit(
                self._run_exploration, mod.id, cat, personality_key
            )
            started += 1

    return {"started": started}


def _pick_personality(self, category: str, allowed_keys=None) -> str:
    """Select a personality that matches the given category."""
    from agents.prompts import EXPLORER_PERSONALITIES
    candidates = []
    for key, info in EXPLORER_PERSONALITIES.items():
        if allowed_keys and key not in allowed_keys:
            continue
        # Match by category name appearing in the key or explicit mapping
        if category in key or category in info.get("focus", ""):
            candidates.append(key)
    if not candidates:
        # Fallback: pick any personality
        candidates = list(EXPLORER_PERSONALITIES.keys())
    return random.choice(candidates)


def _run_exploration(self, module_id: str, category: str,
                     personality_key: str):
    """Execute a single exploration run (called in thread pool)."""
    try:
        module = self.db.get_explore_module(module_id)
        personality = EXPLORER_PERSONALITIES[personality_key]
        repo_path = self.config["repo"]["path"]
        model = self.config["explore"].get("explorer_model", "")

        explorer = ExplorerAgent(model=model, client=self.client)
        run, findings, summary = explorer.explore_module(
            module=module,
            category=category,
            personality_prompt=personality["focus"],
            repo_path=repo_path,
        )

        # Save the exploration run
        explore_run = ExploreRun(
            module_id=module_id,
            category=category,
            personality=personality_key,
            model=model,
            prompt=run.prompt,
            output=run.output,
            session_id=run.session_id,
            findings=findings,
            summary=summary,
            issue_count=len(findings),
            exit_code=run.exit_code,
            duration_sec=run.duration_sec,
        )
        self.db.save_explore_run(explore_run)

        # Update module status and notes
        module = self.db.get_explore_module(module_id)  # re-read
        module.category_status[category] = ExploreStatus.DONE.value
        module.category_notes[category] = summary
        module.updated_at = time.time()
        self.db.save_explore_module(module)

        # Auto-create draft tasks for findings with severity >= major
        for finding in findings:
            if finding.get("severity") in ("critical", "major"):
                self._create_explore_task(module, category, finding)

        log.info("Exploration complete: module=%s category=%s findings=%d",
                 module.name, category, len(findings))

    except Exception as e:
        log.error("Exploration failed: module=%s category=%s: %s",
                  module_id, category, e)
        module = self.db.get_explore_module(module_id)
        if module:
            module.category_status[category] = ExploreStatus.TODO.value
            module.updated_at = time.time()
            self.db.save_explore_module(module)


def _create_explore_task(self, module: ExploreModule, category: str,
                         finding: dict):
    """Create a pending Task from an exploration finding."""
    task = Task(
        title=f"[Explore/{category}] {finding['title']}",
        description=(
            f"**Found by exploration** in module `{module.name}` ({module.path})\n"
            f"**Category**: {category}\n"
            f"**Severity**: {finding['severity']}\n\n"
            f"{finding['description']}\n\n"
            f"**Suggested fix**: {finding.get('suggested_fix', 'N/A')}"
        ),
        priority=TaskPriority.HIGH if finding["severity"] == "critical"
                 else TaskPriority.MEDIUM,
        source=TaskSource.EXPLORE,
        file_path=finding.get("file_path", ""),
        line_number=finding.get("line_number", 0),
    )
    self.db.save_task(task)
    log.info("Created explore task [%s]: %s", task.id, task.title)
```

---

## 7. Web API (`web/app.py`)

### 7.1 Endpoints

```
GET  /api/explore/modules          → list all modules (tree structure)
GET  /api/explore/modules/{id}     → module detail + runs
POST /api/explore/init-map         → trigger map initialization
POST /api/explore/modules          → manually add/edit a module
DEL  /api/explore/modules/{id}     → delete a module
POST /api/explore/start            → start exploration {module_ids?, categories?, personalities?}
POST /api/explore/stop             → stop all running explorations
GET  /api/explore/runs             → list recent exploration runs
GET  /api/explore/runs/{id}        → run detail with parsed output
POST /api/explore/findings/{id}/create-task → create Task from a specific finding
```

### 7.2 Key implementations

```python
@app.get("/api/explore/modules")
async def api_explore_modules():
    """Return all modules as a flat list (frontend builds tree from parent_id)."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    modules = orchestrator.db.get_all_explore_modules()
    return [m.to_dict() for m in modules]


@app.post("/api/explore/init-map")
async def api_init_explore_map():
    """Trigger map initialization (long-running, returns immediately)."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    # Run in background
    orchestrator._pool.submit(orchestrator.init_explore_map)
    return {"status": "initializing"}


@app.post("/api/explore/start")
async def api_start_exploration(request: Request):
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    result = orchestrator.start_exploration(
        module_ids=body.get("module_ids"),
        categories=body.get("categories"),
        personality_keys=body.get("personalities"),
    )
    return result


@app.post("/api/explore/modules/{module_id}")
async def api_update_module(module_id: str, request: Request):
    """Edit module name, description, or manually reset a category status."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    module = orchestrator.db.get_explore_module(module_id)
    if not module:
        return JSONResponse({"error": "Module not found"}, status_code=404)
    body = await request.json()
    if "name" in body:
        module.name = body["name"]
    if "description" in body:
        module.description = body["description"]
    if "category_status" in body:
        for cat, status in body["category_status"].items():
            module.category_status[cat] = status
    if "category_notes" in body:
        for cat, note in body["category_notes"].items():
            module.category_notes[cat] = note
    module.updated_at = time.time()
    orchestrator.db.save_explore_module(module)
    return module.to_dict()
```

---

## 8. Frontend Page

A new **"Explore"** tab in the dashboard, alongside Tasks and TODOs.

### 8.1 Layout

```
┌─────────────────────────────────────────────────────────┐
│  [Tasks] [TODOs] [Explore]                   [Config]   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─ Map Controls ─────────────────────────────────────┐ │
│  │ [Initialize Map] [Start Exploration ▼] [Stop]      │ │
│  │ Categories: ☑perf ☑concurrency ☑error ☑maint ☑sec  │ │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  ┌─ Module Tree ──────────────────────────────────────┐ │
│  │                                                    │ │
│  │  ▼ be/src/vec/exec  (Query Execution)              │ │
│  │    │  perf:🟢  conc:🟡  err:⬜  maint:⬜  sec:⬜   │ │
│  │    ├─ agg/  (Aggregation)                          │ │
│  │    │    perf:🟢  conc:🟢  err:⬜  maint:⬜  sec:⬜ │ │
│  │    └─ join/ (Join Operators)                       │ │
│  │         perf:⬜  conc:⬜  err:⬜  maint:⬜  sec:⬜ │ │
│  │                                                    │ │
│  │  ▼ be/src/olap  (OLAP Engine)                      │ │
│  │    ...                                             │ │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  ┌─ Selected Module Detail ───────────────────────────┐ │
│  │  Module: be/src/vec/exec/agg                       │ │
│  │  Description: Hash and streaming aggregation...    │ │
│  │                                                    │ │
│  │  Category: performance  Status: DONE 🟢            │ │
│  │  Notes: "Explored 12 files. Found 2 issues..."     │ │
│  │  [View Run Detail]                                 │ │
│  │                                                    │ │
│  │  Category: concurrency  Status: DONE 🟢            │ │
│  │  Notes: "No concurrency issues found."             │ │
│  │                                                    │ │
│  │  Findings:                                         │ │
│  │  ┌──────────────────────────────────────────────┐  │ │
│  │  │ 🔴 MAJOR: Race in AggHashMap::merge()       │  │ │
│  │  │ file: agg/hash_agg.cpp:342                   │  │ │
│  │  │ [Create Task] [Dismiss]                      │  │ │
│  │  └──────────────────────────────────────────────┘  │ │
│  │  ┌──────────────────────────────────────────────┐  │ │
│  │  │ 🟡 MINOR: Unused variable in streaming_agg  │  │ │
│  │  │ file: agg/streaming_agg.cpp:89               │  │ │
│  │  │ [Create Task] [Dismiss]                      │  │ │
│  │  └──────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### 8.2 Status icons

| Status | Icon | Color |
|--------|------|-------|
| TODO | ⬜ | gray |
| IN_PROGRESS | 🔵 | blue/spinning |
| DONE (no issues) | 🟢 | green |
| DONE (has issues) | 🟡 | yellow |
| STALE | 🟠 | orange |

### 8.3 Auto-refresh

The page polls `GET /api/explore/modules` every 3 seconds (same pattern as
the Tasks page) to show real-time progress as explorers complete.

---

## 9. File Layout (new/modified files)

```
core/models.py              # + ExploreModule, ExploreRun, ExploreStatus, TaskSource.EXPLORE
core/database.py            # + explore_modules table, explore_runs table, CRUD methods
core/orchestrator.py        # + init_explore_map, start_exploration, _run_exploration, etc.
agents/explorer.py          # NEW: ExplorerAgent class
agents/prompts.py           # + EXPLORER_PERSONALITIES, explorer_prompt(), map_init_prompt()
web/app.py                  # + /api/explore/* endpoints, Explore page HTML/JS
config.yaml                 # + explore: section
tests/test_explore.py       # NEW: tests for models, DB, scheduling, prompt parsing
```

---

## 10. Execution Flow (detailed)

### 10.1 Map Initialization

```
User clicks "Initialize Map"
  → POST /api/explore/init-map
  → orchestrator.init_explore_map() in thread pool
    → planner agent runs map_init_prompt against repo
    → agent outputs JSON with module tree
    → parse JSON, recursively create ExploreModule rows
    → each module gets category_status = {cat: "todo" for cat in categories}
    → save to DB
  → frontend polls GET /api/explore/modules, tree appears
```

### 10.2 Exploration

```
User clicks "Start Exploration" (optionally selects modules/categories)
  → POST /api/explore/start {module_ids, categories}
  → orchestrator.start_exploration()
    → for each (module, category) where status == TODO:
      → set status = IN_PROGRESS, save
      → pick personality for category
      → submit _run_exploration to thread pool
    → return {"started": N}

_run_exploration(module_id, category, personality_key):
  → create ExplorerAgent with model from config
  → build prompt from personality + module info
  → run agent in repo_path (read-only)
  → parse JSON output → findings + summary
  → save ExploreRun to DB
  → update module: status=DONE, notes=summary
  → for each critical/major finding:
    → create Task(source=EXPLORE, status=PENDING)
  → frontend auto-refreshes, sees progress
```

### 10.3 Task Dispatch

```
User sees finding on Explore page → clicks "Create Task"
  → POST /api/explore/findings/{run_id}/create-task {finding_index}
  → creates Task with source=EXPLORE
  → Task appears on Tasks page

User clicks "Dispatch" on Tasks page (existing flow)
  → normal plan → code → review pipeline
```

---

## 11. Key Design Constraints

1. **Explorers are read-only** — they run in the main repo, never modify files, no worktree needed
2. **No automatic dispatch** — tasks created from findings are always PENDING; user decides
3. **Map is editable** — user can rename modules, add children, reset status to TODO for re-exploration
4. **Categories are configurable** — defined in config.yaml, can be extended at any time (new category = all modules get TODO for it)
5. **Findings are immutable** — stored in ExploreRun, never modified; tasks are created as copies
6. **Thread pool shared** — exploration runs share the same `_pool` as task execution, respecting `max_parallel_tasks`

---

## 12. Config Additions

```yaml
# Exploration settings
explore:
  categories:
    - performance
    - concurrency
    - error_handling
    - maintainability
    - security
  max_parallel_explorers: 2
  explorer_model: github-copilot/claude-sonnet-4.6
  map_model: github-copilot/claude-sonnet-4.6
  # Auto-create tasks for findings at or above this severity
  auto_task_severity: major  # critical / major / minor / info
```
