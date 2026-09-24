# Delivery 0.4.0: plan-check gates, router-built commits, review commit order, wait tool

## Context

Delivery (`omp/native/delivery/`, OMP-only; 0.3.0 is in the working tree, uncommitted) has three defects found on 2026-09-24 and one stale tool name:

1. `/delivery:execute` silently drops a task whose heading is malformed: `route_task.py plan` skips it while `route_task.py check` reports it, and the preflight in `skills/orchestration/SKILL.md` stops only on duplicate task numbers (verified: a plan with `### Task 1: Add search` and `### Task 2 — Document search` gives `plan` → one task, `check` → `Invalid task heading`).
2. An approved plan can bypass delivery silently: `extensions/delivery.ts` returns without a notification when the plan file is missing or the router fails (approval path, lines 113–120), and the `xd://propose` gate passes a plan whose every `### Task` heading is malformed because it only blocks when `tasks > 0` (line 162). Verified with a bun probe on OMP 18.3.0.
3. The delivery run commits the review report after `/code-review:fix-all`, so the committed report carries `✅ Fixed` statuses while fix-all leaves the code uncommitted (`plugins-omp/code-review/commands/fix-all.md:579` "Changes remain uncommitted for your control").
4. OMP 18.3.0 has a `wait` tool and no `hub` tool (`src/tools/builtin-names.ts`; `/hub` is a slash command). `SKILL.md:173` says "use `hub` `wait` with the job id" and `OMP_TOOLS` in `scripts/build_omp_edition.py:72` lists `hub`. The `wait` tool takes no arguments (`waitSchema = type({})`).

Improvements delivered with the fixes: the commit message with its `Delivery-*` trailers and the resume detection move from model-written shell into router subcommands `message` and `done`; `node_modules/` is gitignored and the local delivery test commands are documented; CI checks that `OMP_TOOLS` equals OMP's `BUILTIN_TOOL_NAMES`. End state: delivery 0.4.0, generated edition regenerated, README and guide updated.

Decided, not open: the delivery branch is still created only off `main`/`master`. `/delivery:execute` keeps accepting tasks without a `**Files:**` block (routed by Jev or by asking; documented in the README), while the plan-mode gate keeps rejecting them.

Prerequisite outside the tasks: the working tree holds the uncommitted delivery 0.3.0 release and the review fixes (44 modified, 4 untracked files). Delivery's preflight stops on them. When it stops with `Commit or stash your other changes`, commit everything as one commit — `git add -A && git commit -m "feat(delivery): release 0.3.0 with the review fixes"` — and rerun `/delivery:execute local://delivery-hardening-plan.md`.

Routing facts for this repository: the plugin's `.ts` files route to `delivery:implementer` (the nearest `package.json`, `omp/native/delivery/package.json`, has no React), `.py` files route to `python-developer:developer`. `plugins-omp/delivery/**` is generated: every task that changes a file under `omp/native/delivery/` runs `python3 scripts/build_omp_edition.py` and lists the regenerated copy. The delivery run of this plan itself uses the installed delivery 0.3.0 (user scope), whose trailer format is the one Task 3 parses.

## Approach

### Task 1: Ignore node_modules and document the local delivery tests
**Commit:** chore(delivery): ignore node_modules and document the local test commands

**Files:**
- Modify: `.gitignore`
- Modify: `docs/contributing.md`

Why first: Tasks 4 and 7 run `bun test` with OMP linked into `omp/native/delivery/node_modules`; the ignore rule keeps `git add -A` in the delivery task loop from staging that link.

1. In `.gitignore`, insert after the `# Python` block (`__pycache__/`, `*.pyc`, `*.pyo`) and before the `# superpowers SDD scratch` comment, separated by one blank line:
   ```
   # Node packages (the OMP Edition workflow installs OMP under omp/native/delivery/)
   node_modules/
   ```
2. In `docs/contributing.md`, section `## Pull Request Requirements`, insert one bullet directly after the bullet that starts with `- Regenerated OMP edition` and before `- No unrelated changes bundled in the same PR`:
   ```
   - Passing delivery tests (if you changed `omp/native/delivery/`): run `python3 omp/native/delivery/tests/test_route_task.py`; then install OMP next to the extension with `bun install --no-save --cwd omp/native/delivery @oh-my-pi/pi-coding-agent@latest` (it lands in the gitignored `omp/native/delivery/node_modules/`; linking an existing global install works too: `ln -s ~/.bun/install/global/node_modules omp/native/delivery/node_modules`) and run `bun test tests/delivery.test.ts` from `omp/native/delivery/`. The `OMP Edition` workflow runs both.
   ```

Check: `git check-ignore -v omp/native/delivery/node_modules/x` prints the new `.gitignore` line; `grep -c 'bun test tests/delivery.test.ts' docs/contributing.md` prints `1`.

### Task 2: Report tasks without files apart from plan problems
**Commit:** feat(delivery): report tasks without files apart from plan problems

