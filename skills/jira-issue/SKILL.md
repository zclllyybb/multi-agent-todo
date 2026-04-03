---
name: jira-issue
description: Use when 需要通过 Jira REST API 创建 Issue、追加评论，或预览 Jira 请求 payload。
---

# Jira Issue

## Overview

使用当前 skill 目录内脚本访问 Jira REST API v2。命令只依赖 skill 内部结构：`pyproject.toml` 与 `scripts/`。

```bash
uv run --project . python3 scripts/<script>.py ...
```

以下命令都假设当前目录就是 `jira-issue` 这个 skill 目录。默认配置来源按以下优先级生效：
- 命令行参数
- 当前 shell 环境变量
- `~/.env`

`--dry-run` 只打印 payload，不会发请求，也不要求 `JIRA_URL` / `JIRA_TOKEN`。

## Quick Start

1. 确认系统里已经有 `uv`；如果没有，让用户自行按官方文档安装。
2. 如需默认凭据，可写入 `~/.env`；否则直接传 CLI 参数或使用当前 shell 环境变量。
3. Issue 描述和评论内容统一走 `--description-file` / `--comment-file`；传 stdin 时使用 `-`。
4. 先执行 `--dry-run` 预览 payload。
5. 确认无误后去掉 `--dry-run` 正式提交，并记录返回的 `key` / `self` 或 `comment_id` / `self`。

## Prerequisites

- 需要 `uv`
- 如果 `uv` 不在 PATH，提示用户自行安装；本 skill 不提供安装流程

## Create Issue

```bash
uv run --project . python3 scripts/jira_create_issue.py \
  --project-key <PROJECT_KEY> \
  --summary "..." \
  --description-file /path/to/description.md \
  --dry-run
```

常用参数：
- `--auth`：`bearer` 或 `basic`（默认 bearer）
- `--project-key`：也可用 `JIRA_PROJECT`
- `--issue-type`：也可用 `JIRA_ISSUE_TYPE`，默认 Bug
- `--description-file`：从文件读取描述；传 `-` 时从 stdin 读取
- `--label`/`--component`/`--assignee`/`--priority`
- `--affects-version`/`--fix-version`：对应 Affects Version / Fix Version（可重复）
- `--epic`：Epic issue key，创建成功后会将 issue 挂载到该 epic
- `--print-payload`/`--dry-run`

输出：`key` 与 `self`（若返回）。

经验示例：
```bash
uv run --project . python3 scripts/jira_create_issue.py \
  --project-key DORIS \
  --issue-type Bug \
  --summary "回归发版测试 S3 load 因内存超限失败（regression-release #2318/#2314）" \
  --description-file /path/to/description.md \
  --priority Highest \
  --assignee laihui \
  --label 存储小组 \
  --label 导入 \
  --affects-version enter-3.1.4 \
  --fix-version 3.1.4
```

挂载到 Epic 的示例：
```bash
uv run --project . python3 scripts/jira_create_issue.py \
  --project-key DORIS \
  --issue-type Bug \
  --summary "Planner 优化器在特定场景下产生错误执行计划" \
  --description-file /path/to/description.md \
  --label DorisExplorer \
  --epic DORIS-24979 \
  --dry-run
```
创建成功后会输出 `epic_linked=DORIS-24979` 表示已挂载到 epic。

stdin 示例：
```bash
uv run --project . python3 scripts/jira_create_issue.py \
  --project-key QA \
  --issue-type 任务 \
  --summary "..." \
  --description-file - <<'EOF'
第一行

第二行
EOF
```

## Comment Issue

```bash
uv run --project . python3 scripts/jira_comment_issue.py \
  --issue-key DORIS-12345 \
  --comment-file /path/to/comment.md \
  --dry-run
```

常用参数：
- `--issue-key`：目标 Issue key
- `--comment-file`：评论内容文件；传 `-` 时从 stdin 读取
- `--print-payload` / `--dry-run`：预览请求

输出：`comment_id` 与 `self`（若返回）。

## Config

发送请求时需要：
- `JIRA_URL`
- `JIRA_TOKEN`

可选默认值：
- `JIRA_USER`：仅 `--auth basic` 时需要
- `JIRA_PROJECT`
- `JIRA_ISSUE_TYPE`

## Common Mistakes

- `--assignee` 使用 Jira 用户名，不是邮箱，例如 `laihui`
- 多行内容不要试图走内联参数；统一用 `--description-file` / `--comment-file`
- 需要动态内容时，用 `--description-file -` 或 `--comment-file -` 配合 heredoc
- 这些命令默认在 skill 目录内执行；如果不在该目录，先切到 skill 目录再运行
