---
name: "delivery:orchestration"
description: "Internal procedures used by the /delivery commands: plugin roots, routing, the per-task loop."
hide: true
---

# Delivery orchestration

Shared procedures for `/delivery:plan`, `/delivery:execute` and `/delivery:task`. Run only the section a command names. Values in `<ANGLE_BRACKETS>` come from the calling command.

## Plugin roots

Run:

```bash
omp plugin list --json | python3 -c 'import json,sys; want=sys.argv[1:]; d=json.load(sys.stdin); r={p["id"].split("@")[0]: p["entries"][0]["installPath"] for p in d.get("marketplace", []) if p.get("entries")}; print(json.dumps({w: r.get(w) for w in want}))' delivery code-review
```

- `delivery` is `null` → stop with `Install delivery: omp plugin install delivery@av-marketplace`.
- Otherwise `ROUTER` is `<delivery root>/scripts/route_task.py`; use it through `python3 "$ROUTER" ...`.
- Keep the `code-review` root; `null` means code-review is not installed.

The router prints JSON. Each task entry of `python3 "$ROUTER" plan "$REPO" <plan>` has `task`, `title`, `commit`, `block`, `files`, `stack`, `agent`, `groups`.

## Routing

Decide the implementer for each task:

- `stack` is `python`, `frontend`, `php` or `generic` → use the entry's `agent`. Source: `files`.
- `stack` is `split` → do not route. The calling command decides what happens.
- `stack` is `unknown` (the task lists no files):
  1. Run `python3 "$ROUTER" layout "$REPO"` and keep its JSON as `LAYOUT`. If `LAYOUT["stacks"]` is empty → `delivery:implementer`, source `files`.
  2. Otherwise run this in the `eval` tool (python). Set `LAYOUT` to that JSON and `TASK_TEXT` to the task text:
     ```python
     CRIT = {"python": "Python code or its tests.", "frontend": "React/TypeScript web app code or its tests.", "php": "PHP code or its tests."}
     crit = {s: CRIT[s] for s in LAYOUT["stacks"]}
     crit["generic"] = "Work outside those stacks: docs, CI, repo configuration, Node tooling without React."
     crit["split"] = "The task needs changes in two or more of the stacks above."
     Q = {"route": {"type": "choice", "instructions": "Pick the implementer for this task given the repository layout. One task should touch one stack.", "criteria": crit}}
     state = "Repository layout:\n" + "\n".join(LAYOUT["lines"]) + "\n\nTask:\n" + TASK_TEXT
     b = judge_batch({"t": state}, Q, intent="Routing a delivery task")
     got = []
     for _ in range(3):
         got = await b.drain(timeout=60)
         if got:
             break
     model = b.status()["model"]
     item = got[0][1] if got else None
     answer = item.answers["route"] if item is not None and not getattr(item, "error", None) else None
     print({"model": model, "answer": answer, "error": getattr(item, "error", None) if item is not None else "no answer"})
     ```
  3. Accept `answer["choice"]` only when all of these hold:
     - `answer` is not `None`;
     - `"jev" in model.lower()`;
     - `answer["choice"] != "split"`;
     - `answer["confidence"] >= 0.8`.

     The agent is `python` → `python-developer:developer`, `frontend` → `frontend-developer:developer`, `php` → `php-developer:developer`, `generic` → `delivery:implementer`. Source: `jev p=<confidence, 2 decimals> via <model>`.
  4. Otherwise use the `ask` tool:
     - question: `Which implementer should take Task <N>: <title>? (no file list; Jev: <choice, or the error> p=<confidence> via <model>)`;
     - options: one per stack in `LAYOUT["stacks"]`, then `generic`, then `Stop`;
     - `Stop` ends the command. Any other answer maps as in step 3. Source: `user`.

For every routed task, print exactly one line:

```
Routing: task <N> → <agent> (source: <source>)
```

## Task loop

Inputs: `N`, `TASK_BLOCK`, `AGENT`, `PLAN_PATH` (a plan path, or `ad-hoc`), `BRANCH`. `PLUGIN` is the part of `AGENT` before `:` when `AGENT` is a developer agent (`python-developer`, `frontend-developer`, `php-developer`), otherwise `none`.

Every dispatch below is one `task` tool call with one item. Wait for its result before continuing; if it has not arrived, use `hub` `wait` with the job id. Use this `context` for every item:

```
Delivery of <PLAN_PATH> on branch <BRANCH>. One task per agent; the orchestrator reviews and commits.
```

1. `TASK_BASE=$(git rev-parse HEAD)`.
2. **Implement.** Dispatch `AGENT` with the **Implementer template**.
3. **Stage.** If `git rev-parse HEAD` differs from `TASK_BASE`, the agent committed: run `git reset --soft "$TASK_BASE"`. Then run `git add -A`.
4. **Nothing to review?** If `git diff --cached --quiet` succeeds (no changes), or the report's `**Status:**` line contains `❌`, use the `ask` tool:
   - question: `Task <N> produced <no changes | a ❌ Failed report>. What now?`
   - `Retry once` → go back to step 2. Allowed once per task; after a retry, offer only the other two options;
   - `Skip this task` → `git stash push --include-untracked -m "delivery: skipped task <N>"` and return result `skipped`;
   - `Stop delivery` → leave the tree as it is and return result `stopped`.
5. **Review, round `r = 0`.** Dispatch `delivery:task-reviewer` with the **Reviewer template**. Its structured output is `{verdict, findings}`. The review is blocking when any finding has severity `critical` or `important`, whatever `verdict` says.
6. **Fix rounds.** While the review is blocking and `r < 3`:
   - `r = r + 1`;
   - dispatch `AGENT` with the **Fix template**, passing the blocking findings;
   - repeat step 3;
   - dispatch `delivery:task-reviewer` again with the Reviewer template and the previous findings.
7. **Still blocking after round 3** → use the `ask` tool:
   - question: `Task <N> still has <k> blocking findings after 3 fix rounds. What now?`
   - options: `Accept and commit with open findings` (result `accepted-with-open-findings`), `Stop delivery` (result `stopped`).
8. Not blocking → result `approved`. Return `{result, rounds: r, findings}` to the calling command. The calling command commits; this loop never does.

### Implementer template

```
You are implementing Task <N> of the plan <PLAN_PATH>.

<TASK_BLOCK>

Rules:
- Implement exactly this task. Write tests first when the task lists a Test file.
- Do not commit, stash, switch branches or rewrite history. Leave all changes in the working tree.
- End with your report, including a **Status:** line (✅ Complete | ⚠️ Partial | ❌ Failed).
```

### Reviewer template

```
Review the staged changes for Task <N> of <PLAN_PATH>.
Stack plugin: <PLUGIN>

<TASK_BLOCK>
```

From round 1 on, append:

```

Findings from the previous review — verify each is resolved:
<previous findings as JSON>
```

### Fix template

```
Fix the review findings for Task <N> of <PLAN_PATH>. Your earlier implementation is staged (git diff --cached).

<TASK_BLOCK>

Findings to fix — fix exactly these, do not redo the task:
<blocking findings as JSON>

Rules:
- Implement exactly this task. Write tests first when the task lists a Test file.
- Do not commit, stash, switch branches or rewrite history. Leave all changes in the working tree.
- End with your report, including a **Status:** line (✅ Complete | ⚠️ Partial | ❌ Failed).
```