**Files:**
- Modify: `omp/native/delivery/scripts/route_task.py`
- Test: `omp/native/delivery/tests/test_route_task.py`
- Modify: `plugins-omp/delivery/scripts/route_task.py`

Behavior: `route_task.py check <root> <plan.md>` prints `{"tasks": N, "problems": [...], "no_files": [...]}`. `problems` keeps exactly: invalid task heading, empty `**Commit:**`, several stacks, duplicate task numbers. The existing message `Task N (title): lists no files. Add a **Files:** block with "- Create|Modify|Test|Delete: `path`" lines.` moves unchanged from `problems` into the new `no_files` list (same order as the tasks). `plan`, `files` and `layout` do not change. Consumers: the `xd://propose` gate (Task 4) blocks on either list; the preflight (Task 5) stops only on `problems`.

The test file is `unittest`, run with `python3 omp/native/delivery/tests/test_route_task.py`; keep that runner and style (no pytest, no new dependencies). Tests first:

1. In `CheckTest`:
   - `test_reports_every_unroutable_task`: `problems` has 2 entries — `Task 1 (Orders): touches several stacks` (still containing `python: backend/app/orders.py; frontend: web/src/api.ts`) and `Task 2 appears 2 times` — and `no_files` has 1 entry starting with `Task 2 (Notes): lists no files`.
   - `test_routable_plan_has_no_problems`: expected `{"tasks": 2, "problems": [], "no_files": []}`.
   - `test_empty_file_entry_does_not_consume_next_line`: the `lists no files` message is in `no_files` and `problems == []`.
   - `test_invalid_task_heading_in_fence_is_not_reported`: expected dict gains `"no_files": []`.
2. In `CliTest`: `test_check_without_tasks_reports_zero` expects `{"tasks": 0, "problems": [], "no_files": []}`; new `test_check_cli_separates_tasks_without_files`: plan file with a first task `Notes` that has no **Files** block and a second task `Orders` that modifies `backend/app/orders.py`; `check` exits 0, `problems == []`, `no_files` has one entry containing `Task 1 (Notes)`.
3. Implement in `route_task.py`: `check()` collects the no-files messages in a separate `no_files` list and returns `{"tasks": len(tasks), "problems": problems, "no_files": no_files}`; update the module docstring's `check` usage line to `JSON {"tasks": N, "problems": [...], "no_files": [...]}` and the `check()` docstring to say that `no_files` lists tasks without a Files block.
4. Run `python3 scripts/build_omp_edition.py` so `plugins-omp/delivery/scripts/route_task.py` matches.

Checks: `python3 omp/native/delivery/tests/test_route_task.py` → `OK`; `python3 scripts/build_omp_edition.py --check` → `OMP edition is up to date`.

### Task 3: Router subcommands message and done
**Commit:** feat(delivery): build commit messages and detect delivered tasks in the router

**Files:**
- Modify: `omp/native/delivery/scripts/route_task.py`
- Test: `omp/native/delivery/tests/test_route_task.py`
- Modify: `plugins-omp/delivery/scripts/route_task.py`

Two deterministic subcommands (no model, no shell interpolation), consumed by the orchestration skill in Task 5.

`route_task.py message <root> <plan.md> <N> [--open-findings]` prints to stdout exactly:
```
<the task's commit, or "chore: <title>" when the task has no **Commit:** line>

Delivery-Plan: <plan path relative to root, posix>
Delivery-Task: <N>
Delivery-Task-Title: <title>
```
plus, with `--open-findings`, a last line `Delivery-Review: accepted-with-open-findings`. The output ends with one newline. It exits 2 with a stderr message when the plan has no tasks or duplicate numbers (same messages as `plan`), when `N` is not a task number of the plan (`task <N> is not in <plan path>`), when the plan is outside `<root>` (`plan must be inside <root>`), or when an argument other than `--open-findings` follows `<N>`.

`route_task.py done <root> <plan.md>` prints `{"done": [N, ...], "conflicts": [{"commit": "<sha>", "task": N, "committed_title": "<title>" or null, "plan_title": "<title>"}, ...], "base": "<sha>"}`:
- runs `git -C <root> log --topo-order --reverse -F --grep=Delivery-Plan: <rel> --format=%H%x00%B%x00` (the `--grep=` value is one argument); a non-zero exit prints git's stderr and exits 2;
- a commit counts only when its body contains the exact line `Delivery-Plan: <rel>` (the `--grep` substring match also hits `<rel>.bak`; the exact line filters it);
- for each exact line `Delivery-Task: <digits>` of a counting commit whose number is a task of the plan: when the commit's `Delivery-Task-Title: <title>` line equals the plan's current title byte for byte, the number goes to `done`; otherwise a `conflicts` entry with the committed title, or `null` when the commit has no title line. Numbers that are not tasks of the plan are ignored. `done` is sorted and unique;
- `base`: with no counting commit, `git rev-parse HEAD`; otherwise the parent of the first counting commit in that (oldest-first) order, via `git rev-parse <sha>^`; a failure prints git's stderr and exits 2.

