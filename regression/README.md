# Regression Framework

This directory contains the real black-box regression framework for `OpenGiraffe`.

Properties:

- real models
- real daemon process
- real HTTP API calls
- real git repo and worktrees
- isolated temporary SQLite database
- isolated temporary config, logs, port, and PID file
- all runtime artifacts created under `regression/.runtime/` inside this repo

The tests here must not rely on internal orchestrator method calls to drive the product.
They should start the real daemon and interact with it through public interfaces only.

## Current Coverage

The current regression suite contains 12 real black-box end-to-end cases:

| Test file | Primary flow | Public entrypoints exercised | Core assertions | Coverage notes |
| --- | --- | --- | --- | --- |
| `test_regression_develop.py` | Normal develop task completes end-to-end | `POST /api/tasks`, `GET /api/tasks/{id}` | planner/coder/reviewer all ran; branch and worktree created; code and test changed; submission committed; worktree clean; `python -m pytest -q` passes | Covers the main develop happy path from task creation through completed submission. Does not cover split, revise, resume, cancellation, or arbitration. |
| `test_regression_review_only.py` | Review-only task executes the reviewer-only pipeline | `POST /api/tasks/review`, `GET /api/tasks/{id}` | review-only task reaches `COMPLETED`; task mode is `review`; reviewer run exists; planner/coder runs do not exist; review output is persisted; temporary review worktree is cleaned | Covers the dedicated review-only public workflow that the refactor audit calls out as a separate execution path. |
| `test_regression_task_revise.py` | Completed task is revised via manual feedback | `POST /api/tasks`, `POST /api/tasks/{id}/revise`, `GET /api/tasks/{id}` | initial task completes; manual review run recorded; coder/reviewer rerun; revised task completes; revised worktree contains module docstring update and multiply-by-zero test; worktree clean; tests pass | Covers completed -> revise -> coder/reviewer rerun semantics and public revise API. Does not prove failed-task resume behavior. |
| `test_regression_task_resume.py` | Failed task is resumed from the prior coder session | `POST /api/tasks`, `POST /api/tasks/{id}/exec`, `POST /api/tasks/{id}/revise`, `POST /api/tasks/{id}/resume`, `GET /api/tasks/{id}` | initial task completes; task is deliberately broken in worktree; revise fails under forced timeout; task reaches `FAILED`; `/resume` reuses the failed coder session id; task later completes; failing injected test is removed; worktree clean; tests pass | Covers real `FAILED -> /resume` behavior and session reuse. This is the strongest black-box protection for resume semantics. |
| `test_regression_task_split.py` | Planner splits a parent task into dependent child tasks and execution respects dependencies | `POST /api/tasks`, `GET /api/tasks`, `GET /api/tasks/{id}` | parent completes; at least two child tasks created; dependency edge persisted; child tasks all complete; child tasks each ran planner/coder/reviewer; dependent child has branch/worktree; parent plan output records split | Covers planner split JSON, subtask creation, dependency persistence, and dependent execution. Does not yet assert exact execution ordering timestamps. |
| `test_regression_explore.py` | Explore map init and explore run complete end-to-end | `POST /api/explore/init-map`, `POST /api/explore/start`, `GET /api/explore/status`, `GET /api/explore/queue`, `GET /api/explore/runs`, `GET /api/explore/modules/{id}` | map init reaches `done`; map becomes ready; exploration queue drains; at least one run is persisted; module category status becomes `done` or `stale`; run summary exists | Covers basic explore happy path including persisted runs and module status updates. |
| `test_regression_explore_auto_task.py` | User creates a task from an exploration finding through the public API | `POST /api/explore/init-map`, `POST /api/explore/start`, `GET /api/explore/runs`, `GET /api/explore/runs/{id}`, `POST /api/explore/runs/{id}/create-task`, `GET /api/tasks/{id}` | run with findings is persisted; chosen finding can create a task; created task has `source=explore`, expected title/description prefix, and expected file path | Covers manual create-task-from-finding behavior. Does not cover automatic task creation thresholding. |
| `test_regression_explore_auto_task_end_to_end.py` | Exploration automatically creates a task for a major finding | `POST /api/explore/modules`, `POST /api/explore/start`, `GET /api/explore/runs`, `GET /api/tasks`, `GET /api/tasks/{id}` | manual module injection succeeds; explore run with findings is persisted; a new `source=explore` task appears automatically without calling create-task API; task points at the expected file | Covers explore -> auto-task threshold behavior end-to-end. This is the direct black-box guard for automatic task creation. |
| `test_regression_explore_restart_recovery.py` | Explore queue recovers after daemon crash/restart | `POST /api/explore/init-map`, `POST /api/explore/start`, `GET /api/explore/queue`, daemon restart via harness, `GET /api/explore/runs`, `GET /api/explore/modules/{id}` | map init succeeds; queue becomes active; daemon is killed while exploration is running; restart recovers the queued/running work; queue later drains; persisted explore run exists after restart | Covers crash -> restart -> DB-backed explore queue recovery. This is the main black-box protection for explore restart semantics. |
| `test_regression_jira.py` | In-place Jira assignment on an existing task in dry-run mode | `POST /api/tasks`, `POST /api/tasks/{id}/jira`, `GET /api/tasks/{id}` | source task first completes normally; in-place Jira assign succeeds; source task gets `jira_status=created`, synthetic key/url, payload preview, and `jira_assign` agent run; payload contains `DorisExplorer`, expected project, summary prefix, issue type, and priority | Covers the main in-place Jira assignment flow used by the product. It does not cover standalone Jira task submission via `POST /api/tasks/jira`. |
| `test_regression_jira_standalone.py` | Standalone Jira-mode task runs end-to-end in dry-run mode | `POST /api/tasks/jira`, `GET /api/tasks/{id}` | jira-mode task reaches `COMPLETED`; `task_mode=jira`; synthetic key/url/payload are persisted; `jira_assign` run exists; payload contains required label, project, summary prefix, issue type, and priority | Covers the standalone Jira public workflow separately from in-place assign. |
| `test_regression_config_persistence.py` | Runtime model config update survives restart | `GET /api/config`, `POST /api/config`, daemon restart via harness, `GET /api/config` | config update succeeds over public API; planner/coder/reviewer/explorer/map models reflect updated values immediately; after restart using persisted runtime config the values are still present and loadable | Covers the brittle config persistence path called out in the refactor audit. |

