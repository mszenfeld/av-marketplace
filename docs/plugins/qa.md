# QA Plugin

`/qa:run` takes a change from configuration through a reviewed test plan, disposable accounts, FE/BE testing and a bounded test → fix → retest loop. Reports use `QA-NNN` issue IDs that code-review's `/fix QA-001` and `/fix-report` understand.

**Version:** 4.0.0

## Quick start

Install QA and Code Review, then run this in the project checkout:

```text
/qa:run
```

On the first interactive run, QA reads repository evidence and proposes `.av/config.toml`: named target origins, service probes and commands, account recipes, and QA policy. It asks two questions: whether the data behind every target is disposable (`qa.mutations`) and how to handle fixes (`qa.fix`), then shows the config diff and complete trust subset for one **Apply and trust** confirmation. It recommends unrestricted mutations only when every target is loopback and you confirm that its data is disposable. The proposal does not run commands or read secret values.

After approval, QA writes the shared config and adds `.av/local.toml` and `.av/secrets.local.env` to `.gitignore` when needed. If a required private dotenv key is absent, it names the keys to fill and stops; do not paste their values into chat. Otherwise it reuses or generates a plan, checks the environment, provisions its personas, tests, and asks before each batch of source fixes by default.

**Commit `.av/config.toml` and the ignore entries.** Keep personal overrides in `.av/local.toml` and private inputs in `.av/secrets.local.env`, both uncommitted. See [Configuration](../configuration.md) for these shared files.

Later runs reuse those settings. They do **not** ask you to choose disposable-data or fix policy again, re-enter targets/accounts, export tokens, restart the harness, or approve plan generation. They can still ask about pre-existing tracked changes, each fix batch, a changed trust subset, a stale plan, missing config keys, a failed recipe/probe, service bring-up, a live run lock, open plan blockers or a one-time retry of setup gaps. Editing a trust-pinned setting requires approval of the new subset; merely changing another plugin's table does not.

## Commands

### `/qa:run [plan-path | change-source]`

The only executor. No `--` options are accepted; set policy in the config instead.

```text
/qa:run
/qa:run docs/testing/plans/2026-09-30-user-auth-test-plan.md
/qa:run #123
/qa:run feature/user-auth
/qa:run last 5 commits
/qa:run staged
```

An existing file argument selects that plan explicitly. Otherwise the argument is a change source for the planner. With no argument, the engine picks the newest plan in `docs/testing/plans/` whose `## Source` has the current `Branch:`. No branch match, or a detached HEAD, generates a new reviewed plan without a generation-confirmation question.

An automatically selected plan is **stale** if its `Head:` is missing/invalid, is no longer an ancestor of HEAD, or the intervening committed diff touches anything outside `docs/`. Interactive runs show `changed_files` and offer **Regenerate** (default) or **Use existing plan**; headless runs regenerate. An explicit file path bypasses automatic staleness selection, so inspect that plan yourself. Failed regeneration never falls back silently. Generated plans show their path and FE/BE counts; open review blockers require an interactive run-anyway decision, while headless runs stop. Concerns remain visible but do not stop execution.

#### Execution flow

1. **Config and trust:** validate the shared/local merge. Missing file or `[qa]` invokes bootstrap; invalid config stops with all file/key errors. New/changed trust is shown and approved before sources or recipes execute.
2. **Plan:** reuse, regenerate or author/review as above. A newly generated plan with no executable scenarios exits gracefully without testers; an existing empty plan is an error.
3. **Plan check:** derive personas, values, targets and DB needs from scenarios. Offer a scoped config extension for gaps, then re-check once. Off-target URLs stop the run rather than silently widening access.
4. **Working tree:** when fixes are enabled, the dirty-tree gate follows `qa.fix`: `approve` asks, `auto` proceeds with the recorded baseline. The baseline is captured before bootstrap writes, so QA's own config/ignore edits do not count as pre-existing dirt.
5. **Run start:** bind the effective config, acquire target-origin locks and create the private run directory. A live lock requires an approved takeover or a stop.
6. **Services:** check configured probes; offer `up` followed by `prepare` and a re-check when needed. `fix = "auto"` starts configured services without that question. Persistent failures can offer repair.
7. **Accounts and secrets:** provision only personas referenced by the plan and populate the private channel, even if there are zero personas. A create/confirm/login failure can offer repair.
8. **Baseline:** prepare FE/BE dispatches sequentially, refreshing logins before each; tester agents may then run in parallel. Save and ingest each answer. Remaining service/tool/fixture gaps offer one retry of affected whole sections, continue, or abort; headless continues with gaps visible.
9. **Report:** the engine assigns IDs and renders observed results and setup gaps. No failures means teardown without a fix iteration or final run.
10. **Fix iterations:** select eligible issues; approve one batch per iteration, or print the automatic scope banner. Fixers run sequentially, then independent testers re-run whole affected sections. Stop on no progress, regression/oscillation, plan drift or budget limits. Fixer self-reports are not proof.
11. **Final run:** after entering the iteration path, re-test every section. This pass counts toward usage but is not blocked by exhausted fix budgets. Only a whole-scenario PASS, including all edges, writes `**Status:** ✅ Fixed`. Zero-failure baselines, `fix = "off"`, headless `approve`, aborts and hard stops do not perform this pass.
12. **Teardown:** on every started-run exit, including errors, tear down accounts, refresh the report's Accounts line, stop only services this run started, show the summary, release locks and delete the private run directory. Stops flush partial observations without new Fixed status lines.

