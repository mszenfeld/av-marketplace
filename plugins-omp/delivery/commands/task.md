---
description: "Implement one ad-hoc task with an automatically chosen developer agent and a review; changes stay staged, nothing is committed."
argument-hint: "<task description>"
---

# Delivery: task

Task: **$ARGUMENTS**

1. `read skill://delivery:orchestration` and run its **Plugin roots** section. `REPO=$(git rev-parse --show-toplevel)`.
2. If `git status --porcelain` prints anything → stop with `Commit or stash your changes first.`
3. Route the task with the skill's **Routing** section, as a task with stack `unknown`, `N = 1`, title `ad-hoc` and `TASK_TEXT` = the task above. Print its `Routing: task 1 → <agent> (source: <source>)` line in your reply before continuing.
4. Run the skill's **Task loop** with:
   - `N = 1`;
   - `TASK_BLOCK` = `### Task 1: ad-hoc`, a blank line, then the task above;
   - `AGENT` = the routed agent;
   - `PLAN_PATH = ad-hoc`;
   - `BRANCH` = `git branch --show-current`.
5. Print the `Routing:` line from step 3, the result, the number of fix rounds, any open findings, and `Changes are staged, not committed. Review them, then commit.`