## Coverage Gaps

Important public-product flows not yet covered by regression:

| Gap | Public entrypoint | Why it matters |
| --- | --- | --- |
| Arbitration resolution flow | `POST /api/tasks/{id}/arbitrate` | `NEEDS_ARBITRATION` is a real terminal/decision state in the product, but there is no end-to-end regression proving approve/revise/reject behavior through the public API. |
| Jira temp-file hygiene | indirectly exercised by `POST /api/tasks/jira` and `POST /api/tasks/{id}/jira` | Real Jira agent runs appear able to leave temporary description files such as `skills/jira-issue/description.md` behind in the repository root. There is still no regression asserting that Jira temp artifacts are cleaned up. |

Secondary gaps worth considering later:

| Gap | Why it is lower priority |
| --- | --- |
| Task cancellation and cleanup/publish flows | Important operationally, but lower refactor risk than the main execution, explore, and Jira domains. |
| Explore cancellation and map review-required flow | Valuable, but the current refactor risk is more strongly concentrated in normal explore run, auto-task, and restart recovery semantics. |
| Exact dependency execution ordering evidence in split flow | Current split regression already proves dependency persistence and successful dependent execution, which gives decent black-box protection for now. |

## Execute

```bash
python -m pytest regression/ --run-regression -v -s
```

Useful form:

```bash
python -m pytest regression/ --run-regression -v -s \
  --regression-profile stable \
  --regression-keep-artifacts
```

Environment variables:

```bash
REGRESSION_ENABLE=1
REGRESSION_PROFILE=stable
REGRESSION_KEEP_ARTIFACTS=1
REGRESSION_BASE_CONFIG=/path/to/config.yaml
```

By default, each test run creates an isolated workspace under `regression/.runtime/` and
removes it afterward. Use `--regression-keep-artifacts` or `REGRESSION_KEEP_ARTIFACTS=1`
to preserve those directories for inspection.

## Interface Guide

When writing a new regression test, use only these layers:

1. `regression_harness_factory("fixture_name")`
2. Public HTTP-driving helpers on `RegressionHarness`
3. Persisted artifacts for assertions

The main helper methods are:

```python
harness.submit_develop_task(...)
harness.assign_jira_for_task(task_id)
harness.revise_task(task_id, feedback=...)
harness.resume_task(task_id, message=...)
harness.init_explore_map()
harness.start_exploration(...)
harness.get_explore_queue()
harness.get_explore_run_detail(run_id)
harness.create_task_from_finding(run_id, finding_index=0)
harness.exec_in_task_worktree(task_id, command=...)
harness.restart(...)
harness.crash_daemon()
harness.wait_for_task_terminal(task_id)
harness.wait_for_explore_map_terminal()
harness.wait_for_exploration_idle()
harness.get_task_detail(task_id)
harness.list_tasks()
harness.get_explore_status()
harness.list_explore_modules()
harness.get_explore_module_detail(module_id)
harness.get_explore_runs_api()
```

Use database-backed artifact reads only for final verification, not for driving behavior:

```python
harness.get_task_record(task_id)
harness.get_task_runs(task_id)
harness.get_explore_runs()
```

## Writing Rules

1. Start the daemon only through the harness.
2. Trigger product behavior only through HTTP API helpers.
3. Assert on stable public outcomes:
   - task status
   - persisted runs
   - branch/worktree existence
   - file content in generated worktree
   - API payloads and persisted fields
4. Avoid asserting exact model wording.
5. Keep fixture projects tiny and deterministic.
6. If Jira coverage is needed, use the regression dry-run path instead of a real Jira server.

## Model Profiles

Regression model profiles are resolved from the top-level config under:

```yaml
regression:
  default_profile: stable
  dry_run_jira: false
  model_profiles:
    stable:
      planner_model: github-copilot/gpt-5.4
      coder_model_default: github-copilot/gpt-5.4
      reviewer_models:
        - github-copilot/gpt-5.4
      explorer_model: github-copilot/gpt-5.4
      map_model: github-copilot/gpt-5.4
    free:
      planner_model: opencode/qwen3.6-plus-free
      coder_model_default: opencode/qwen3.6-plus-free
      reviewer_models:
        - opencode/qwen3.6-plus-free
      explorer_model: opencode/qwen3.6-plus-free
      map_model: opencode/qwen3.6-plus-free
```

The harness expands the selected profile into a temporary runtime config. Production repo,
worktrees, logs, databases, ports, and PID files are never reused, and regression runs do
not create temp resources outside this repository.
