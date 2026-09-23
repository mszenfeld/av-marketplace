---
description: "Execute a delivery plan: routed implementer per task, per-task review with up to 3 fix rounds, one commit per task, then the full code review."
argument-hint: "<plan path>"
---

# Delivery: execute

Plan: **$ARGUMENTS**

Run the steps in order. Every `stop` prints its message and ends the command.

## 1. Preflight

1. `read skill://delivery:orchestration` and run its **Plugin roots** section. Keep the `code-review` root.
2. `REPO=$(git rev-parse --show-toplevel)`. `PLAN_PATH` is the plan path made relative to `REPO`.
3. If `git status --porcelain` prints anything → stop with `Commit or stash your changes first.`
4. `BRANCH=$(git branch --show-current)`:
   - empty (detached HEAD) → stop with `Check out a branch first.`;
   - `main` or `master` → `STEM` is the plan file name without `.md` and without a leading `YYYY-MM-DD-`. If `git rev-parse --verify --quiet refs/heads/delivery/<STEM>` succeeds, `git switch delivery/<STEM>`; otherwise `git switch -c delivery/<STEM>`. `BRANCH` becomes `delivery/<STEM>`.
5. `TASKS` = JSON output of `python3 "$ROUTER" plan "$REPO" "$PLAN_PATH"`. A non-zero exit → stop with the router's error.
6. **Done tasks and base.**
   - `git log -F --grep="Delivery-Plan: $PLAN_PATH" --format=%B`. Every line `Delivery-Task: <N>` marks task `N` as done.
   - `BASE`: if `git log -F --grep="Delivery-Plan: $PLAN_PATH" --reverse --format=%H` prints commits, `BASE` is the parent of the first one (`git rev-parse <first>^`); otherwise `BASE=$(git rev-parse HEAD)`.
7. Any not-done task with stack `split` → stop with `Task <N> touches several stacks (<groups>). Split it, or run /delivery:plan again.`
8. Route every not-done task with the skill's **Routing** section. For an `unknown` task, `TASK_TEXT` is its `block`. Print the `Routing: task <N> → <agent> (source: <source>)` line for every task in your reply before starting step 2; it is the audit trail of each routing decision.
9. Every routed agent must be listed among the `task` tool's available agents. A missing one → stop with `Install <plugin>: omp plugin install <plugin>@av-marketplace, then start a new session.` (`<plugin>` is the part before `:`).
10. `todo init` with one item per not-done task: `Task <N>: <title>`.

If every task is already done, print `All tasks of <PLAN_PATH> are delivered.` and go to step 4 (final review).

## 2. Tasks

For each not-done task, in ascending order of `N`:

1. Mark its todo item in progress. Run the skill's **Task loop** with `N`, `TASK_BLOCK` = the task's `block`, `AGENT` = its routed agent, `PLAN_PATH`, `BRANCH`.
2. Act on the result:
   - `stopped` → print the summary (step 3) and end the command;
   - `skipped` → mark the todo item done and note `skipped` in the summary;
   - `approved` or `accepted-with-open-findings` → commit with `git commit -F -` and exactly this message:
     ```
     <the task's commit, or "chore: <title>" when commit is null>

     Delivery-Plan: <PLAN_PATH>
     Delivery-Task: <N>
     ```
     For `accepted-with-open-findings`, add the line `Delivery-Review: accepted-with-open-findings` directly after `Delivery-Task: <N>`. Mark the todo item done.
   - A failed commit (for example a rejecting hook) → print git's output and stop.

## 3. Summary

Print the table `Task | Agent | Routing | Fix rounds | Result`, one row per task handled in this run. The `Routing` column holds the routing source.

## 4. Final review

- `code-review` root is `null` → print `Install code-review@av-marketplace and run /code-review:review.` and end.
- Otherwise `read <code-review root>/commands/review.md` and carry it out completely, as if it had been invoked with this argument in place of its `$ARGUMENTS`:
  ```
  Changes on branch <BRANCH> in <BASE>..HEAD (git diff <BASE>..HEAD), delivered from <PLAN_PATH>. Review only these changes.
  ```

## 5. Fix offer

If the review saved a report, use the `ask` tool: `Run /code-review:fix-all on <report path>?` with options `Yes` and `No`. On `Yes`, `read <code-review root>/commands/fix-all.md` and carry it out completely with `$ARGUMENTS` = the report path.
