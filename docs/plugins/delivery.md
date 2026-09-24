# Delivery Plugin (Oh My Pi only)

Delivery runs approved OMP plans task by task, or delivers an existing plan with `/delivery:execute <PLAN_PATH>`. Each task goes to the agent selected from its file list, then receives a review and its own commit.

## Plan format

Write the plan's Approach as numbered `### Task N: <title>` blocks. For example (paths are illustrative):

```markdown
# Catalog search plan

## Approach

### Task 1: Add catalog search
**Commit:** feat: add catalog search

**Files:**
- Create: `src/catalog/search.py`
- Modify: `src/catalog/api.py`
- Test: `tests/test_search.py`
- Delete: `src/catalog/legacy_search.py`

Write a failing search test first, implement the search endpoint, then remove the legacy implementation.

## Verification
- Run the catalog search tests.
```

Number tasks 1, 2, 3… in execution order, each number exactly once; put producers before consumers. Every file change belongs to a task: text outside task blocks is context, not implementation work. List every file the task will create, modify, test, or delete under `**Files:**`, with repository-relative paths in backticks. The file list determines the implementer. Keep one stack per task (Python, React/TypeScript frontend, PHP, or other files such as docs and CI); split work spanning stacks into separate tasks. Each task must stand alone and name any functions, types, or signatures later tasks depend on. Do not put `##` or `###` headings inside a task outside fenced code blocks: the next heading ends that task.

`**Commit:**` is optional; without it, the task commit subject is `chore: <title>`. After the last task, an optional `## Verification` section lists checks Delivery runs in order. A plan with no `### Task` headings runs without Delivery. In plan mode, proposing a plan checks its task format; `/delivery:execute` runs the plan file, so check a hand-written plan against these rules before starting.

## Branch and plan location

On `main` or `master`, Delivery creates a `delivery/<slug>` branch (adding a numeric suffix if that name exists). On any other branch, it stays on that branch. When `/delivery:execute` receives a plan file already in the repository, it keeps that plan at its existing path. An external plan file, or a plan approved in OMP plan mode, is saved to `docs/plans/<date>-<slug>.md` (with a numeric suffix if the destination exists). Delivery commits a new or changed plan before the first task; an unchanged plan already in the repository needs no new plan commit.

## Prerequisites

Run in a git repository on a checked-out branch. At detached HEAD, Delivery stops with `Check out a branch first.` The working tree must have no changes other than the plan itself; commit or stash other changes before starting. Install the plugin for each agent that will receive a task (Python Developer, Frontend Developer, PHP Developer, or Delivery's generic implementer). If an agent is unavailable, Delivery stops and prints the plugin installation command.

## Commit trailers and resuming

Each task commit carries `Delivery-Plan: <PLAN_PATH>`, `Delivery-Task: <N>`, and `Delivery-Task-Title: <title>`. If you choose to accept unresolved review findings, it also carries `Delivery-Review: accepted-with-open-findings`.

To resume, run `/delivery:execute <PLAN_PATH>`. Delivery skips a task only if a commit for that exact plan path contains both its task number and exactly the same task title as the current plan. If a task number matches but the title differs (or the title trailer is missing), Delivery stops and shows the commit, task number, committed title, and plan title rather than silently treating the task as done.

## Review and fix rounds

Every task is reviewed before its commit. Findings marked `critical` or `important` return to the same implementing agent for up to 3 fix rounds, with another review after each round. If blocking findings remain, choose `Accept and commit with open findings` or `Stop delivery`. Accepted open findings add the review trailer above; stopping leaves the delivery unfinished.