Tests first, in `test_route_task.py` (CLI-level, through `run_router`, like `CliTest`):

1. `MessageTest(RoutingFixture)` writing `PLAN` to `plan.md` in the fixture root:
   - `message <root> plan.md 1` → stdout is exactly `feat: add orders endpoint\n\nDelivery-Plan: plan.md\nDelivery-Task: 1\nDelivery-Task-Title: Orders endpoint\n`;
   - `message <root> plan.md 2 --open-findings` → stdout starts with `chore: Docs\n\n` and ends with `Delivery-Task-Title: Docs\nDelivery-Review: accepted-with-open-findings\n`;
   - `message <root> plan.md 3` → exit 2, stderr contains `task 3 is not in`;
   - `message <root> plan.md 1 --bogus` → exit 2;
   - a plan written to a second temporary directory outside the root → exit 2, stderr contains `plan must be inside`.
2. `DoneTest(unittest.TestCase)` with a fresh temporary git repository per test: `git init -q`, one `init` commit, `PLAN` written to `docs/plans/plan.md`. Helper `commit(repo, message)` runs `git -C <repo> -c user.name=test -c user.email=test@example.com -c commit.gpgsign=false commit -q --allow-empty -F -` with `input=message` and returns the new HEAD sha. Cases:
   - no delivery commits → `{"done": [], "conflicts": [], "base": <HEAD sha>}`;
   - one commit `feat: x\n\nDelivery-Plan: docs/plans/plan.md\nDelivery-Task: 1\nDelivery-Task-Title: Orders endpoint\n` → `done == [1]`, `conflicts == []`, `base` == sha of `init`;
   - the same with title `Old name` → `done == []`, one conflict `{"commit": <sha>, "task": 1, "committed_title": "Old name", "plan_title": "Orders endpoint"}`;
   - the same without a `Delivery-Task-Title` line → one conflict with `committed_title` `None`;
   - a commit with `Delivery-Plan: docs/plans/plan.md.bak` and matching task lines → `done == []`, `conflicts == []`, `base` == HEAD;
   - a commit with the exact plan line and `Delivery-Task: 9` → `done == []`, `conflicts == []`, `base` == the parent of that commit;
   - commits for task 2 then task 1 (both titles matching) → `done == [1, 2]`, `base` == the parent of the task-2 commit.
3. Implement in `route_task.py`: `import subprocess`; `plan_rel(root: Path, plan_path: Path) -> str` (`plan_path.resolve().relative_to(root).as_posix()`; `ValueError` → `main` prints `plan must be inside <root>` and returns 2); `commit_message(rel: str, tasks: list[dict], number: int, open_findings: bool) -> str` (raises `ValueError("task <N> is not in the plan")`; `main` prints `task <N> is not in <plan path>`); `delivered(root: Path, rel: str, tasks: list[dict]) -> dict` (raises `RuntimeError(git stderr)`); extend `main()`: add `message` and `done` to the accepted commands, load the plan and reject "no headings"/duplicates exactly as `plan` does, then dispatch; keep the manual `argv` parsing (no argparse). Update the module docstring usage block with both subcommands.
4. Run `python3 scripts/build_omp_edition.py`.

Checks: `python3 omp/native/delivery/tests/test_route_task.py` → `OK`; `python3 scripts/build_omp_edition.py --check` → up to date.

### Task 4: Warn when approval skips delivery and block plans without a valid task
**Commit:** fix(delivery): warn when approval skips delivery and block plans without a valid task

**Files:**
- Modify: `omp/native/delivery/extensions/delivery.ts`
- Test: `omp/native/delivery/tests/delivery.test.ts`
- Modify: `plugins-omp/delivery/extensions/delivery.ts`

Depends on Task 2 (`no_files` in the `check` JSON). Prerequisite for running the tests: `[ -e omp/native/delivery/node_modules ] || ln -s "$HOME/.bun/install/global/node_modules" omp/native/delivery/node_modules` (that global install holds `@oh-my-pi/pi-coding-agent` 18.3.0 and `@oh-my-pi/pi-tui`; the link is gitignored since Task 1), then `bun test tests/delivery.test.ts` from `omp/native/delivery/`.

Changes in `delivery.ts` (keep the current order of checks):

1. Declare once, next to `ROUTER`: `type PlanCheck = { tasks: number; problems: string[]; no_files: string[] };` and make `checkPlan` return `Promise<PlanCheck>`; the gate's `let result: {...}` becomes `let result: PlanCheck`.
2. Approval path (`before_agent_start`, after `approvedPlanPath` matched and `gitRoot` succeeded):
   - `!file || !existsSync(file)` → `ctx.ui.notify(\`Delivery skipped: plan file not found for ${url}. The plan runs without delivery.\`, "warning")` and return `undefined`;
   - `checkPlan` throws → `ctx.ui.notify(\`Delivery skipped: plan check failed for ${url}: ${String(error)}. The plan runs without delivery.\`, "warning")` and return `undefined` (keep `tasks` as a local; `({ tasks, problems } = ...)` or a `result` variable);
   - `tasks === 0`: when `problems.length > 0`, `ctx.ui.notify(\`Delivery skipped: ${url} has no valid task heading. ${problems.join(" ")} The plan runs without delivery.\`, "warning")`; return `undefined` in both cases (a plan with no `### Task` line at all stays silent).
