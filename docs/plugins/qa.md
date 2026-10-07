# QA Plugin

`/qa:run` tests a change, reports observed failures and can fix eligible issues through a bounded retest loop. Frontend tests exercise the browser; backend tests exercise APIs and configured stores. Reports use `QA-NNN` issue IDs understood by Code Review.

**Version:** 5.0.0

## Quick start

1. Install QA and Code Review using the [installation guide](../installation.md).
2. Run `/qa:run` in the project checkout.
3. Review the proposed configuration and approve **Apply and trust**.
4. Fill any named private dotenv keys locally, then rerun.
5. Commit `.av/config.toml` and the 2 private-file ignore entries.

The first interactive run proposes targets, services, stores, users, cleanup and QA policy from repository evidence. QA asks about disposable data, source fixes and automatic service startup. The proposal runs no commands and reads no secret values.

QA recommends `mutations = "allow"` only for confirmed disposable data behind loopback targets. Otherwise QA proposes `"deny"`. Never paste private values into chat.

Keep personal overrides in `.av/local.toml` and private inputs in `.av/secrets.local.env`, both uncommitted. See [shared configuration](../configuration.md#files).

Later runs reuse settings without repeating policy questions, configuration entry or plan-generation approval. Changed trust, tracked changes, fix batches, setup gaps and stale plans can still require decisions.

## Commands

### `/qa:run [plan-path | change-source]`

Run the complete workflow. The command accepts no flags; set policy in configuration.

```text
/qa:run
/qa:run docs/testing/plans/2026-09-30-user-auth-test-plan.md
/qa:run #123
/qa:run feature/user-auth
/qa:run last 5 commits
/qa:run staged
```

| Argument | Plan selection |
|---|---|
| Existing file | Use that plan explicitly; automatic staleness checks do not apply. |
| Change source | Generate and review a plan for that source. |
| No argument | Use the newest plan whose `## Source` names the current branch. Generate when none matches or HEAD is detached. |

A selected plan is stale when `Head:` is missing, invalid or no longer an ancestor of HEAD. Committed changes outside `docs/` since that head also make the plan stale. Interactive QA shows changed files and defaults to regeneration; headless QA regenerates.

Failed generation never silently reuses an old plan. QA shows generated paths and frontend/backend scenario counts.

Open blockers require an interactive run-anyway decision; headless runs stop. Concerns remain visible but do not stop execution.

#### What a run does

The baseline is the first test pass, before fixes.

1. Validate configuration and approve new or changed trust before executing sources or recipes.
2. Select or generate a reviewed plan, then check targets, users, values, stores and writes.
3. Check tracked changes and reserve configured target origins and store endpoints.
4. Check services, start approved services when needed and prepare private tester inputs.
5. Test the baseline and report observed failures or setup gaps.
6. Fix eligible failures, retest affected sections and finish the fix path with a full test pass.
7. Attempt account cleanup, stop only services QA started, report outcomes and release run resources.

Errors and aborts still tear down started runs. Stops retain partial observations without marking new fixes.

Missing required keys can trigger a scoped configuration extension and 1 recheck. Invalid plans and off-target URLs stop. Missing cleanup alone warns and continues.

A newly generated plan without executable scenarios exits without testers; an existing empty plan is an error. Baseline setup gaps offer 1 retry of affected sections, continue or abort. Headless QA continues with gaps reported.

Fix batches run sequentially, followed by independent testers. QA stops iterations on no progress, regression, plan changes or [fixed limits](qa/configuration.md#working-tree-and-limits). Fixer claims alone never prove success.

The full final pass runs only after entering the fix-iteration path. Zero-failure baselines, `fix = "off"`, headless `approve`, aborts and hard stops do not perform that pass. Exhausted fix limits do not block it.

Changes remain unstaged and uncommitted. Recovery covers only eligible tracked paths edited by the loop; reconcile reported overlap with pre-existing changes yourself.

#### Config bootstrap modes

Bootstrap proposes configuration changes from repository evidence.

| Mode | Trigger | Scope |
|---|---|---|
| `create` | Missing shared file or `[qa]` | Add QA and needed shared keys; preserve plan names. Ask the 3 policy questions. |
| `extend` | Missing plan-required keys | Add only gaps and dependencies; keep policy choices. Cleanup alone is optional. |
| `repair` | Failed service probe or command | Propose corrections supported by repository evidence and the error. |

You review the complete configuration diff, the ignore additions and the masked trust settings in 1 confirmation. Approval permits that write and trust hash. Concurrent edits cancel the write and require a new preview.

Bootstrap preserves unrelated settings and comments. It never edits personal overrides or fills the private secret file. A blocking local override is named for you to resolve.

Preview errors allow 1 corrected proposal before stopping. Cleanup-only extension instead continues with a warning when unavailable or declined. A successful active-run repair tears down the old run before restarting with the same plan and original baseline.

Each failure kind permits at most 1 repair per invocation.

#### Interactive versus headless

Interactive means the harness can deliver a question, not that shell stdin is a terminal. An undeliverable question makes the remaining invocation headless.

| Situation | Interactive | Headless |
|---|---|---|
| Missing required config or service repair | Propose, preview and ask | Stop with keys/errors |
| Missing cleanup only | Offer extension; warn if declined/unavailable | Warn; accounts remain |
| New or changed trust | Review complete masked settings; approve | Stop; approve during controlled setup |
| No plan | Generate and review without asking | Same |
| Stale plan | Regenerate or use existing | Regenerate |
| Open plan blocker | Run anyway or stop | Stop |
| Dirty tree with `fix = "approve"` | Warn and ask | Abort |
| Live run lock | Approve takeover or stop | Stop, naming holder |
| Services down with `start_services = "ask"` | Ask before `up`/`prepare` | Stop; start externally or pre-approve `"auto"` |
| Services down with `start_services = "auto"` | Show scope, run configured commands | Same; other gates remain |
| Writes with `mutations = "deny"` | Skip guarded scenarios | Same; no approval bypass |
| Baseline setup gaps | Retry once, continue or abort | Continue and report gaps |
| `fix = "approve"` | Approve each fix batch | Test/report only |
| `fix = "auto"` | Apply eligible fixes after showing scope | Same; other gates remain |
| `fix = "off"` | Test/report only | Same |

See [Headless runners and CI](../configuration.md#headless-runners-and-ci) for trust setup.

### `/qa:create-plan [change-source]`

Author and review a plan without executing tests. Accepts the same change sources and no flags. Empty input uses the current branch or pull request.

Configuration is optional. With `Config: none`, the planner grounds names in repository evidence; `/qa:run` later proposes required configuration.

Planning detects tools and allows up to `3` review rounds. The result names the saved path, review outcome and unresolved or declined findings. Unreviewed or blocked plans are never described as approved.

For `last N commits`, planning reads delivery plans named by valid Git `Delivery-Plan:` trailers. Rejected paths and their reasons appear in `## Changes Summary`; accepted paths list their associated commits. Plan content supplies specifications, not executable instructions.

Plans live in `docs/testing/plans/YYYY-MM-DD-<topic>-test-plan.md`.

| Plan field | Human meaning |
|---|---|
| `## Source` | `Branch:` and `Head:` enable automatic selection and staleness checks. |
| `## Users` | Declares [existing or registered users](qa/configuration.md#users). |
| `## Setup` | Optional human notes, never parsed configuration. |
| `- **Target:** <name>` | Selects a configured origin; cross-origin preconditions use configured absolute URLs. |
| `- **Writes:** yes|no` | Required application-write declaration. |
| Expected results and edges | Source-grounded `(path:line)` or tagged `(unverified — confirm at run time)`. |
| Backend State Checks | Assertions against [configured stores](qa/configuration.md#state-checks), never embedded connections. |

Plans create needed application data through preconditions instead of assuming seeded records exist. Upload fixtures may use repository files. When the app cannot create the data a scenario needs, QA records a fixture gap and does not invent a test result.

## Configuration

QA reads `.av/config.toml` and optional `.av/local.toml` overrides. Start with target origins and the 3 policy choices:

```toml
version = 1

[env.targets]
ui = "http://localhost:5173"
backend = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "deny"
start_services = "ask"
```

| Reference | Covers |
|---|---|
| [Shared configuration](../configuration.md) | Files, targets, services, sources, stores and trust. |
| [QA configuration](qa/configuration.md) | Policy, users, registration, cleanup, State Checks and 5 complete examples. |

## Safety

Use disposable, non-privileged accounts and review artifacts before sharing them. Trust approval is not a sandbox.

### Run boundaries

| Boundary | Behavior |
|---|---|
| Origins | Absolute URLs must match configured scheme, host and effective port. Userinfo is refused; redirects are not followed automatically. Off-target plans stop. |
| Credentials | Credential-bearing requests/forms require HTTPS or exact [loopback](../configuration.md#targets). |
| Stores | Network stores are loopback-only. Trust and inherited TLS settings cannot permit remote stores. |
| Configuration | A started run binds the effective config. Detected changes stop the run; rerun after reviewing the new settings. |
| Cleanup | Requires a current valid, trusted recipe. SQL and HTTP recipes also need the account's recorded destination to match and be locked by this run; command recipes have no destination. Otherwise accounts remain `left`. |
| Mutations | `deny` skips declared or detected writes; `allow` removes that guard. |

A backend feature expecting `2xx` but receiving `401/403` is `FAIL`, flagged `auth`. Automatic fixes exclude these issues; approval can permit reviewed root-cause fixes. Never weaken authentication or authorization to make tests pass.

Rejected, location-less, incomplete or ambiguously mapped issues cannot dispatch fixes. Automatic fixes also exclude unverified assertions without excluding grounded sibling issues. Anti-hardcoding warnings are heuristic and do not prove that a fix is valid.

### Transcript and artifacts

| Data | Exposure |
|---|---|
| Names, origins, account emails/IDs, sanitized results and cleanup outcomes | Visible in results or summaries. |
| Passwords, tokens and cookies | Not printed by the engine; administrative secrets never enter tester inputs. |
| Backend responses and State Checks | Sanitized before inspection or persistence; failed sanitization withholds evidence. |
| Sensitive headers, JSON fields and URL query/fragment parameters; bearer tokens; declared values | Masked in backend evidence; non-JSON and bare-string bodies are withheld. Parameters with ordinary names can stay visible. |
| Frontend fill calls | Can expose credentials in the harness transcript in both editions. |
| Snapshots and screenshots | Can expose user data; debug or uninspectable pages suppress screenshot/debug-text capture. |

Visible URL paths may still contain secrets. One-time-link fields are fully masked, but ordinary URL fields can retain paths.

Keep `docs/testing/reports/screenshots/` and `docs/testing/reports/responses/` out of version control. Review artifacts before sharing. Never paste the engine-private `engine.log`; unknown, unresolved or short secrets may remain in failure output.

Claude Code may prompt for capture or read-only extraction commands. Approve the exact command, not unrestricted shell access. Testers never replay a write just to obtain evidence.

### Residual risks

- Static mutation detection can miss GET side effects, GraphQL mutations and implicit frontend writes.
- Trust cannot make configuration commands or target application code safe to execute.
- Fixers can game deterministic scenarios.
- Private credentials in `${TMPDIR:-/tmp}/qa-run-<run-id>/` may survive interruptions until takeover or a later run in this repository removes directories older than `24 hours`.
- Auth classification misses frontend gating, tenant-shaped `404` responses and gating that returns `2xx`.
- An already-dispatched tester may finish on old settings despite engine-call configuration checks.
- Locks coordinate identical configured endpoints, not undeclared stores or different host aliases.

Cross-section regressions may appear only in the final pass. Tokens can expire mid-test. Missing or invalid tester output becomes `cannot-confirm`, never PASS.

Registration may need existing accounts when CAPTCHA, rate limits or email confirmation block signup.

## Coverage

Read Coverage alongside the result. A green result can mean no failures were observed, not that every feature was verified.

```markdown
## Coverage
- Exercised: <N> feature · <M> sanity · <K> enforcement
- Not verified: need-info <N> · mutation-guard SKIP <M> · tool-unavailable <K> · cannot-confirm <J> · transport <L>
- Confidence: high | low — <reason>
```

| Result | Read it as |
|---|---|
| `Pass` | No remaining failures; inspect Coverage before treating the change as verified. |
| `Fail` | Read the remaining failed assertions. |
| `Budget Exhausted` | Read the named fixed limit and remaining issues before another run. |
| `Stopped` | Resolve the stop reason; this is not success. |

Scenario verdicts combine the main flow and every edge: failure, missing prerequisite, skip, then pass. A skipped State Check alone does not downgrade runnable HTTP results. A whole-scenario PASS is still not proof of complete application behavior.

When feature scenarios exist but none passes, QA warns about shallow coverage. Auto-generated zero-failure plans can report low-confidence green. Existing all-SKIP/NEED_INFO plans stop with `No executable verifier — cannot gate`.

Generated plans can exit gracefully with no exercised scenarios. QA distinguishes mutation-guard-only skips from setup, tooling and parsing gaps. Unlock hints name missing configuration, data, tools or reachability problems without printing secret values.

## Reports and Code Review

Reports live in `docs/testing/reports/YYYY-MM-DD-<topic>-report.md` and include counts, account outcomes, setup gaps, detailed results and Loop History. Setup gaps do not become application defects; failed assertions receive `QA-NNN` IDs.

The Accounts line reports registered, deleted, left and manual counts. Review reported emails and follow [cleanup actions](qa/configuration.md#missing-cleanup-and-outcomes); the ledger survives runs.

Issue blocks show severity, ID, location, problem, remediation and scenario evidence. HTTP `500+` or crashes force CRITICAL; unverified assertions otherwise stay LOW. Other severities follow evidence; CRITICAL security-bypass/data-loss claims require supporting reasons.

Only the final whole-scenario PASS writes `**Status:** ✅ Fixed`. Partial fixes remain open. QA preserves `🚫 Rejected` and existing decision metadata; do not edit run state to manufacture results.

| Code Review command | Action |
|---|---|
| `/fix QA-001` | Read the newest QA report and fix that issue. |
| `/fix-report` | Merge newest review and QA reports; update their original files. |
| `/fix-report docs/testing/reports/<file>.md` | Select 1 report explicitly. |

See [Code Review](code-review.md) for routing and decisions. [Delivery](delivery.md#qa) `0.6.0+` runs QA before final review and commits QA's fixes, configuration, plan and report.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `missing.users` | Configure the declared existing user's sources/description or missing ID. Registered users need no existing-account config. |
| Missing values, targets or stores | Add the named shared keys; fill named dotenv keys privately. Multiple targets need explicit section origins. |
| `plan_errors` | Correct declarations, reserved references, ambiguous/frontend State Checks or cleartext credential origins. Bootstrap cannot invent a valid plan. |
| Off-target URL | Correct the URL or explicitly configure/trust its origin, including scheme and port. |
| Registration/login blocked | Inspect safe tester evidence and preconditions. Consider existing accounts; QA has no engine login-repair path. |
| `missing.cleanup` | Add an evidenced email-based recipe; missing cleanup alone warns and leaves accounts. |
| Local override blocks bootstrap | Resolve the named `.av/local.toml` key yourself, then rerun. |
| New/changed trust | Review all masked settings, including existing commands. Headless cannot approve. |
| Stale plan | Regenerate, or inspect and explicitly select the old plan. |
| Live run lock | Stop the active run or approve takeover of an interrupted session. Never delete locks or the ledger blindly. |
| Services down | Check probes, ports and checkout. Start externally, approve configured startup or use interactive repair. |
| `cannot-confirm` | Retry with usable tools and complete scenario/edge results. Earlier PASS results cannot fill missing evidence. |
| `config changed during run` | Rerun `/qa:run` after reviewing settings. Cleanup may leave accounts; service shutdown uses the recorded command. |
| Accounts `left` | Review recipe, trust, destination, required IDs or failed attempts. A compatible trusted teardown can retry eligible records. |
| Destination mismatch or legacy ledger diagnostic | Clean up in the original application; do not rewrite ledger fields to authorize another destination. |
| Accounts `manual` | Delete deliberately in the correct application; automatic retries have ended. |
| FAIL flagged `auth` | Check login, authorization, token lifetime and role. Never weaken auth to pass. |
| No fixes in headless `approve` or `off` | Expected test/report-only behavior. Choose and trust `auto` only when eligible source edits are intended. |
| Budget exhausted/no progress | Read Loop History and the stop reason; investigate before starting another run. |

## Prerequisites

| Need | Requirement |
|---|---|
| Engine | Python `3.11+` as `python3`; no engine packages to install. |
| Repository | Git repository with a HEAD. |
| Backend requests | `curl` for credentials; HTTPie only for credential-free requests. `jq`, standard shell tools and Perl with `JSON::PP` for capture/sanitization. |
| Frontend browser | Playwright MCP in Claude Code; built-in browser in OMP. |
| Application | Running services or evidenced health/startup commands. |
| State Checks | Configured store and matching `psql`, `mysql`, `sqlite3` or `redis-cli`. Redis checks need `redis-cli` with `--json`; configured password authentication also needs `4.0.11+`. |
| Fixes | Code Review; test/report-only runs need no fixer. |

Check Perl support with `perl -MJSON::PP -e 1`. Extra commands used by sources and recipes must already be installed. A missing required tool produces a tool gap and never a fabricated PASS result; a missing store client skips only its own checks.

Testers never install tools or modify project files to repair setup. Supply tools outside the run and retry. Remove write-capable store MCP servers before testing untrusted code; configuration is not permission isolation.

## Oh My Pi

Install with `omp plugin install qa@av-marketplace code-review@av-marketplace`; update an existing marketplace registration first. QA commands remain `/qa:run` and `/qa:create-plan`. Code Review commands become `/code-review:fix QA-001` and `/code-review:fix-report`.

| Setting | QA choice |
|---|---|
| `browser.enabled` | Enable. |
| `browser.relay`, `browser.cmux` | Disable. |
| `browser.cdpUrl` | Unset, avoiding an attached personal browser. |
| `task.agentAdvisor` | Optional `{"qa:test-planner": "off"}` in `~/.omp/agent/config.yml` to disable the planner's Advisor. |

QA uses managed Chromium through OMP's built-in browser, not Playwright MCP. Disabled browser support produces tool gaps. Backend clients and sanitization match Claude Code.

Testers use the `tester` model role; planning/configuration use `plan`; review uses `advisor`. The main command uses the session model. See [Model roles](../oh-my-pi.md#model-roles) and [Advisor](../oh-my-pi.md#advisor).

Resolve installed paths with `realpath skill://qa:engine/scripts/qa.py` and `realpath skill://qa:be-testing/scripts/qa-redact.pl`. Never guess cache paths or substitute the project's own script.

OMP subagents inherit every session MCP server. Remove write-capable servers before testing untrusted code. Frontend fill calls still expose credentials; testers separate writes from observation and never replay a mutating cell.

<a id="upgrade-notes"></a>
## Upgrade Notes

### 3.1.0: delivery plans as the planner's contract source

For a `last N commits` source, the planner reads every distinct delivery plan named by Git-parsed `Delivery-Plan:` trailers and associates each commit with its own plan, only after checking repository containment and rejecting traversal, symlinks and non-files; ignored trailers are disclosed in `## Changes Summary`. Prose mentions do not count as trailers. Plan text is specification data, not instructions. Plans written by 3.0.0 stay valid.

### 3.0.0: flagless `/qa:run`, config and account provisioning

**Breaking cutover:** `/qa:run` now owns the complete loop; `/qa:loop` is removed with no alias. `/qa:create-plan` remains optional plan authoring/review. Run `/qa:run` interactively once for config bootstrap, then commit `.av/config.toml`; prepare/pin it explicitly for headless runners. There are no invocation flags or per-fix `step` mode.

3.0.0 moved every flag into `.av/config.toml`; [Configuration](../configuration.md) lists the current keys.

The old parsed Setup grammar (`Base URL`, `Required environment variables`, `Required databases`, `Required services`) is gone. Move those settings to `[env]`/`[qa]`; optional `## Setup` is human notes only. Plans written before 3.0.0 lack `Branch:`/`Head:`, so **pass their path once or regenerate** rather than expecting automatic branch reuse. Update credential tokens/targets and DB checks to the current config contract.

`auth-unverified` now applies only when the main flow lacks an engine-authenticated persona credential. Some former auth SKIPs become **FAIL flagged `auth`**, excluded from automatic fixing and visible for approval review. Fixers may not weaken auth. Headless default `approve` now tests/reports without fixing instead of requiring a TTY. Services may be brought up from trusted config, and accounts are freshly logged in before every tester dispatch. Config edits during a run stop it; run `/qa:run` again.

### Earlier releases (historical compatibility)

**`qa` 2.9.0:** In OMP, `qa:test-planner` runs with OMP's Advisor: a second model on the `advisor` role watches the planner while it writes the plan and can steer it, before `qa:test-plan-reviewer` reviews the result. Expect more model cost per plan. Switch it off with `task.agentAdvisor` (see [Oh My Pi](#oh-my-pi)). The Claude Code edition is unchanged.

**`qa` 2.6.0 pairs with `code-review` ≥ 2.0.0 wherever a shared report carries a decision-stage rejection.** That is the precondition, and it is worth stating plainly: `**Fix-policy:** needs-decision` is emitted by `code-review`'s own producers alone — today, reports written by `/review` — while `/qa:run` and `/qa:loop` never write the field, and an absent field is `auto` by both fix commands' fail-safe. A report this plugin produces therefore cannot presently reach the decision gate, and cannot acquire a `🚫 Rejected` status or any of the loop-written decision fields. `qa` 2.6.0's handling of them is **forward compatibility** for a schema the QA producers do not yet emit.

Where the state does arise — a `/review` report fed through the decision stage and then re-rendered by this plugin — the pairing binds: `code-review` 2.0.0 adds a `🚫 Rejected` status to reports it shares with this plugin, and `/qa:loop` on `qa` ≥ 2.6.0 knows to read it as terminal and preserve the line. An older `/qa:loop` (< 2.6.0) does not: its Step 4.1 in-place Status update overwrites a `🚫 Rejected` line and its reason whenever a sibling issue passes on the same scenario in a later iteration, silently discarding the rejection. So keep both plugins on paired minimums (`code-review` ≥ 2.0.0, `qa` ≥ 2.6.0) for any report that can carry a rejection. This is milder than `code-review`'s own intra-plugin skew — an older `code-review` reader can silently re-offer and dispatch a rejected finding, which is worse, and which is unconditional rather than waiting on a producer that does not exist yet. See [code-review.md's Upgrade Notes](code-review.md#upgrade-notes) for the fuller detail.