Changes remain uncommitted and unstaged. Recovery hints restore only the loop's own eligible tracked paths, never the whole tree. Files already dirty before the run and later edited by a fixer are reported as overlap and left for you to reconcile.

#### Config bootstrap: three modes

| Mode | Trigger | Scope |
|---|---|---|
| `create` | Missing `.av/config.toml` or its `[qa]` table | Add the QA table and only missing shared environment keys; preserve an existing plan's names. Ask the two policy questions. |
| `extend` | `plan check` reports missing keys/capabilities | Propose exactly those keys and their dependencies; do not reopen policy choices. |
| `repair` | An account recipe or service probe/lifecycle command fails | Propose a correction grounded in the engine error and repository evidence. |

`qa:config-author` is read-only. It cites repository evidence for targets, commands and recipes, never probes candidate commands, reads secret values, or edits `.av/local.toml`. The engine previews the full proposed diff and resulting trust subset; one confirmation approves both the write and that hash. Apply uses a snapshot of `.av/config.toml` and `.gitignore`: concurrent changes write nothing and require a new preview, not a silent retry. Other tables, unrelated keys and comments are preserved. A key supplied by a personal override blocks shared extension/repair and is named for you to resolve.

Bootstrap never creates/fills the secret file. One corrected author round is allowed after preview errors; remaining errors stop. A successful repair after run start first tears down the old run with its recorded config, then restarts from plan check with the same plan and original dirty baseline. It does not use repaired settings in the middle of an existing run. There is at most one repair per failure kind per invocation.

#### Interactive versus headless

Interactivity means the harness can deliver an ask-tool question, **not** that an agent's Bash stdin is a TTY. An unavailable/undeliverable question makes the rest of this invocation headless; no answer is invented. Loop-engineering's fail-closed TTY requirement (item 4) is **not met**: this command discloses and uses ask capability instead.

| Situation | Interactive | Headless |
|---|---|---|
| Missing config/table; missing plan-check keys; recipe repair | Propose, preview and ask | Stop with keys/error and config guidance |
| New or changed trust | Show complete masked subset; ask once | Stop; pre-review and pin config in controlled setup |
| No plan | Generate and review, no question | Same |
| Stale plan | Regenerate/use existing | Regenerate |
| Open plan blocker | Run anyway or stop | Stop |
| Dirty tree under `fix = "approve"` | Warn and ask | Abort |
| Live origin lock | Approved takeover or stop | Stop, naming holder |
| Services down, `up` configured | Ask, except under `fix = "auto"` | Start only under `auto`; otherwise stop |
| Baseline prerequisite gaps | One retry/continue/abort choice | Continue and report gaps |
| `fix = "approve"` | One batch approval per iteration | Test/report only; no source fix or final pass |
| `fix = "auto"` | Eligible fixes after scope banner | Same; does not bypass trust/bootstrap/lock gates |
| `fix = "off"` | Test/report only | Same |