3. Gate (`tool_call`): replace `if (result.tasks === 0 || result.problems.length === 0) return undefined;` with `const violations = [...result.problems, ...result.no_files]; if (violations.length === 0) return undefined;` and list `violations` instead of `result.problems` in `reason`. Header and closing lines of `reason` stay as they are.
4. File header comment: add a line `- Plan approval that cannot start delivery (missing plan file, failed check, no valid task) warns instead of staying silent.`

Tests in `delivery.test.ts` (the existing five stay; add four, reusing `callHook`, `notifications`, `notificationLevels` and the `execute` override pattern of the nonzero-codes test):

- `approval warns when the plan file is missing`: approved prompt for `local://gone-plan.md` without writing the file → result `undefined`; `notifications[0]` contains `Delivery skipped: plan file not found for local://gone-plan.md`; `notificationLevels[0]` is `warning`.
- `approval warns when the router fails`: `validPlan` written to `feature-plan.md`; `execute` override returns `code: 1` for `python3` → `undefined`; `notifications[0]` contains `Delivery skipped: plan check failed for local://feature-plan.md`.
- `xd://propose blocks a plan whose only task heading is malformed`: plan text `# Plan\n\n### Task 1 - Docs\n**Files:**\n- Modify: \`README.md\`\n` written to `bad-plan.md`; `tool_call` with content `bad` → `block === true`, `reason` contains `Invalid task heading`.
- `approval warns and skips a plan whose only task heading is malformed`: the same plan approved → `undefined`; `notifications[0]` contains `has no valid task heading` and `Invalid task heading`; level `warning`.

Then `python3 scripts/build_omp_edition.py`.

Checks: `bun test tests/delivery.test.ts` (from `omp/native/delivery/`) → `9 pass`, `0 fail`; `python3 scripts/build_omp_edition.py --check` → up to date.

### Task 5: Orchestration: check before branching, router-built commits and resume, review commit before the fix offer, wait tool
**Commit:** feat(delivery): stop on every plan problem before branching and commit the review before the fix offer

**Files:**
- Modify: `omp/native/delivery/skills/orchestration/SKILL.md`
- Modify: `plugins-omp/delivery/skills/orchestration/SKILL.md`

Depends on Tasks 2 and 3. Edit `omp/native/delivery/skills/orchestration/SKILL.md`; everything not named below stays byte-identical. The fenced blocks below are quoted with `~~~` only to carry them in this plan: copy their content without the outer `~~~` lines.

A. Replace the whole `### 1. Preflight` section (from that heading up to, not including, `### 2. Tasks`) with:

~~~markdown
### 1. Preflight

1. Run **Plugin roots**. Keep the `code-review` root.
2. `REPO=$(git rev-parse --show-toplevel)`. A failure → stop with `Delivery needs a git repository.` Step 3 resolves paths from the session's working directory. Run every command after step 3 from `$REPO`.
3. **Plan file.**
   - `PLAN_FILE` given → keep it.
   - `PLAN_SOURCE` is a `local://` URL and no `PLAN_FILE` is given → leave `PLAN_FILE` unset.
   - Otherwise `PLAN_FILE=$(realpath "$PLAN_SOURCE")`; a failure → stop with `Plan not found: <PLAN_SOURCE>.`
   - `PLAN_FILE` inside `$REPO/` → `PLAN_PATH` is its path relative to `REPO`. Otherwise step 7 sets `PLAN_PATH`.
4. **Slug.**
   ```bash
   SLUG=$(python3 -c 'import re,sys; s=sys.argv[1].rsplit("/",1)[-1].lower().removesuffix(".md"); s=re.sub(r"[^a-z0-9]+","-",s).strip("-"); s=re.sub(r"^\d{4}-\d{2}-\d{2}-","",s); print(re.sub(r"-plan$","",s) or "plan")' "$PLAN_SOURCE")
   ```
5. **Other changes.** When `PLAN_PATH` is set, run `git status --porcelain --untracked-files=all -- . ":(exclude,literal)$PLAN_PATH"`; otherwise run `git status --porcelain --untracked-files=all`. Any output → stop with `Commit or stash your other changes before delivery, then retry <PLAN_SOURCE>.` Nothing below runs on this path.
6. **Current branch.** `BRANCH=$(git branch --show-current)`; empty (detached HEAD) → stop with `Check out a branch first.`
7. **Save the plan** when `PLAN_PATH` is not set yet:
   ```bash
   DATE=$(date +%F); P="docs/plans/$DATE-$SLUG.md"; n=2
   while [ -e "$P" ]; do P="docs/plans/$DATE-$SLUG-$n.md"; n=$((n+1)); done
   mkdir -p docs/plans
   ```
   `PLAN_PATH=$P`. With `PLAN_FILE`, run `cp "$PLAN_FILE" "$PLAN_PATH"`. Without it, `read <PLAN_SOURCE>:raw` and `write` that exact text to `PLAN_PATH`. The file stays untracked until step 10.
