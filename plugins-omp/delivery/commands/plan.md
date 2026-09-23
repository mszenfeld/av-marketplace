---
description: "Write a delivery plan with the plan-role model and check that every task routes to one implementer."
argument-hint: "<spec path or feature description>"
---

# Delivery: plan

Input: **$ARGUMENTS**

1. **Setup.**
   - `read skill://delivery:orchestration` and run its **Plugin roots** section.
   - `REPO=$(git rev-parse --show-toplevel)`, `DATE=$(date +%F)`, `PLAN_DIR=docs/superpowers/plans`.
2. **Plan.** Dispatch `delivery:planner` with the `task` tool, one item. Use `context` `Planning a delivery in <REPO>.` and this `task`:
   ```
   Write the implementation plan for:
   $ARGUMENTS

   PLAN_DIR: docs/superpowers/plans
   DATE: <DATE>
   ```
   Wait for the result. Take `plan_path` from its structured output.
3. **Check the routing.** Run `python3 "$ROUTER" plan "$REPO" <plan_path>`.
   - If any task has stack `split` or `unknown`, dispatch `delivery:planner` once more with:
     ```
     Revise <plan_path>: tasks <numbers> must each touch one stack and list their files.
     Router output: <the JSON entries of those tasks, without "block">
     ```
     Then rerun the router.
   - If a task is still `split` or `unknown`, print the routing table from step 4, then `Edit these tasks by hand, then run /delivery:execute <plan_path>`, and stop.
4. **Report.** Print the table `Task | Title | Stack | Agent` and the line `Next: /delivery:execute <plan_path>`.