For CI trust setup, see [Headless runners and CI](../configuration.md#headless-runners-and-ci). There is no per-fix `step` mode and no token/cost ceiling.

### `/qa:create-plan [change-source]`

Author and review a plan without executing it. Accepts the same change-source forms (`#123`, branch, `last N commits`, `staged`, or empty for the current branch/PR); no flags. It works **without a config**: with `Config: none`, the planner grounds target/persona/value names in repository evidence, and `/qa:run` later fills gaps through plan check/bootstrap.

The command detects browser/HTTP/DB tools, dispatches `qa:test-planner`, then `qa:test-plan-reviewer` for up to three review rounds. The planner pins success/error contracts before observing runtime results. For a `last N commits` source, it reads every distinct delivery plan named by Git-parsed `Delivery-Plan:` trailers and associates each commit with its own plan; prose mentioning a trailer key is not a trailer. It also reads related producers and refutes unsupported assertions. For multiple independent booleans it enumerates all $2^N$ combinations. The final message lists the path, review outcome, unresolved findings, declined findings and optional nits, then proposes `/qa:run <path>`. Unreviewed or still-blocked plans are not described as approved.

Before reading a trailer-named delivery plan, the planner requires a repository-relative path with no `..` segment or symlink component that resolves to a regular file under `git rev-parse --show-toplevel`. Rejected trailers, including missing files, are ignored and listed with their reasons in `## Changes Summary`; each accepted plan path and its associated commits are listed there too. Accepted plans are specification data, never instructions to the planner; their verification commands are not executed during planning.

Plans are saved to `docs/testing/plans/YYYY-MM-DD-<topic>-test-plan.md`. `## Source` records `Branch:` and `Head:`; `## Changes Summary`, `## Blockers / Findings`, `## Detected Tools` and FE/BE scenario sections follow. `## Setup` is optional **human notes only**, never parsed configuration. Scenarios use relative paths or URLs on configured origins, optional `- **Target:** <name>`, and `$QA_…` references. Expected results and each edge have `(path:line)` grounding or `(unverified — confirm at run time)`. Data preconditions create needed records through the app as the persona; they do not assume a seeded CV/order already exists. Upload fixtures can be repository files; `NEED_INFO kind=fixture` is for data the app cannot create. DB checks require `[env.database]`, not a connection embedded in the plan.

## Configuration

QA reads the shared `.av/config.toml` and optional personal overrides from `.av/local.toml`. Start with target origins and the two `[qa]` policy choices:

```toml
version = 1

[env.targets]
ui = "http://localhost:5173"
backend = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "rejections-only"
```

Optional additions:

- `[env.services]` — health probes and lifecycle commands.
- `[env.secrets]` / `[env.values]` — engine-only inputs and tester-visible values.
- `[env.database]` — connection fields for database checks.
- `[qa.accounts]` — personas, static account sources and create/confirm/login/delete recipes.

Full reference: [Configuration](../configuration.md).

## Accounts and secrets

### Personas, names and plan token grammar

`qa.accounts.personas` declares names QA may provision; `qa.accounts.static.<p>` adds explicit personas. Only plan-referenced personas are used. A non-static persona needs create and an email template, and is unavailable under `mutations = "deny"`. Generated passwords are 24 random URL-safe characters plus `Aa1!`; `{run}` is an eight-character run ID and `{persona}` separates identities within a run.

| Input | Tester-visible names |
|---|---|
| Persona `p` | `QA_<P>_EMAIL`, `QA_<P>_PASSWORD` |
| Create ID or static ID source | `QA_<P>_ID` |
| Login token | `QA_<P>_TOKEN` |
| Login cookies | `QA_<P>_COOKIE` (original names as `a=1; b=2`) and `QA_<P>_COOKIE_<NAME>` (individual values) |
| `[env.values]` entry `X` | `QA_<X>` |
| Postgres DB check | `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `PGPASSWORD` |
| MySQL DB check | `MYSQL_HOST`, `MYSQL_TCP_PORT`, `MYSQL_USER`, `MYSQL_DATABASE`, `MYSQL_PWD` |
| SQLite DB check | `SQLITE_DB` |
| `[env.secrets]` | Never exposed to testers |

Persona/value names uppercase for the channel. Cookie normalization uppercases, replaces each run of non-`A-Z0-9` characters with `_` and trims leading/trailing `_`: `__Host-session` → `HOST_SESSION`, `connect.sid` → `CONNECT_SID`. Colliding cookie names are config errors; the combined Cookie header retains original names.

The engine scans **every part of every scenario**, including headers, payloads, preconditions and edges, for `$QA_NAME` or `${QA_NAME}` with names matching `QA_[A-Z0-9_]+`. A configured persona plus `EMAIL`, `PASSWORD`, `ID`, `TOKEN`, `COOKIE` or `COOKIE_<NAME>` is a persona field; otherwise a configured value name is a value. Unknown names shaped like persona fields become `missing.personas`; other unknown names become `missing.values`. For example `$QA_API_TOKEN` means persona `api`'s token when `api` is configured, otherwise value `API_TOKEN` when that value exists. Capabilities matter: naming a persona does not create a TOKEN/ID/cookie capability without the corresponding recipe output/source. Plan check lists the reason; empty required runtime outputs stop before channel writes/tester launch.

### Private run channel

`${TMPDIR:-/tmp}/qa-run-<run-id>/` is mode **0700**. It holds:

- `secrets.env` and `secrets.json` (**0600**): only persona fields and exposed values the plan references, plus database names when DB checks are present. Shell values are quoted, not pasted into request argv.
- `load.sh` (**0600**): each request/DB Bash call begins `. '<run-dir>/load.sh' NAME... || exit 1`. It unsets inherited `QA_*`, `PG*`, `MYSQL_*` and `SQLITE_DB`, requires a readable regular owner-owned non-symlink `secrets.env`, sources it, restores allexport state, and fails naming any required empty value. It never prints values. Sanitiser bookkeeping uses validated lower-case shell variables after loading.
- `redact-names`: exposed names only, consumed by the installed fail-closed BE sanitiser.
- `accounts.private.json` (**0600**, **engine-private**): identities, generated passwords, tokens/cookies and issue times used by later accounts/dispatch processes. Neither orchestrator nor tester reads it. `run end` deletes it with the directory.
- `results/`: tester answers saved before ingest.
- `engine.log` (**0600**, **engine-private**): successful sources/services/command recipes retain only exit/line-count summaries, not stdout/stderr; failures retain at most 2048 characters after masking. Only values known to the writing process and at least four characters long are masked. Unresolved configured values, undeclared secrets and shorter values may remain in failure tails; never read this file into the transcript.

The channel is written even with zero personas. OMP FE JavaScript reads `secrets.json` with `Bun.file`, checking required names before any fill; it does not use `process.env` as the credential source. Sources are resolved only after trust and only when an operation needs them, including dependencies of login/delete recipes in each separate engine process. Engine-only administrative secrets never enter the tester files.

**A fresh login at every tester dispatch:** baseline, retry, iteration and final dispatches all re-login their section's personas before recording the assignment. Token/cookie entries are replaced atomically; other personas, values, DB names and `redact-names` survive. Engine calls are sequential even when testers run in parallel. A login failure stops rather than dispatching with a stale token. Refresh does not prevent expiry during a long-running dispatch.

### Teardown and the ledger

Before attempting create, QA records the identity as `pending` in `${XDG_STATE_HOME:-~/.local/state}/av-marketplace/qa-accounts.json`, keyed by repository realpath. The ledger survives plan/report changes, repair restarts and private-directory deletion. It contains persona, email, ID, owning run ID, delete-recipe hash, target origin and deletion/status metadata, **never passwords or tokens**. Only explicitly configured conflict statuses retry create once; other errors can leave an ambiguous pending record.

Teardown deletes accounts only while the current config is valid and trusted; it then uses the recorded recipe/targets for the current run's records and the current recipe for earlier records. It deletes only when the recipe hash matches **and** its target resolves to exactly the stored origin (scheme, lowercased host and effective port). Rebinding the same target name to another origin does not authorize deletion, even if the old origin is still listed elsewhere. Command deletes have no target (`origin = null`), so only their recipe hash is compared. Delete receives the record's own ID/run ID and no password. Static accounts are never enrolled for deletion.

- `deleted`: cleanup succeeded and is recorded.
- `left`: no delete recipe, changed/missing recipe or origin, a current config that is invalid or not trusted (account deletion needs a valid, trusted current config, even for the current run's recorded recipe), or a failed delete. Nothing is deleted blind.
- `unresolved`: deletion requires an ID that an attempted create did not return. The pending account may or may not exist; inspect the test stack before manual cleanup.

Review these outcomes in the report/summary; retaining a ledger record is not successful teardown. A later trusted run can retry compatible records. Normal `run end` removes private files; interrupted sessions that cannot finish cleanup leave them until the next run's cleanup of this repository's directories older than 24 hours.

### What reaches the transcript and artifacts

Names, target origins, account emails/IDs, sanitized results and cleanup outcomes are visible. Passwords, tokens and cookies are not printed by the engine. BE requests pass credentials via curl config on stdin; every response and DB result goes through the installed `qa-redact.pl` before inspection/persistence. Missing Perl/`JSON::PP`, missing names file, failed capture or failed redaction fails closed; a write is never replayed to obtain an artifact. Sensitive headers/JSON fields, bearer tokens, sensitive URL parameters and declared values are masked; non-JSON/bare-string bodies are withheld. Ordinary URL-named fields can retain scheme/host/path with userinfo/query/fragment masked; one-time-link fields remain fully masked. Do not assume a visible URL path is secret-free.

**FE fill calls can expose credentials in the harness transcript**, and snapshots/screenshots can show user data. Use disposable, non-privileged accounts. Before a failure screenshot the tester inspects a fresh snapshot; debug pages or an uninspectable page suppress screenshot/debug-text capture, retaining only a sanitized URL/status and generic title. A screenshot path is reported only after its file exists. Keep `docs/testing/reports/screenshots/` and `docs/testing/reports/responses/` out of version control and review artifacts before sharing them.

## Safety

- **Exact origins:** relative paths use the scenario/default target. Absolute URLs must match a configured origin including scheme and effective port, not just host; otherwise `SKIP — off-target URL refused: <origin>`. Userinfo is always refused; redirects are never followed automatically. Off-target plan URLs stop at plan check. Non-loopback targets require trust.
- **Mutation policy:** `allow` guards nothing and is meant for disposable data. `deny` guards every detected write and forbids provisioning. `rejections-only` exempts a state-changing BE scenario only when the main and each edge assert exactly one standalone HTTP status ≥ 400, every assertion is grounded, any DB check is read-only and there are no other write steps/preconditions. Missing/ambiguous statuses, unverified assertions, 1xx–3xx or writes elsewhere retain the guard. Guarded scenarios are SKIP, not fix candidates. Unexpected success on an exempt request can write once: disposability still matters.
- **Trust and run-bound config:** pins live outside the repository, per realpath/plugin, and hash full unmasked canonical settings. Sources, all recipes, widened gates, shared services and non-loopback targets/DB hosts cannot change silently. A started run binds the **entire effective config**, including non-pinned keys. Accounts/services/dispatch drift checks stop with `config changed during run`; cleanup uses recorded settings, but deletes accounts only while the current config is valid and trusted, so an edit to a pinned key, or one that makes the config invalid, leaves the run's accounts `left` for a later compatible trusted teardown while `services down` still runs. Source definitions are pinned, not mutable command outputs or dotenv contents.
- **Source restrictions:** committed `[env]` `env:` names must begin `AV_`; QA sources permit `AV_` or `QA_`. Committed `file:` paths must be repository-relative and git-ignored, and must not escape the repository. Committed secret/static-password literals are forbidden; a DB-password literal is allowed only for loopback. Personal overrides can use arbitrary environment names, absolute file sources and literals. See [Secrets and values](../configuration.md#secrets-and-values) for the rules. Prefer a private file or `cmd:` source instead of exporting into a running harness: `env:` sees only its startup environment.
- **Auth rule:** a BE main flow expecting 2xx but getting 401/403 is a **FAIL flagged `auth`** if it sends a credential of a persona authenticated by this dispatch. Recognized credentials are that persona's TOKEN/COOKIE/COOKIE_<NAME>, or EMAIL/PASSWORD with a login recipe. It is **never auto-fixed** under `fix = "auto"`; `approve` can offer it only with its auth flag and human approval. A fixer may never weaken authentication or authorization to make a scenario pass. Without an engine-authenticated persona credential the main flow is `auth-unverified`, not proof of a feature defect; expected 401/403 enforcement remains ordinary testing.
- **Fix guards and authority:** rejected, location-less, incomplete or ambiguously mapped issues never dispatch. Automatic mode excludes auth/unverified assertions per issue, without blocking grounded sibling issues. Payload-literal anti-hardcoding warnings are heuristic and non-blocking. Only independent tester evidence and the final whole-scenario PASS can credit a fix; a preserved `🚫 Rejected` line and its reason are never overwritten.

### Residual risks

Mutation classification is static/best-effort: GET side effects, GraphQL mutations without explicit verbs and implicit FE submit/click writes can escape it. Config shell/commands and the target app are executable code, not a sandbox; a trust pin cannot make an untrusted branch safe. A fixer seeing deterministic scenarios can game verification despite the warning/default approval gate. Cross-section regressions may surface only in the final pass.

Session logins needing CSRF round trips require command recipes; HTTP recipes handle one request. Signup rate limits, CAPTCHA and mandatory email confirmation without a local mail catcher can block provisioning; use an evidenced command or static account. UI-only signup has no built-in recipe. Private temp files remain during the run and may outlive an interrupted session. FE transcript exposure and token-bearing URL paths remain risks.

Auth classification covers engine-authenticated BE personas, not arbitrary scenario-owned login, 2xx-shaped gating, tenant-shaped 404s or FE gating. Tokens can expire mid-dispatch. Config drift is checked at engine calls, so an already-dispatched tester can finish on old settings. Origin locks exclude overlapping target sets, but disjoint origins can still share a database. Tester adherence to the structured result block is prompt-level; invalid/missing output degrades to `cannot-confirm`, never PASS.

## Coverage honesty

The summary reports what was **exercised**, not just whether failures remained. It retains the coverage-honesty behavior introduced in 2.3.0:

```markdown
## Coverage
- Exercised: <N> feature · <M> sanity · <K> enforcement
- Not verified: auth-unverified <N> · need-info <M> · mutation-guard SKIP <K> · tool-unavailable <J> · …
- Confidence: high | low — <reason>
```

A feature PASS is an upper bound on verification, not proof of complete behavior. Scenario verdict precedence is failure → missing prerequisite → auth-unverified → skip → pass, aggregating main flow and every edge. A skipped DB check alone does not downgrade the HTTP result. An independent edge failure can win over an auth-gated main flow and retain its own fix eligibility.

**Shallow-coverage warning:** when feature scenarios exist but none passes, the summary warns that a green result reflects infrastructure/enforcement checks only. This applies to every fix policy and both existing and generated plans. A mutation-guard-only all-SKIP generated plan has its own graceful message; zero feature scenarios do not trigger this warning.

**Low-confidence green:** on a zero-failure exit with shallow coverage for an auto-generated plan, the summary says the plan may not reflect runtime auth/setup. An existing plan instead retains the no-failing-assertions wording alongside Coverage. An all-SKIP/NEED_INFO existing plan is `Stopped` (`No executable verifier — cannot gate`), not success. An all-SKIP/NEED_INFO generated plan can exit gracefully, distinguishing mutation-guard-only coverage from setup/tooling/parse gaps with a coverage-zero warning.

**Auth-unverified:** without an engine-authenticated persona credential, a 401/403 instead of expected 2xx means the feature path was gated, not exercised. These main-flow issues never dispatch to fix-auto, never count as PASS or regressions, and are presented in Skip counts and Not verified. A persona credential authenticated by the engine changes that outcome to FAIL as described under Safety.

**Unlock hints** name what to change, not secret values: adjust `qa.mutations` only for disposable data; add the persona's TOKEN/cookie and login recipe for authenticated coverage; fill the missing config key/start the evidenced service; enable the missing tool; or re-run `/qa:run` when the dispatch limit is exhausted. No harness restart is needed for the private file channel. Broad transport/service failures suggest checking reachability without claiming an app crash.

Assertions tagged `(unverified — confirm at run time)` are flagged for approval review and excluded from automatic fixes, per issue. Grounded sibling failures stay eligible. The engine summary always carries `**Result:** Pass`, `Fail`, `Budget Exhausted` or `Stopped`; a stop/budget exhaustion is not success, and Pass is never a claim of full coverage.

## Reports and Code Review

Reports live in `docs/testing/reports/YYYY-MM-DD-<topic>-report.md`. The engine renders Summary counts, Accounts (`provisioned|static`, `deleted|left`), conditional `## Setup gaps`, detailed results and one Loop History row per iteration. Only failed assertions mint issues; service/tool/fixture gaps do not become app defects.

Issue blocks use `### [SEVERITY] QA-NNN: Title`, `**ID:**`, `**Location:**`, `**Category:** Testing`, `**Problem:**` and `**Remediation:**`, plus scenario/response/screenshot context. The engine carries forward Status, decision records and corrected Location by QA token. Only final whole-scenario PASS writes Fixed; a partial pass leaves issues open. Mechanical severity is CRITICAL for observed HTTP ≥ 500 or crash, otherwise LOW for unverified assertions; other severities follow evidence, with CRITICAL security-bypass/data-loss requiring an explicit reason.

With Code Review installed, `/fix QA-001` reads the newest QA report; `/fix-report` without a path merges the newest review and QA reports and writes statuses back to their original files. `/fix-report docs/testing/reports/<file>.md` selects one report. See [Code Review](code-review.md) for routing and decision handling.

Delivery 0.6.0 and later runs `/qa:run last <N> commits` over a delivered change before its final code review and commits the fixes, configuration, plan and report QA produces; see the [Delivery guide](delivery.md#qa).

## Engine

The stdlib-only CLI at `plugins/qa/skills/engine/scripts/qa.py` owns config/trust, plan checks, origins/mutations, services/accounts, private state, dispatch counters, verdicts, QA IDs, candidates, stop decisions and report rendering. The model owns planning, test execution, fixes and sanitized issue prose. Neither the orchestrator nor users should patch the sidecar/report to manufacture results.

The CLI registers handlers on its argument subparsers, with one shared executor for locked run-state operations. Repository-option registration is centralized in `repo_option`, so `--repo` keeps the same behavior before and after subcommands.

The durable sidecar remains `docs/testing/reports/<topic>-loop-state.json`; the engine detects plan hash changes, reuses/adopts compatible report state and rebaselines changed plans while preserving report metadata. Account cleanup is independent in the external ledger. CLI commands include `config`, `trust accept`, `config preview|apply`, `tools`, `plan resolve|check`, `run start|stop|end`, `services check|up|prepare|down`, `accounts provision|refresh|teardown`, `dispatch`, `ingest`, `issues`, `candidates`, `fix done`, `iteration open|close`, `report` and `summary`.

Internally, `qa_engine/runs.py` owns lifecycle and locked state access, `locks.py` owns target-origin locks, `dispatch.py` owns tester/fixer bookkeeping, `results.py` owns `qa-results` block ingestion, and `verdicts.py` owns pure evaluation. `issues.py` and `candidates.py` own QA IDs and fix eligibility; `iterations.py` owns loop decisions and recovery; `sidecar.py` owns reuse, adoption and rebaselining. Durable record types and their JSON validators remain together in `schema.py`; `models.py` supplies the run handle without importing lifecycle commands. HTTP and command account recipes have separate validators and executors. Account creation owns the pending ledger entry, the single conflict retry and confirmation; provisioning owns persona selection and the private channel. Report-prose validation uses an ordered rule table, keeping the existing validation precedence, error messages and CLI shapes. The `config` JSON contract now returns `policy{fix, mutations}` and derived `defaults{FE, BE}`, without `budget`; Python consumers import from the owning modules rather than the removed `state.py` or `av_config.py` APIs.

Fix eligibility shares one `CandidateContext` (state, assertion claims and report blocks) across the issues in a selection pass. Run creation takes `RunStartOptions` for takeover, provenance and baseline choices, then passes the run identity and baseline to sidecar preparation as `SidecarContext`. These are internal parameter objects.

`models.py` is a leaf handle/error module: it depends only on `files.py` and `schema.py`, not plan parsing, config, accounts, services or lifecycle commands, including type-only imports. Consumers load a run's plan through `plan.run_plan`, which preserves strict hash checks by default and permits non-strict reads for stop reports and summaries. Path display lives with plan fingerprints in `files.py`, keeping the handle out of the report/plan dependency graph.

`qa_engine/common.py` is the shared boundary for report blocks/fields, assertion keys and lookup, sanitized origin formatting and JSON artifact I/O. Both fix eligibility and report rendering read Status/Location/decision metadata only before Category, accepting bare or bulleted field lines; prose fields remain readable across the issue body. Origins always include their port and bracket IPv6 hosts. `config.DATABASE_NAMES` maps tester database variables to config fields for exposure, provisioning and unlock hints. Git subprocesses share one bounded runner in `git.py`; trust storage and origin locks share `av_config.files.default_state_home`.

See the [engine skill](../../plugins/qa/skills/engine/SKILL.md) for installed-path resolution, every command/JSON shape and the result protocol. Every subcommand accepts internal `--repo <root>`; these are **not `/qa:run` flags**. Stdout is one JSON object except successful `summary` (Markdown). Exit 0 means the operation completed, 1 is a domain stop/gap, and 2 is usage/invalid config. `config` can exit 0 with a missing-file/table state; that is not a configured run.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `plan check` lists missing personas/fields | Add the persona/static entry and evidenced create/login/ID/cookie output required by its token. Provisioning is unavailable under `deny`; choose static accounts or an appropriate disposable policy. Interactive `extend` proposes just the gaps. |
| Missing values, targets or database | Add `[env.values]` sources, `[env.targets]` named `ui`/`backend`, or `[env.database]`. Fill named private dotenv keys locally. Do not restore Setup declarations. |
| Off-target URL | Correct the scenario or explicitly configure/trust the intended origin. A host match with a different scheme/port is still off-target. |
| Account create/confirm/login recipe fails | Inspect the safe status/key error, route/extractor/confirmation assumptions and local stack. Interactive `repair` offers one correction per failure kind, followed by cleanup/restart; headless stops. CAPTCHA/rate limits may need static accounts or a command recipe. |
| Local override blocks extension/repair | Resolve/remove the named key in your own `.av/local.toml`, then re-run. Bootstrap cannot override or edit your personal file. |
| New/changed trust prompt | Review the complete masked subset, including existing shared commands, not just the latest diff. Approve only the shown hash; changed files/another worktree need a fresh pin. Headless cannot consent. |
| Stale plan | Regenerate (default), or deliberately use it after review. Headless regenerates. Passing a path chooses it explicitly; old plans can lack Branch/Head metadata. |
| Live lock from another run | Stop the other active run, or approve takeover only if its session was interrupted. The prompt names the holder/start time. Headless refuses takeover; do not delete locks or the account ledger blindly. |
| Services down | Check failing probes, correct ports and checkout. Approve evidenced `up`/`prepare`, or start the stack yourself. If still down, use interactive repair; a 5xx health response is down, a responding status below 500 is up. |
| `cannot-confirm` | The tester omitted/malformed its `qa-results` block, section, assigned scenario or edge. Re-run the affected section/run with usable tools and complete results; missing output is never a retained earlier PASS. |
| `config changed during run` | The effective config drifted. QA stops and cleans up using recorded settings: `services down` still runs, but accounts are deleted only if the current config is valid and trusted, otherwise they stay `left` for a later compatible trusted teardown; **re-run `/qa:run`** to validate, trust, check and lock the new config. Do not continue the existing run or edit its sidecar. |
| Accounts `left` | Review ledger identities and missing/changed delete recipe, origin/trust or delete error. A later compatible trusted teardown can retry; otherwise clean up deliberately in the correct test stack. |
| Accounts `unresolved` | A pending create did not return the ID deletion needs. Inspect whether it created anything; recover the identity safely/manual-clean up, never guess an ID or delete on another origin. |
| FAIL flagged `auth` | This persona logged in for the dispatch yet expected 2xx got 401/403. Inspect auth/authorization, token lifetime and intended role. Auto drops it; approve only a real root-cause fix, never weakening auth. |
| No fixes in headless `approve` or under `off` | Expected test/report-only behavior. Set/trust `qa.fix = "auto"` only when automatic eligible source edits are intended. |
| Budget exhausted or no-progress stop | Read Loop History and the named limit/stop reason. The limits are fixed (3 iterations, 50 dispatches, 30 minutes); investigate the remaining issue, then start a new run. Do not reset counters by hand. |

## Prerequisites

- **Python ≥ 3.11 as `python3`** for the engine and TOML parser. Older versions exit with an explicit version error. No engine dependencies are installed.
- **Git** and a repository with a HEAD for branch/plan resolution and tracked-change recovery.
- **curl** for credential-bearing BE requests and fail-closed capture; HTTPie is only a credential-free alternative. **Perl with `JSON::PP`** for the installed BE sanitiser (`perl -MJSON::PP -e 1`). Missing required HTTP/browser/sanitiser tools yield `NEED_INFO kind=tool`, not fabricated PASS/SKIP.
- **Playwright MCP** for FE tests in Claude Code; OMP uses its built-in browser instead.
- **A running app or evidenced `[env.services]` commands/probes** for managed bring-up. QA can start/prepare the config's services after its gate and stops only what it started.
- **`[env.database]` and the matching `psql`, `mysql` or `sqlite3` client** for DB checks. An unavailable client skips only the DB check, not runnable HTTP assertions. Available MCP servers do not establish a test-DB binding; remove write-capable DB MCP servers from the session. Tool declarations/instructions are not tool-grant isolation.
- **Code Review** for `code-review:fix-auto` when fixes are enabled; test/report-only runs do not require a fixer. Extra commands named by your sources/recipes (for example Supabase CLI and `jq`) must already be available.

Testers do not install browsers/drivers/packages or change the project to repair a tool probe. Tester-authored files are limited to report artifacts and temp files. Supply tools outside the run and retry.

## Oh My Pi

Install with `omp plugin install qa@av-marketplace code-review@av-marketplace`; update the marketplace first if it was already added. Commands are `/qa:run` and `/qa:create-plan`. `/qa:run` requires Code Review for its fix path; `/fix QA-001` and `/fix-report` are `/code-review:fix QA-001` and `/code-review:fix-report` in OMP.

`qa:fe-tester` and `qa:be-tester` use the `tester` model role. The test planner and read-only config author use `plan`; the test-plan reviewer uses `advisor`. The main command stays on the session model. `qa:test-planner` also has OMP's Advisor watching its drafting turns; disable it with `task.agentAdvisor: {"qa:test-planner": "off"}` in `~/.omp/agent/config.yml`. See [Model roles](../oh-my-pi.md#model-roles) and [Advisor](../oh-my-pi.md#advisor) for mappings and fallbacks.

Resolve the installed engine with `realpath skill://qa:engine/scripts/qa.py` and BE sanitiser with `realpath skill://qa:be-testing/scripts/qa-redact.pl`; do not guess paths under `~/.omp` or run a project's own script. A missing sanitiser stops requests with `NEED_INFO kind=tool`.

FE uses `eval`'s `browser` global in a JavaScript/Bun cell, not Playwright MCP. Enable `browser.enabled`; for QA disable `browser.relay` and `browser.cmux` and unset `browser.cdpUrl` so it does not use your own/attached browser. Managed Chromium is the fallback only when those alternatives are not selected. With browser enabled, OMP removes Playwright MCP servers; with it disabled, FE returns NEED_INFO/tool. Credentials come from the private `secrets.json`, not the process environment, but fill arguments/snapshots can still expose them in the transcript. The tester ends a cell immediately after a submit/write-triggering click and reads resulting state in a new cell; it never re-runs a mutating cell.

OMP subagents inherit every session MCP server regardless of their `tools:` list. Remove write-capable servers before testing untrusted code; config/plan declarations are policy, not permission isolation. BE uses the same CLI clients and redaction as Claude Code.

<a id="upgrade-notes"></a>
## Upgrade Notes

### 4.0.0: minimal configuration

Configuration starts with `[qa]` containing only `fix` and `mutations`; section origins are derived from target names rather than configured separately.

| 3.x setting | 4.0.0 replacement | Rule |
|---|---|---|
| `[qa.policy] fix` | `qa.fix` | `approve` (default), `auto` or `off`; unchanged semantics. |
| `[qa.policy] mutations` + `disposable_data` | `qa.mutations` | `allow`, `rejections-only` (default) or `deny`; `allow` alone means the data is disposable. |
| `[qa.policy] dirty_tree` | Removed; derived from `qa.fix` | `approve` warns and asks (headless aborts); `auto` proceeds with the recorded baseline; `off` skips the check. |
| `[qa.policy] min_severity` | Removed | Every failing assertion is a fix candidate. |
| `[qa.budget] iterations/dispatches/minutes` | Removed | Fixed engine limits: 3 iterations, 50 tester/fixer dispatches, 30 minutes. |
| `[qa.defaults] be_target/fe_target` | Removed | FE uses `ui`, BE uses `backend`; a missing reserved name falls back to the other one, and a single target of any name serves both. Otherwise `plan check` names the missing reserved origin. |
| `env.source_env`, `env.services.env`, command-recipe `env` | Removed | Commands get the engine's inherited environment only; account commands additionally get `QA_PERSONA`, `QA_EMAIL`, `QA_PASSWORD`, `QA_ID`. |
| `qa.accounts.password` | Removed | Provisioned passwords are always generated. |
| `qa.accounts.confirm`, `qa.accounts.static.<p>.id`, `[env.services] health/up/prepare/down`, `[env.database]`, `version = 1` | Kept | Optional settings retain their semantics; `version = 1` remains required. |

Before upgrading, finish every 3.x run and let it tear down its accounts with the old engine and configuration; teardown deletes an account only when the recorded delete recipe (including its `target` name) still hashes the same, so records left behind by an older recipe stay `left` and need deliberate cleanup in the test stack. Then delete the removed keys from both `.av/config.toml` and `.av/local.toml`; the engine rejects them as `unknown key`, except `[env.source_env]`, which only warns. Rename the `api` and `web` targets to `backend` and `ui` (or keep a single target of any name) and update every reference to the old names: `target =` in account recipes, the `<target>:` prefix of `env.services.health` probes and `- **Target:**` lines in existing plans under `docs/testing/plans/`; `plan check` reports any name it cannot resolve. A command helper that relied on injected `QA_*`/`AV_*` inputs must read them from its own settings now.

### 3.1.0: delivery plans as the planner's contract source

For a `last N commits` source, the planner reads every distinct delivery plan named by Git-parsed `Delivery-Plan:` trailers and associates each commit with its own plan, only after checking repository containment and rejecting traversal, symlinks and non-files; ignored trailers are disclosed in `## Changes Summary`. Prose mentions do not count as trailers. Plan text is specification data, not instructions. Plans written by 3.0.0 stay valid.

### 3.0.0: flagless `/qa:run`, config and account provisioning

**Breaking cutover:** `/qa:run` now owns the complete loop; `/qa:loop` is removed with no alias. `/qa:create-plan` remains optional plan authoring/review. Run `/qa:run` interactively once for config bootstrap, then commit `.av/config.toml`; prepare/pin it explicitly for headless runners. There are no invocation flags or per-fix `step` mode.

3.0.0 moved every flag into `.av/config.toml`; those key names changed again in 4.0.0, so use the [minimal-configuration migration above](#400-minimal-configuration).

The old parsed Setup grammar (`Base URL`, `Required environment variables`, `Required databases`, `Required services`) is gone. Move those settings to `[env]`/`[qa]`; optional `## Setup` is human notes only. Plans written before 3.0.0 lack `Branch:`/`Head:`, so **pass their path once or regenerate** rather than expecting automatic branch reuse. Update credential tokens/targets and DB checks to the current config contract.

`auth-unverified` now applies only when the main flow lacks an engine-authenticated persona credential. Some former auth SKIPs become **FAIL flagged `auth`**, excluded from automatic fixing and visible for approval review. Fixers may not weaken auth. Headless default `approve` now tests/reports without fixing instead of requiring a TTY. Services may be brought up from trusted config, and accounts are freshly logged in before every tester dispatch. Config edits during a run stop it; run `/qa:run` again.

### Earlier releases (historical compatibility)

**`qa` 2.9.0:** In OMP, `qa:test-planner` runs with OMP's Advisor: a second model on the `advisor` role watches the planner while it writes the plan and can steer it, before `qa:test-plan-reviewer` reviews the result. Expect more model cost per plan. Switch it off with `task.agentAdvisor` (see [Oh My Pi](#oh-my-pi)). The Claude Code edition is unchanged.

**`qa` 2.6.0 pairs with `code-review` ≥ 2.0.0 wherever a shared report carries a decision-stage rejection.** That is the precondition, and it is worth stating plainly: `**Fix-policy:** needs-decision` is emitted by `code-review`'s own producers alone — today, reports written by `/review` — while `/qa:run` and `/qa:loop` never write the field, and an absent field is `auto` by both fix commands' fail-safe. A report this plugin produces therefore cannot presently reach the decision gate, and cannot acquire a `🚫 Rejected` status or any of the loop-written decision fields. `qa` 2.6.0's handling of them is **forward compatibility** for a schema the QA producers do not yet emit.

Where the state does arise — a `/review` report fed through the decision stage and then re-rendered by this plugin — the pairing binds: `code-review` 2.0.0 adds a `🚫 Rejected` status to reports it shares with this plugin, and `/qa:loop` on `qa` ≥ 2.6.0 knows to read it as terminal and preserve the line. An older `/qa:loop` (< 2.6.0) does not: its Step 4.1 in-place Status update overwrites a `🚫 Rejected` line and its reason whenever a sibling issue passes on the same scenario in a later iteration, silently discarding the rejection. So keep both plugins on paired minimums (`code-review` ≥ 2.0.0, `qa` ≥ 2.6.0) for any report that can carry a rejection. This is milder than `code-review`'s own intra-plugin skew — an older `code-review` reader can silently re-offer and dispatch a rejected finding, which is worse, and which is unconditional rather than waiting on a producer that does not exist yet. See [code-review.md's Upgrade Notes](code-review.md#upgrade-notes) for the fuller detail.