8. **Plan check.** `CHECK=$(python3 "$ROUTER" check "$REPO" "$PLAN_PATH")`; a non-zero exit → stop with the router's error. When its JSON `problems` list is not empty → stop with one line per problem:
   ```
   Delivery cannot run <PLAN_PATH>:
   - <problem>
   Fix these tasks in <PLAN_PATH>, then run /delivery:execute <PLAN_PATH>.
   ```
   A plan saved in step 7 stays where it is. Entries of `no_files` are not problems: step 14 routes those tasks.
9. **Delivery branch.** `BRANCH` is `main` or `master` → create the first free delivery branch; the untracked plan comes along.
   ```bash
   B="delivery/$SLUG"; n=2
   while git rev-parse --verify --quiet "refs/heads/$B" >/dev/null; do B="delivery/$SLUG-$n"; n=$((n+1)); done
   git switch -c "$B"
   ```
   `BRANCH` becomes `$B`.
10. **Commit the plan** when `git status --porcelain -- "$PLAN_PATH"` prints anything. The subject is `docs: update delivery plan <SLUG>` when `git ls-files --error-unmatch -- "$PLAN_PATH"` succeeds, otherwise `docs: add delivery plan <SLUG>`. Run `git add -- "$PLAN_PATH"`, then `git commit -m "<subject>" -- "$PLAN_PATH"`. A failed commit → print git's output and stop.
11. If `git status --porcelain` prints anything → stop with `Commit or stash your other changes, then run /delivery:execute <PLAN_PATH>.`
12. `TASKS` = JSON output of `python3 "$ROUTER" plan "$REPO" "$PLAN_PATH"`; a non-zero exit → stop with the router's error.
13. **Done tasks and base.** `DONE=$(python3 "$ROUTER" done "$REPO" "$PLAN_PATH")`; a non-zero exit → stop with the router's error. When its `conflicts` list is not empty → stop, printing one line per entry: `Task <task> is committed as "<committed_title, or missing>" in <commit>, but <PLAN_PATH> titles it "<plan_title>". Restore the title or drop the commit, then run /delivery:execute <PLAN_PATH>.` Mark no task done on this path. Otherwise the tasks listed in `done` are done and `BASE` is its `base`.
14. Route every not-done task with **Routing**. For an `unknown` task, `TASK_TEXT` is its `block`. Print the `Routing: task <N> → <agent> (source: <source>)` line for every task in your reply before starting step 2; it is the audit trail of each routing decision.
15. Every routed agent must be listed among the `task` tool's available agents. A missing one → stop with `Install <plugin>: omp plugin install <plugin>@av-marketplace, then start a new session.` (`<plugin>` is the part before `:`).
16. `todo init` with one item per not-done task, `Task <N>: <title>`, then `Plan verification` and `Final code review`.

If every task is already done, print `All tasks of <PLAN_PATH> are delivered.` and go to step 3.
~~~

B. In `### 2. Tasks`, step 2, replace the bullet that starts with `- \`approved\` or \`accepted-with-open-findings\` → commit with \`git commit -F -\`` (including its fenced message and the `For \`accepted-with-open-findings\`` sentence) and the following bullet `- A failed commit (for example a rejecting hook) → print git's output and stop.` with:

~~~markdown
   - `approved` or `accepted-with-open-findings` → commit the staged changes with the router's message: `python3 "$ROUTER" message "$REPO" "$PLAN_PATH" <N> | git commit -F -`; for `accepted-with-open-findings` add `--open-findings` after `<N>`. Mark the todo item done.
   - A failed commit (for example a rejecting hook, or a router error that leaves the message empty) → print the output and stop.
~~~

C. In `### 5. Final review`, after the bullet that reads `review.md` and its fenced argument, add this bullet (before `Mark \`Final code review\` done.`):

~~~markdown
- If the review saved a report, commit it before anything else touches it: `git add -- "<report path>"`, then `git commit -m "docs: add review of delivery $SLUG" -- "<report path>"`. Commit only that path. If adding or committing fails, print git's output and stop.
~~~

D. Replace the whole `### 6. Fix offer` section with:

~~~markdown
### 6. Fix offer

If the review saved a report, use the `ask` tool: `Run /code-review:fix-all on <report path>?` with options `Yes` and `No`. On `Yes`, `read <code-review root>/commands/fix-all.md` and carry it out completely with `$ARGUMENTS` = the report path. Whatever it changes, including the statuses it writes into the report, stays uncommitted: end with `Review fixes are uncommitted; review them and commit.` Without a saved report, end the run.
~~~

E. In `## Plugin roots`, replace the last paragraph (`The router prints JSON. Each task entry of ...`) with:

~~~markdown
The router prints JSON:

- `check "$REPO" <plan>` → `{"tasks": N, "problems": [...], "no_files": [...]}`. `problems` are plan errors that stop a run; `no_files` names tasks without a **Files:** block, which **Routing** handles.
- `plan "$REPO" <plan>` → one entry per task with `task`, `title`, `commit`, `block`, `files`, `stack`, `agent`, `groups`.
- `message "$REPO" <plan> <N> [--open-findings]` → the commit message of task N with its `Delivery-*` trailers.
- `done "$REPO" <plan>` → `{"done": [...], "conflicts": [...], "base": "<sha>"}` from the branch's delivery commits.
~~~

F. In `## Routing`, delete the bullet `- \`stack\` is \`split\` → do not route; the Delivery run stops on it.` (preflight step 8 stops on it before routing).

G. In `## Task loop`, replace the sentence `Wait for its result before continuing; if it has not arrived, use \`hub\` \`wait\` with the job id.` with `Its result arrives on its own; if you have nothing else to do until then, call the \`wait\` tool.`

Then `python3 scripts/build_omp_edition.py`.

Checks: `grep -n 'hub\|steps 11\|touches several stacks (<groups>)' omp/native/delivery/skills/orchestration/SKILL.md` prints nothing; `grep -c '"\$ROUTER" message\|"\$ROUTER" done\|"\$ROUTER" check' omp/native/delivery/skills/orchestration/SKILL.md` prints `3` or more; `python3 scripts/build_omp_edition.py --check` → up to date.

### Task 6: OMP_TOOLS names wait, and a checker compares it with OMP
**Commit:** fix(omp-edition): replace hub with wait in OMP_TOOLS and check it against OMP

**Files:**
- Modify: `scripts/build_omp_edition.py`
- Create: `scripts/check_omp_tools.py`
- Test: `scripts/test_check_omp_tools.py`

No equivalent checker exists; `OMP_TOOLS` is only ever compared by hand.

1. Test first, `scripts/test_check_omp_tools.py` (`unittest`; `sys.path.insert(0, str(Path(__file__).resolve().parent))`; `from check_omp_tools import builtin_tool_names, main`; `from build_omp_edition import OMP_TOOLS`; run with `python3 scripts/test_check_omp_tools.py`; capture output with `contextlib.redirect_stdout`/`redirect_stderr` into `io.StringIO`). Fixture text: `'export const BUILTIN_TOOL_NAMES = [\n\t"read",\n\t"wait",\n] as const;\n\nexport const HIDDEN_TOOL_NAMES = ["yield"] as const;\n'`. Cases:
   - `builtin_tool_names(fixture) == {"read", "wait"}` (hidden names excluded); a text without the constant raises `ValueError`.
   - a temp package dir with `src/tools/builtin-names.ts` listing exactly `sorted(OMP_TOOLS)` and `package.json` `{"version": "18.3.0"}` → `main([dir]) == 0` and stdout is `OMP_TOOLS matches OMP 18.3.0 (<len(OMP_TOOLS)> tools)\n`.
   - the same file with an extra `"hub"` → `main([dir]) == 1`, stderr contains `missing from OMP_TOOLS: hub`; with `"wait"` removed instead → `1` and stderr contains `not in OMP: wait`.
   - an empty temp dir → `main([dir]) == 2`, stderr contains `builtin-names.ts not found`.
2. Create `scripts/check_omp_tools.py`:
   - module docstring: purpose, usage `check_omp_tools.py [<pi-coding-agent package dir>]`, default dir, exit codes 0/1/2;
   - `DEFAULT_PACKAGE = Path("omp/native/delivery/node_modules/@oh-my-pi/pi-coding-agent")` (where `.github/workflows/omp-edition.yml` installs OMP);
   - `NAMES = re.compile(r"export const BUILTIN_TOOL_NAMES = \[(.*?)\] as const;", re.S)`; `builtin_tool_names(source: str) -> set[str]` returns `set(re.findall(r'"([^"]+)"', match.group(1)))`, raising `ValueError("BUILTIN_TOOL_NAMES not found")` without a match;
   - `main(argv: list[str]) -> int`: package dir = `Path(argv[0])` when given, else `DEFAULT_PACKAGE`; names file = `<dir>/src/tools/builtin-names.ts`; missing → stderr `builtin-names.ts not found under <dir>; run: bun install --no-save --cwd omp/native/delivery @oh-my-pi/pi-coding-agent@latest` → 2; version = `"version"` of `<dir>/package.json` when that file exists, else `?`; compare `OMP_TOOLS` (`from build_omp_edition import OMP_TOOLS`; the script lives in the same directory) with the parsed set: equal → stdout `OMP_TOOLS matches OMP <version> (<N> tools)` → 0; otherwise stderr `OMP_TOOLS in scripts/build_omp_edition.py is out of date for OMP <version>: missing from OMP_TOOLS: <comma-separated, sorted>; not in OMP: <comma-separated, sorted>` (omit an empty half) → 1;
   - `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`; executable bit like the other scripts.
3. In `scripts/build_omp_edition.py`, `OMP_TOOLS`: replace `"hub"` with `"wait"` (keep the set's layout); change the comment above it to say the set mirrors OMP's `tools/builtin-names.ts` and that `scripts/check_omp_tools.py` verifies it in CI.

Checks: `python3 scripts/test_check_omp_tools.py` → `OK`; `python3 scripts/test_build_omp_edition.py` → `OK`; `python3 scripts/check_omp_tools.py "$HOME/.bun/install/global/node_modules/@oh-my-pi/pi-coding-agent"` → exit 0 and `OMP_TOOLS matches OMP 18.3.0 (29 tools)`; `python3 scripts/build_omp_edition.py --check` → up to date.

### Task 7: CI tool check, guide and README updates, delivery 0.4.0
**Commit:** chore(delivery): release 0.4.0, check OMP_TOOLS in CI, update the guide

**Files:**
- Modify: `.github/workflows/omp-edition.yml`
- Modify: `docs/plugins/delivery.md`
- Modify: `README.md`
- Modify: `omp/native/delivery/.omp-plugin/plugin.json`
- Modify: `omp/native/delivery/package.json`
- Modify: `.omp-plugin/marketplace.json`
- Modify: `plugins-omp/delivery/.omp-plugin/plugin.json`
- Modify: `plugins-omp/delivery/package.json`

Depends on Task 6 (the script) and Task 5 (the behavior described).

1. `.github/workflows/omp-edition.yml`: directly after the step `Install OMP for delivery hook tests` and before `Test delivery hooks against OMP`, add:
   ```yaml
         - name: Check OMP_TOOLS against OMP
           run: python3 scripts/check_omp_tools.py
   ```
2. `docs/plugins/delivery.md`:
   - In `## Plan format`, replace the last sentence of the last paragraph (`In plan mode, proposing a plan checks its task format; \`/delivery:execute\` runs the plan file, so check a hand-written plan against these rules before starting.`) with:
     ```
     Proposing a plan in plan mode rejects it, listing every violation, when a task heading is malformed, a task lists no files or touches several stacks, a `**Commit:**` line is empty, or a task number repeats. `/delivery:execute` runs the same check before it creates a branch or commits anything and stops with the same list; the one difference is a task without a `**Files:**` block, which `/delivery:execute` accepts and routes by asking Jev, or you (see the README).
     ```
   - In `## Commit trailers and resuming`, append to the second paragraph:
     ```
     The stop message names them: `Task N is committed as "<committed title>" in <commit>, but <plan> titles it "<plan title>"`.
     ```
   - In `## Review and fix rounds`, append a new paragraph:
     ```
     When the final review saves a report, Delivery commits it alone as `docs: add review of delivery <slug>` before offering `/code-review:fix-all`. Whatever fix-all changes, including the statuses it writes into the report, stays uncommitted for you to review and commit.
     ```
3. `README.md`, section `### Oh My Pi (OMP)`, paragraph starting `Delivery runs approved plans end to end`:
   - replace `proposing a plan whose task mixes stacks or lists no files is rejected with the reason` with `proposing a plan whose task mixes stacks, lists no files or has a malformed \`### Task\` heading is rejected with the reason`;
   - replace `Before creating a branch or committing the plan, delivery stops if the working tree has changes other than the plan itself.` with `Before creating a branch or committing the plan, delivery stops if the working tree has changes other than the plan itself, or if the plan check finds a problem.`;
   - in item 3, replace `If you save the review report, delivery commits that report alone after the optional \`/code-review:fix-all\`.` with `If you save the review report, delivery commits that report alone, then offers \`/code-review:fix-all\`, whose changes stay uncommitted.`
4. Version 0.4.0: set `"version": "0.4.0"` in `omp/native/delivery/.omp-plugin/plugin.json` and `omp/native/delivery/package.json`, then run `python3 scripts/build_omp_edition.py`; it rewrites `.omp-plugin/marketplace.json` (delivery entry `0.4.0`) and the two copies under `plugins-omp/delivery/`.

Checks: `python3 scripts/build_omp_edition.py --check` → up to date; `grep -c '0\.4\.0' .omp-plugin/marketplace.json omp/native/delivery/.omp-plugin/plugin.json omp/native/delivery/package.json` → `1` per file; `grep -n 'check_omp_tools' .github/workflows/omp-edition.yml` → one line, after the OMP install step.

## Critical files & anchors

- `omp/native/delivery/scripts/route_task.py` — `check()` (~line 185) gains `no_files`; `main()` (~line 249) gains `message` and `done`; the plan-loading branch of `main` is shared by `plan`, `message`, `done`.
- `omp/native/delivery/extensions/delivery.ts` — approval branch lines 106–121 (silent returns), gate lines 141–171 (`result.tasks === 0 ||` condition).
- `omp/native/delivery/skills/orchestration/SKILL.md` — `### 1. Preflight` (whole section replaced), `### 2. Tasks` step 2 commit bullet, `### 5`/`### 6` (report commit moves before the fix offer), `## Plugin roots` last paragraph, `## Routing` split bullet, `## Task loop` line 173 (`hub`).
- `scripts/build_omp_edition.py:69-75` — `OMP_TOOLS`; validation error at line 182 uses it.
- `.github/workflows/omp-edition.yml` — the new step must follow `Install OMP for delivery hook tests`.

## Verification

Run from the repository root on the delivery branch after Task 7:

1. `python3 scripts/build_omp_edition.py --check` → `OMP edition is up to date`.
2. `python3 omp/native/delivery/tests/test_route_task.py` → ends with `OK`, no failures or errors.
3. `python3 scripts/test_build_omp_edition.py && python3 scripts/test_check_omp_tools.py` → both end with `OK`.
4. `python3 scripts/check_omp_tools.py "$HOME/.bun/install/global/node_modules/@oh-my-pi/pi-coding-agent"` → exit 0, prints `OMP_TOOLS matches OMP 18.3.0 (29 tools)`.
5. Hook tests: `[ -e omp/native/delivery/node_modules ] || ln -s "$HOME/.bun/install/global/node_modules" omp/native/delivery/node_modules`, then `bun test tests/delivery.test.ts` in `omp/native/delivery/` → `9 pass`, `0 fail`.
6. A malformed heading is reported, not dropped. Write `/tmp/dlv-check.md` with:
   ```
   # P

   ### Task 1: Add search
   **Files:**
   - Modify: `README.md`

   ### Task 2 - Document search
   **Files:**
   - Modify: `docs/x.md`
   ```
   `python3 omp/native/delivery/scripts/route_task.py check . /tmp/dlv-check.md` → `{"tasks": 1, "problems": ["Invalid task heading '### Task 2 - Document search'. Use '### Task N: <title>' on one line."], "no_files": []}`.
7. `message` and `done` against this delivery's own commits: `P=$(ls docs/plans/*-delivery-hardening*.md | head -1)`.
   - `python3 omp/native/delivery/scripts/route_task.py message . "$P" 1` prints `chore(delivery): ignore node_modules and document the local test commands`, an empty line, `Delivery-Plan: $P`, `Delivery-Task: 1`, `Delivery-Task-Title: Ignore node_modules and document the local delivery tests`.
   - `python3 omp/native/delivery/scripts/route_task.py done . "$P"` prints `done` = `[1, 2, 3, 4, 5, 6, 7]`, `conflicts` = `[]`, and `base` equal to `git log --format=%H --grep='docs: add delivery plan delivery-hardening' -1` (the plan commit is the parent of the first task commit).
8. `git status --porcelain` prints nothing (the `node_modules` link is ignored).
9. Manual, in a fresh OMP session after `omp plugin install delivery@av-marketplace` picks up 0.4.0, in the fixture `/tmp/delivery-e2e` (clean, on `main`): (a) in plan mode, write a plan whose only heading is `### Task 1 - Bad` and propose it → the proposal is rejected with `Invalid task heading`; (b) approve a two-task plan whose Task 2 title contains backticks (for example `Rename \`foo\``), interrupt the session after Task 1's commit, run `/delivery:execute <PLAN_PATH>` → Task 1 is skipped as done, Task 2's commit body carries `Delivery-Task-Title: Rename \`foo\`` verbatim; (c) save the review report and answer `Yes` to the fix offer → `git log --format=%s` shows `docs: add review of delivery <slug>` on top, and `git status --porcelain` lists the report and the files fix-all changed as uncommitted.

## Assumptions & contingencies

- Version bump is `0.4.0` (behavior changes and new router subcommands). If it should fold into the uncommitted 0.3.0 instead, keep `0.3.0` and skip step 4 of Task 7.
- OMP 18.3.0 is globally installed at `~/.bun/install/global/node_modules` with `@oh-my-pi/pi-coding-agent` and `@oh-my-pi/pi-tui`. If that directory is missing, run the CI command `bun install --no-save --cwd omp/native/delivery @oh-my-pi/pi-coding-agent@latest` (verified with `--dry-run` on 2026-09-24) instead of the symlink; both land in the gitignored `node_modules/`.
- Verification step 4 expects `29 tools`; if the global OMP is newer than 18.3.0 and the check exits 1, that is the checker doing its job: report the difference as a `fail` with the names, do not edit `OMP_TOOLS` during verification.
- The delivery of this plan runs on the installed delivery 0.3.0, so its own commits are made by the 0.3.0 procedure; Verification step 7 depends only on the trailer lines, which 0.3.0 and 0.4.0 write identically.
- If the preflight stops on the uncommitted 0.3.0 work, commit it as one commit `feat(delivery): release 0.3.0 with the review fixes` (decided) and rerun `/delivery:execute local://delivery-hardening-plan.md`.
