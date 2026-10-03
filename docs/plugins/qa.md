# QA Plugin

`/qa:run` takes a change from configuration through a reviewed test plan, configured users and tester-registered accounts, FE/BE testing and a bounded test → fix → retest loop. Reports use `QA-NNN` issue IDs that code-review's `/fix QA-001` and `/fix-report` understand.

**Version:** 4.0.0

## Quick start

Install QA and Code Review, then run this in the project checkout:

```text
/qa:run
```

On the first interactive run, QA reads repository evidence and proposes `.av/config.toml`: named target origins, service probes and commands, stores, existing users, a cleanup recipe, and QA policy. It asks three questions: whether the data behind every target is disposable (`qa.mutations`), how to handle fixes (`qa.fix`), and whether to start configured services automatically (`qa.start_services`), then shows the config diff and complete trust subset for one **Apply and trust** confirmation. It recommends `mutations = "allow"` only when every target is loopback and you confirm that its data is disposable; otherwise it proposes `"deny"`. The proposal does not run commands or read secret values.

After approval, QA writes the shared config and adds `.av/local.toml` and `.av/secrets.local.env` to `.gitignore` when needed. If a required private dotenv key is absent, it names the keys to fill and stops; do not paste their values into chat. Otherwise it reuses or generates a plan, checks the environment, provisions the private user/value channel, tests, and asks before each batch of source fixes by default. Testers log in and register accounts themselves.

**Commit `.av/config.toml` and the ignore entries.** Keep personal overrides in `.av/local.toml` and private inputs in `.av/secrets.local.env`, both uncommitted. See [Configuration](../configuration.md) for these shared files.

Later runs reuse those settings. They do **not** ask you to choose disposable-data, fix or service-start policy again, re-enter targets/users/stores, export tokens, restart the harness, or approve plan generation. They can still ask about pre-existing tracked changes, each fix batch, a changed trust subset, a stale plan, missing config keys, a failed service probe/command, service bring-up, a live run lock, open plan blockers or a one-time retry of setup gaps. Editing a trust-pinned setting requires approval of the new subset; merely changing another plugin's table does not.

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
3. **Plan check:** derive users, registrations, values, targets and stores from scenarios. Offer a scoped config extension for required gaps, then re-check once. `plan_errors` and `off_target` stop with their diagnostics rather than silently widening access. Missing cleanup alone offers a cleanup-only extension and otherwise continues with a warning.
4. **Working tree:** when fixes are enabled, the dirty-tree gate follows `qa.fix`: `approve` asks, `auto` proceeds with the recorded baseline. The baseline is captured before bootstrap writes, so QA's own config/ignore edits do not count as pre-existing dirt.
5. **Run start:** bind the effective config, acquire target-origin and store-endpoint locks and create the private run directory. A live lock requires an approved takeover or a stop.
6. **Services:** check configured probes. If bring-up is needed, `start_services = "ask"` asks once (headless stops); `"auto"` prints the scope and runs `up`, then `prepare`, then re-checks. Preparation runs only after this run's `up`, not on an already-running stack. Persistent failures can offer repair.
7. **Users and secrets:** always run `users provision --run <run>` to populate the private channel with referenced existing user fields, values, store variables and `QA_NEW_PASSWORD`, even with zero users. Missing `file:.av/secrets.local.env#NAME` inputs name the keys to fill and stop; the engine does not attempt or repair logins.
8. **Baseline:** prepare FE/BE dispatches sequentially, issuing each a tag and private capture files; tester agents may then run in parallel. Testers log in/register in preconditions and immediately record successful registrations. Save and ingest each answer. Remaining service/tool/fixture gaps offer one retry of affected whole sections, continue, or abort; headless continues with gaps visible.
9. **Report:** the engine assigns IDs and renders observed results and setup gaps. No failures means teardown without a fix iteration or final run.
10. **Fix iterations:** select eligible issues; approve one batch per iteration, or print the automatic scope banner. Fixers run sequentially, then independent testers re-run whole affected sections. Stop on no progress, regression/oscillation, plan drift or budget limits. Fixer self-reports are not proof.
11. **Final run:** after entering the iteration path, re-test every section. This pass counts toward usage but is not blocked by exhausted fix budgets. Only a whole-scenario PASS, including all edges, writes `**Status:** ✅ Fixed`. Zero-failure baselines, `fix = "off"`, headless `approve`, aborts and hard stops do not perform this pass.
12. **Teardown:** on every started-run exit, including errors, run `users teardown --run <run>` and show `deleted`, `left` and `manual` emails, update the report's Accounts line, stop only services this run started, show the summary, release locks and delete the private run directory. Stops flush partial observations without new Fixed status lines.

Changes remain uncommitted and unstaged. Recovery hints restore only the loop's own eligible tracked paths, never the whole tree. Files already dirty before the run and later edited by a fixer are reported as overlap and left for you to reconcile.

#### Config bootstrap: three modes

| Mode | Trigger | Scope |
|---|---|---|
| `create` | Missing `.av/config.toml` or its `[qa]` table | Add the QA table and only missing shared environment keys; preserve an existing plan's names. Ask the three policy questions. |
| `extend` | `plan check` reports missing users, values, targets, stores or cleanup | Propose exactly those keys and their dependencies; do not reopen policy choices. Cleanup is a soft gap. |
| `repair` | A service probe/lifecycle command fails | Propose a correction grounded in the engine error and repository evidence. |

`qa:config-author` is read-only. It cites repository evidence for targets, commands and recipes, never probes candidate commands, reads secret values, or edits `.av/local.toml`. The engine previews the full proposed diff and resulting trust subset; one confirmation approves both the write and that hash. Apply uses a snapshot of `.av/config.toml` and `.gitignore`: concurrent changes write nothing and require a new preview, not a silent retry. Other tables, unrelated keys and comments are preserved. A key supplied by a personal override blocks shared extension/repair and is named for you to resolve.

Bootstrap never creates/fills the secret file. One corrected author round is allowed after preview errors; remaining errors stop for required configuration. **Cleanup-only extension is different:** if the author returns `proposal: null`, preview still fails after the single retry, the gate is declined, or the run is headless, keep the existing valid config, print `No cleanup recipe: registered accounts will remain in the application.` and continue. An invalid config or required gap still stops. A successful service repair after run start first tears down the old run, then restarts from plan check with the same plan and original dirty baseline. It does not use repaired settings in the middle of an existing run. There is at most one repair per failure kind per invocation.

#### Interactive versus headless

Interactivity means the harness can deliver an ask-tool question, **not** that an agent's Bash stdin is a TTY. An unavailable/undeliverable question makes the rest of this invocation headless; no answer is invented. Loop-engineering's fail-closed TTY requirement (item 4) is **not met**: this command discloses and uses ask capability instead.

| Situation | Interactive | Headless |
|---|---|---|
| Missing config/table; required plan-check keys; service repair | Propose, preview and ask | Stop with keys/error and config guidance |
| Missing cleanup only | One cleanup-only extension; continue with warning if unavailable or declined | Continue with warning; registered accounts remain |
| New or changed trust | Show complete masked subset; ask once | Stop; pre-review and pin config in controlled setup |
| No plan | Generate and review, no question | Same |
| Stale plan | Regenerate/use existing | Regenerate |
| Open plan blocker | Run anyway or stop | Stop |
| Dirty tree under `fix = "approve"` | Warn and ask | Abort |
| Live target/store lock | Approved takeover or stop | Stop, naming holder |
| Services down, `up` configured, `start_services = "ask"` | Ask once before `up`/`prepare` | Stop; start the stack outside QA or pre-approve `"auto"` |
| Services down, `start_services = "auto"` | Print scope and run configured `up`/`prepare` | Same; other gates still apply |
| Registration or other writes under `mutations = "deny"` | Guard the scenario as SKIP | Same; no approval bypass |
| Baseline prerequisite gaps | One retry/continue/abort choice | Continue and report gaps |
| `fix = "approve"` | One batch approval per iteration | Test/report only; no source fix or final pass |
| `fix = "auto"` | Eligible fixes after scope banner | Same; does not bypass trust/bootstrap/lock gates |
| `fix = "off"` | Test/report only | Same |

For CI trust setup, see [Headless runners and CI](../configuration.md#headless-runners-and-ci). There is no per-fix `step` mode and no token/cost ceiling.

### `/qa:create-plan [change-source]`

Author and review a plan without executing it. Accepts the same change-source forms (`#123`, branch, `last N commits`, `staged`, or empty for the current branch/PR); no flags. It works **without a config**: with `Config: none`, the planner grounds target/user/value/store names in repository evidence, and `/qa:run` later fills gaps through plan check/bootstrap.

The command detects browser/HTTP/DB tools, dispatches `qa:test-planner`, then `qa:test-plan-reviewer` for up to three review rounds. The planner pins success/error contracts before observing runtime results. For a `last N commits` source, it reads every distinct delivery plan named by Git-parsed `Delivery-Plan:` trailers and associates each commit with its own plan; prose mentioning a trailer key is not a trailer. It also reads related producers and refutes unsupported assertions. For multiple independent booleans it enumerates all $2^N$ combinations. The final message lists the path, review outcome, unresolved findings, declined findings and optional nits, then proposes `/qa:run <path>`. Unreviewed or still-blocked plans are not described as approved.

Before reading a trailer-named delivery plan, the planner requires a repository-relative path with no `..` segment or symlink component that resolves to a regular file under `git rev-parse --show-toplevel`. Rejected trailers, including missing files, are ignored and listed with their reasons in `## Changes Summary`; each accepted plan path and its associated commits are listed there too. Accepted plans are specification data, never instructions to the planner; their verification commands are not executed during planning.

Plans are saved to `docs/testing/plans/YYYY-MM-DD-<topic>-test-plan.md`. `## Source` records `Branch:` and `Head:`; `## Changes Summary`, `## Blockers / Findings`, `## Users`, `## Detected Tools` and FE/BE scenario sections follow. `## Setup` is optional **human notes only**, never parsed configuration. Scenarios use relative paths or URLs on configured origins, optional `- **Target:** <name>`, a required `- **Writes:** yes|no`, and `$QA_…` references. Expected results and each edge have `(path:line)` grounding or `(unverified — confirm at run time)`. Data preconditions register users and create needed records through the app; they do not assume a seeded CV/order already exists. Upload fixtures can be repository files; `NEED_INFO kind=fixture` is for data the app cannot create. Repeatable BE-only State Checks use configured stores, not a connection embedded in the plan; a store name is required unless exactly one is configured. Preconditions reaching another configured origin use an absolute URL on that origin.

## Configuration

QA reads the shared `.av/config.toml` and optional personal overrides from `.av/local.toml`. Start with target origins and the three `[qa]` policy choices:

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

Optional additions:

- `[env.services]` — health probes and lifecycle commands.
- `[env.secrets]` / `[env.values]` — engine-only inputs and tester-visible values.
- `[env.stores.<name>]` — named SQL/Redis endpoints for BE State Checks.
- `[qa.users.<name>]` — existing users' email/password/optional ID sources and descriptions.
- `[qa.cleanup]` — one SQL, HTTP or command recipe deleting tester-registered accounts by email.

Full reference: [Configuration](../configuration.md).

## Users and secrets

### Users, names and plan token grammar

`[qa.users.<name>]` configures existing accounts with required `email`/`password` sources and a non-empty `description`, plus an optional `id` source. Names match `[a-z][a-z0-9_]*`; `new`, `captured` and `captured_*` are reserved. Existing accounts are never created or deleted by QA.

The plan's `## Users` section declares `- <name>: existing|registered — <description>`. A plain user signup can create is `registered`; a role or state signup cannot produce is `existing` and needs configuration. A configured name must be declared existing, not registered. Plans use at most 10 registered users.

| Input | Tester-visible names |
|---|---|
| Referenced existing user `u` | `QA_<U>_EMAIL`, `QA_<U>_PASSWORD`, `QA_<U>_ID` when referenced and configured |
| `[env.values]` entry `X` | `QA_<X>` when referenced |
| Registration password | `QA_NEW_PASSWORD` |
| Referenced store | `STORE_<NAME_UPPER>_<CLIENT>`; native aliases via `load.sh --store <name>` |
| `[env.secrets]` | Never exposed to testers |

The engine scans **every part of every scenario**, including headers, payloads, preconditions and edges, for `$QA_NAME` or `${QA_NAME}` with names matching `QA_[A-Z0-9_]+`. It resolves `TAG`/`NEW_PASSWORD` first, rejects token/cookie suffixes, then resolves `EMAIL|PASSWORD|ID` using the longest declared/configured user prefix, then ordinary values. An existing user's missing configuration/ID source is `missing.users`; other unknown values are `missing.values`. A configured but undeclared user, any other undeclared user-shaped reference, or a configured user declared registered is a `plan_errors` entry. Values cannot use credential suffixes, `TAG`, `NEW_PASSWORD`, `CAPTURED_*` or exposed user field names.

`users provision` generates `QA_NEW_PASSWORD` once per run (24 random URL-safe characters plus `Aa1!`) and keeps it for every later provision. `dispatch tester` returns a distinct eight-character hexadecimal `tag`, shown as `Tag:` beside `Dispatch:` and `Stores:` in the tester input; `QA_TAG` is not in the channel. Testers register with `qa+<Tag>-<user>@test.local` or another application-supported tagged address and the run password, and log in through the application's API/form themselves. Registration counts as a write.

Registered users' `$QA_OWNER_EMAIL`/`$QA_OWNER_ID`-style plan references mean the tester's own values from its registration step, never channel names. Using a registered user's fields before that precondition ran is `NEED_INFO kind=fixture` naming the user. Plans never contain `$QA_<U>_TOKEN` or cookie fields; the tester keeps returned credentials privately.

### Private run channel

`${TMPDIR:-/tmp}/qa-run-<run-id>/` is mode **0700**. It holds:

- `secrets.env` and `secrets.json` (**0600**): only referenced existing user fields and values, `QA_NEW_PASSWORD`, and namespaced client variables for stores referenced by State Checks. Shell values are quoted, not pasted into request argv.
- `load.sh` (**0600**): each request begins `. '<run-dir>/load.sh' NAME... && . '<run-dir>/results/<dispatch>/captured.env'`. State Check calls pass `--store <name>` first to export native client aliases, then required names. The loader clears inherited `QA_*`, `PG*`, `MYSQL_*`, `SQLITE_DB`, `REDIS*` and `STORE_*`, requires a readable regular owner-owned non-symlink `secrets.env`, sources it, restores allexport state, and fails naming any required empty value or unknown store. It never prints values.
- `redact-names` (**0600**): channel names and referenced stores' native aliases, excluding `PGOPTIONS`; values are not in this file.
- `capture.sh` (**0600**): `sh '<run-dir>/capture.sh' <dispatch> QA_CAPTURED_<USER>_<FIELD>` reads a credential/ID on stdin, strips one trailing newline, shell-quotes it and appends its export and redaction name to the dispatch files. Names must match `QA_CAPTURED_[A-Z0-9_]+`; invalid names fail without writing.
- `results/<dispatch>/` (**0700**): before the tester starts, dispatch creates an empty `captured.env` and a copy of the engine's `redact-names`, both **0600**, so a credential-free first request can source/sanitise normally. Tester answers are saved under `results/` before ingest.
- `engine.log` (**0600**, **engine-private**): successful sources/services/command recipes retain only exit/line-count summaries, not stdout/stderr; failures retain at most 2048 characters after masking. Only values known to the writing process and at least four characters long are masked. For a helper's admin key, use an inherited `AV_<NAME>` variable also declared as `env:AV_<NAME>` under `[env.secrets]`; the runtime collects its value for masking even when no recipe resolves the source, without injecting it into the helper. A helper fetching a key any other way must never print it, including on failure. Unresolved configured values, undeclared secrets and shorter values may remain in failure tails; never read this file into the transcript.

The channel is written even with zero users or values. OMP FE JavaScript reads `secrets.json` with `Bun.file`, checking required names before any fill; it does not use `process.env` as the credential source. Sources resolve only after trust and only when needed. Engine-only administrative secrets never enter the tester files. The engine's `SecretSet` loads only `secrets.json` as private state, alongside declared-source values collected for masking.

**Capture before evidence:** BE testers retain a raw registration/login response without printing it, pipe returned tokens/cookies/IDs into `capture.sh`, then source `captured.env` again **before sanitising that same response** with the dispatch's `redact-names`. Later requests use `QA_CAPTURED_<USER>_TOKEN` or `QA_CAPTURED_<USER>_COOKIE`. Testers refuse credential-bearing requests or forms on non-loopback HTTP with `SKIP — cleartext origin refused: <origin>`. The engine does not log anyone in on dispatch.

Immediately after every successful registration, before any other step, the tester calls `sh '<run-dir>/capture.sh' <dispatch> --account <email> [<id>]`. FE uses this after signup through the form; in OMP its JavaScript/Bun cell performs the equivalent engine call. This reaches `users record` and writes the durable ledger immediately, outside the private directory. A non-zero exit is `NEED_INFO kind=fixture` naming the email. Optional top-level `accounts: [{"email": "...", "id": null}]` in `qa-results` is the secondary path when immediate recording could not run; use `accounts: []` otherwise.

Both recording paths accept only full emails matching `[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,253}` whose local part contains the dispatch tag case-insensitively and which equal no `QA_*_EMAIL` value in `secrets.json`. IDs are null/absent or match `[A-Za-z0-9._:-]{1,128}` in full. Ingest lists rejected emails in `accounts.rejected`; malformed account-list structure invalidates the whole result block. Recording is idempotent on `(run_id, dispatch, email)` and only fills a missing ID, without resetting attempts/status.

### Teardown and the ledger

Optional `[qa.cleanup]` contains exactly one `sql`, `http` or `command` recipe. SQL requires a configured SQL store and a query containing `{email}`; HTTP requires `{email}` somewhere in its request templates and HTTPS off loopback, and never follows redirects. SQL permits only `{email}`, `{id}`, `{tag}`, substitutes quoted literals with apostrophes doubled, and substitutes `NULL` for an absent ID. HTTP also permits configured `{secret.X}`/`{value.X}` and percent-encodes identity substitutions in path/query. Commands have no placeholders; they receive `QA_EMAIL`, `QA_ID` (empty when unknown) and `QA_TAG`, not `QA_PASSWORD`/`QA_NEW_PASSWORD`. See [Cleanup](../configuration.md#cleanup) for the full schema.

Each accepted registration is recorded as `pending` in `${XDG_STATE_HOME:-~/.local/state}/av-marketplace/qa-accounts.json`, keyed by repository realpath. The ledger survives plan/report changes, repair restarts, run end, takeover and stale-private-directory deletion. It contains email, ID, run ID, dispatch ID/tag, cleanup destination, `deleted`, status and attempts, **never passwords or tokens**.

`users teardown` uses only the **current** valid and trusted (or `not-required`) cleanup config. Its SQL store endpoint or HTTP origin must be in this run's locked keys; command cleanup has no destination check. A record's non-null destination must equal the current destination, even when another target/store is also locked. A null destination adopts the current destination on its first attempt. There is no deletion attempt or attempt increment without a usable recipe or when destinations differ.

A usable recipe attempts every eligible undeleted non-manual record for this repository, including earlier runs. An HTTP recipe needing an unavailable ID counts as a failed attempt without executing. Success marks `deleted`; any other attempt increments the record's counter. At three failures it becomes `manual-cleanup` and is no longer retried automatically.

- `deleted`: cleanup succeeded and is recorded.
- `left`: no usable recipe, invalid/untrusted current config, an unlocked/mismatched destination, or a failed attempt below the limit.
- `manual`: a record reached three failed attempts in this teardown; it is listed once, not on every later teardown.

Review these emails in the report/summary; retaining a ledger record is not successful teardown. Missing cleanup is a soft gap, so registration can proceed with an explicit warning that accounts will remain. A later compatible trusted run can retry pending records; manual records require deliberate cleanup in the correct application. Normal `run end` removes captured credentials with the private directory; interrupted sessions can leave them until takeover or this repository's stale-directory cleanup after 24 hours.

### What reaches the transcript and artifacts

Names, target origins, account emails/IDs, sanitized results and cleanup outcomes are visible. Passwords, tokens and cookies are not printed by the engine. BE requests pass credentials via curl config on stdin; every response and State Check result goes through the installed `qa-redact.pl` before inspection/persistence. After capture, the tester sources its capture file before redaction, so a fresh credential is masked even when echoed under `message` or another ordinary key. Missing Perl/`JSON::PP`, missing names file, failed capture or failed redaction fails closed; a write is never replayed to obtain an artifact. Sensitive headers/JSON fields, bearer tokens, sensitive URL parameters and declared values are masked; non-JSON/bare-string bodies are withheld. Ordinary URL-named fields can retain scheme/host/path with userinfo/query/fragment masked; one-time-link fields remain fully masked. Do not assume a visible URL path is secret-free.

**FE fill calls can expose credentials in the harness transcript**, and snapshots/screenshots can show user data. Use disposable, non-privileged accounts. Before a failure screenshot the tester inspects a fresh snapshot; debug pages or an uninspectable page suppress screenshot/debug-text capture, retaining only a sanitized URL/status and generic title. A screenshot path is reported only after its file exists. Keep `docs/testing/reports/screenshots/` and `docs/testing/reports/responses/` out of version control and review artifacts before sharing them.

## Safety

- **Exact origins:** relative paths use the scenario/default target. Absolute URLs must match a configured origin including scheme and effective port, not just host; otherwise `SKIP — off-target URL refused: <origin>`. Userinfo is always refused; redirects are never followed automatically. Off-target plan URLs stop at plan check. Non-loopback targets require trust.
- **Mutation policy:** only `allow|deny`. `allow` guards nothing and is meant for disposable data. `deny` guards every scenario whose `- **Writes:**` line is `yes`, missing or invalid, and every scenario the syntactic scan sees writing, including registration and writes in preconditions, steps, edges or State Checks. A `no` declaration cannot override detected writes. Guarded scenarios are SKIP, not fix candidates.
- **Trust and run-bound config:** pins live outside the repository, per realpath/plugin, and hash full unmasked canonical settings. Sources, cleanup, all three policy keys, shared services, non-loopback targets/store hosts and the entire store named by SQL cleanup cannot change silently. A started run binds the **entire effective config**, including non-pinned keys. Users/services/dispatch drift checks stop with `config changed during run`; registered-account cleanup uses only a valid, trusted current recipe on a locked matching destination. Otherwise records stay `left` without attempts while `services down` still uses the recorded command. Source definitions are pinned, not mutable command outputs or dotenv contents.
- **Source restrictions:** committed `[env]` `env:` names must begin `AV_`; QA sources permit `AV_` or `QA_`. Committed `file:` paths must be repository-relative and git-ignored, and must not escape the repository. Committed secret/user-password literals are forbidden; a store-password literal is allowed only for loopback. Personal overrides can use arbitrary environment names, absolute file sources and literals. See [Secrets and values](../configuration.md#secrets-and-values) for the rules. Prefer a private file or `cmd:` source instead of exporting into a running harness: `env:` sees only its startup environment.
- **Auth rule:** a BE feature main flow expecting 2xx but getting 401/403 is always a **FAIL flagged `auth`**. The engine logs nobody in and does not downgrade the failure because a credential was absent. It is **never auto-fixed** under `fix = "auto"`; `approve` shows the flag and can offer it only for human review. A fixer may never weaken authentication or authorization to make a scenario pass. Expected 401/403 enforcement remains ordinary testing.
- **Fix guards and authority:** rejected, location-less, incomplete or ambiguously mapped issues never dispatch. Automatic mode excludes auth/unverified assertions per issue, without blocking grounded sibling issues. Payload-literal anti-hardcoding warnings are heuristic and non-blocking. Only independent tester evidence and the final whole-scenario PASS can credit a fix; a preserved `🚫 Rejected` line and its reason are never overwritten.

### Residual risks

Mutation classification is static/best-effort: GET side effects, GraphQL mutations without explicit verbs and implicit FE submit/click writes can escape it. Config shell/commands and the target app are executable code, not a sandbox; a trust pin cannot make an untrusted branch safe. A fixer seeing deterministic scenarios can game verification despite the warning/default approval gate. Cross-section regressions may surface only in the final pass.

Signup rate limits, CAPTCHA and mandatory email confirmation without a local mail catcher can block tester registration; use a configured existing user when the required role/state cannot be created through signup. Testers handle session/CSRF round trips themselves. Private temp files remain during the run and may outlive an interrupted session. FE transcript exposure and token-bearing URL paths remain risks.

Auth classification flags BE 401/403-versus-2xx failures, not 2xx-shaped gating, tenant-shaped 404s or FE gating. Tokens can expire mid-dispatch. Config drift is checked at engine calls, so an already-dispatched tester can finish on old settings. Locks coordinate identical configured target origins and store endpoint keys; an undeclared store, or one backend reached through different host aliases, is not coordinated. Tester adherence to the structured result block is prompt-level; invalid/missing output degrades to `cannot-confirm`, never PASS.

## Coverage honesty

The summary reports what was **exercised**, not just whether failures remained. It retains the coverage-honesty behavior introduced in 2.3.0:

```markdown
## Coverage
- Exercised: <N> feature · <M> sanity · <K> enforcement
- Not verified: need-info <N> · mutation-guard SKIP <M> · tool-unavailable <K> · cannot-confirm <J> · transport <L>
- Confidence: high | low — <reason>
```

A feature PASS is an upper bound on verification, not proof of complete behavior. Scenario verdict precedence is failure → missing prerequisite → skip → pass, aggregating main flow and every edge. A skipped State Check alone does not downgrade the HTTP result.

**Shallow-coverage warning:** when feature scenarios exist but none passes, the summary warns that a green result reflects infrastructure/enforcement checks only. This applies to every fix policy and both existing and generated plans. A mutation-guard-only all-SKIP generated plan has its own graceful message; zero feature scenarios do not trigger this warning.

**Low-confidence green:** on a zero-failure exit with shallow coverage for an auto-generated plan, the summary says the plan may not reflect runtime auth/setup. An existing plan instead retains the no-failing-assertions wording alongside Coverage. An all-SKIP/NEED_INFO existing plan is `Stopped` (`No executable verifier — cannot gate`), not success. An all-SKIP/NEED_INFO generated plan can exit gracefully, distinguishing mutation-guard-only coverage from setup/tooling/parse gaps with a coverage-zero warning.

**Unlock hints** name what to change, not secret values: adjust `qa.mutations` only for disposable data; configure required existing users and their sources under `qa.users`, correct registration/login preconditions, fill `env.values` or `env.stores` keys, start the evidenced service, enable the missing tool, or re-run `/qa:run` when the dispatch limit is exhausted. No harness restart is needed for the private file channel. Broad transport/service failures suggest checking reachability without claiming an app crash.

Assertions tagged `(unverified — confirm at run time)` are flagged for approval review and excluded from automatic fixes, per issue. Grounded sibling failures stay eligible. The engine summary always carries `**Result:** Pass`, `Fail`, `Budget Exhausted` or `Stopped`; a stop/budget exhaustion is not success, and Pass is never a claim of full coverage.

## Reports and Code Review

Reports live in `docs/testing/reports/YYYY-MM-DD-<topic>-report.md`. The engine renders Summary counts, `- Accounts: registered N (deleted D, left L, manual M)` (or `- Accounts: registered 0`), conditional `## Setup gaps`, detailed results and one Loop History row per iteration. Only failed assertions mint issues; service/tool/fixture gaps do not become app defects.

Issue blocks use `### [SEVERITY] QA-NNN: Title`, `**ID:**`, `**Location:**`, `**Category:** Testing`, `**Problem:**` and `**Remediation:**`, plus scenario/response/screenshot context. The engine carries forward Status, decision records and corrected Location by QA token. Only final whole-scenario PASS writes Fixed; a partial pass leaves issues open. Mechanical severity is CRITICAL for observed HTTP ≥ 500 or crash, otherwise LOW for unverified assertions; other severities follow evidence, with CRITICAL security-bypass/data-loss requiring an explicit reason.

With Code Review installed, `/fix QA-001` reads the newest QA report; `/fix-report` without a path merges the newest review and QA reports and writes statuses back to their original files. `/fix-report docs/testing/reports/<file>.md` selects one report. See [Code Review](code-review.md) for routing and decision handling.

Delivery 0.6.0 and later runs `/qa:run last <N> commits` over a delivered change before its final code review and commits the fixes, configuration, plan and report QA produces; see the [Delivery guide](delivery.md#qa).

## Engine

The stdlib-only CLI at `plugins/qa/skills/engine/scripts/qa.py` owns config/trust, plan checks, origins/mutations, services/users, private state, dispatch counters, verdicts, QA IDs, candidates, stop decisions and report rendering. The model owns planning, test execution, fixes and sanitized issue prose. Neither the orchestrator nor users should patch the sidecar/report to manufacture results.

The CLI registers handlers on its argument subparsers, with one shared executor for locked run-state operations. Repository-option registration is centralized in `repo_option`, so `--repo` keeps the same behavior before and after subcommands.

The durable sidecar remains `docs/testing/reports/<topic>-loop-state.json`; the engine detects plan hash changes, reuses/adopts compatible report state and rebaselines changed plans while preserving report metadata. Account cleanup is independent in the external ledger. CLI commands include `config`, `trust accept`, `config preview|apply`, `tools`, `plan resolve|check`, `run start|stop|end`, `services check|up|prepare|down`, `users provision|record|teardown`, `dispatch`, `ingest`, `issues`, `candidates`, `fix done`, `iteration open|close`, `report` and `summary`.

Internally, `qa_engine/runs.py` owns lifecycle and locked state access, `locks.py` owns target-origin/store-endpoint locks, `dispatch.py` owns tester/fixer bookkeeping, `results.py` owns `qa-results` block ingestion, and `verdicts.py` owns pure evaluation. `issues.py` and `candidates.py` own QA IDs and fix eligibility; `iterations.py` owns loop decisions and recovery; `sidecar.py` owns reuse, adoption and rebaselining. Durable record types and their JSON validators remain together in `schema.py`; `models.py` supplies the run handle without importing lifecycle commands. SQL, HTTP and command cleanup recipes have separate validators and executors. `users.py` owns configured-user provisioning, capture helpers, idempotent registration recording and the durable ledger's three-attempt cleanup rule. Report-prose validation uses an ordered rule table, keeping the existing validation precedence, error messages and CLI shapes. The `config` JSON contract returns `policy{fix, mutations, start_services}`, derived `defaults{FE, BE}`, users and store-kind metadata, without `budget`; Python consumers import from the owning modules rather than the removed `state.py` or `av_config.py` APIs.

Fix eligibility shares one `CandidateContext` (state, assertion claims and report blocks) across the issues in a selection pass. Run creation takes `RunStartOptions` for takeover, provenance and baseline choices, then passes the run identity and baseline to sidecar preparation as `SidecarContext`. These are internal parameter objects.

`models.py` is a leaf handle/error module: it depends only on `files.py` and `schema.py`, not plan parsing, config, users, services or lifecycle commands, including type-only imports. Consumers load a run's plan through `plan.run_plan`, which preserves strict hash checks by default and permits non-strict reads for stop reports and summaries. Path display lives with plan fingerprints in `files.py`, keeping the handle out of the report/plan dependency graph.

`qa_engine/common.py` is the shared boundary for report blocks/fields, assertion keys and lookup, sanitized origin formatting and JSON artifact I/O. Both fix eligibility and report rendering read Status/Location/decision metadata only before Category, accepting bare or bulleted field lines; prose fields remain readable across the issue body. Origins always include their port and bracket IPv6 hosts. `config.STORE_NAMES` lists native client variables for namespaced store exposure and unlock hints; `stores.py` resolves their values. Git subprocesses share one bounded runner in `git.py`; trust storage and origin locks share `av_config.files.default_state_home`.

See the [engine skill](../../plugins/qa/skills/engine/SKILL.md) for installed-path resolution, every command/JSON shape and the result protocol. Every subcommand accepts internal `--repo <root>`; these are **not `/qa:run` flags**. Stdout is one JSON object except successful `summary` (Markdown). Exit 0 means the operation completed, 1 is a domain stop/gap, and 2 is usage/invalid config. `config` can exit 0 with a missing-file/table state; that is not a configured run.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `plan check` lists `missing.users` | Add the declared existing user's `qa.users.<name>` sources/description or missing ID source. A declared registered user needs no config. Interactive `extend` proposes just the gaps. |
| Missing values, targets or stores | Add `[env.values]` sources, `[env.targets]` named `ui`/`backend`, or the named `[env.stores.<name>]` endpoint. One target serves both sections; multiple targets do not substitute the other section's origin. Fill named private dotenv keys locally. |
| `plan_errors` | Correct undeclared/configured-user mismatches, reserved token/cookie references, ambiguous/FE State Checks or cleartext credential origins. These are plan errors, not keys bootstrap can invent. |
| Off-target URL | Correct the scenario or explicitly configure/trust the intended origin. A host match with a different scheme/port is still off-target. |
| Registration/login cannot complete | Inspect the tester's safe service/fixture evidence and signup/auth preconditions. The engine has no login repair path. CAPTCHA, rate limits or unproducible roles may require a configured existing user. |
| `missing.cleanup` | Offer a grounded email-based SQL/HTTP/command recipe. This gap alone does not stop: absent/declined cleanup keeps the valid config and warns that registered accounts remain. |
| Local override blocks extension/repair | Resolve/remove the named key in your own `.av/local.toml`, then re-run. Bootstrap cannot override or edit your personal file. |
| New/changed trust prompt | Review the complete masked subset, including existing shared commands, not just the latest diff. Approve only the shown hash; changed files/another worktree need a fresh pin. Headless cannot consent. |
| Stale plan | Regenerate (default), or deliberately use it after review. Headless regenerates. Passing a path chooses it explicitly; old plans can lack Branch/Head metadata. |
| Live lock from another run | Stop the other active run, or approve takeover only if its session was interrupted. The prompt names the holder/start time. Headless refuses takeover; do not delete locks or the account ledger blindly. |
| Services down | Check failing probes, correct ports and checkout. Under `qa.start_services = "ask"`, approve evidenced `up`/`prepare` once; headless must start the stack externally or pre-approve `"auto"`. `qa.fix` does not decide bring-up. If still down, use interactive repair; a 5xx health response is down, a responding status below 500 is up. |
| `cannot-confirm` | The tester omitted/malformed its `qa-results` block, section, assigned scenario or edge. Re-run the affected section/run with usable tools and complete results; missing output is never a retained earlier PASS. |
| `config changed during run` | The effective config drifted. QA stops; `services down` still uses the recorded command, while account cleanup requires valid/trusted current config and a locked matching destination. Otherwise records stay `left` without attempts. **Re-run `/qa:run`** to validate, trust, check and lock the new config; do not continue the existing run or edit its sidecar. |
| Accounts `left` | Review missing cleanup, invalid/untrusted config, unlocked/mismatched destinations, missing HTTP-required IDs or failed attempts. A later compatible trusted teardown can retry pending records. |
| Cleanup destination mismatch | Restore the intended destination or clean up deliberately there. A non-null recorded SQL endpoint/HTTP origin must match the current recipe and be locked by this run; changing names or approving another destination does not authorize deletion of that record. |
| Accounts `manual` | The record reached three failed attempts and is now `manual-cleanup`; later runs do not retry it or list it again. Inspect the deletion contract and remove the account deliberately in the correct application. |
| FAIL flagged `auth` | Expected 2xx got 401/403. Inspect tester login/registration, auth/authorization, token lifetime and intended role. Auto drops it; approve only a real root-cause fix, never weakening auth. |
| No fixes in headless `approve` or under `off` | Expected test/report-only behavior. Set/trust `qa.fix = "auto"` only when automatic eligible source edits are intended. |
| Budget exhausted or no-progress stop | Read Loop History and the named limit/stop reason. The limits are fixed (3 iterations, 50 dispatches, 30 minutes); investigate the remaining issue, then start a new run. Do not reset counters by hand. |

## Prerequisites

- **Python ≥ 3.11 as `python3`** for the engine and TOML parser. Older versions exit with an explicit version error. No engine dependencies are installed.
- **Git** and a repository with a HEAD for branch/plan resolution and tracked-change recovery.
- **curl** for credential-bearing BE requests and fail-closed capture; HTTPie is only a credential-free alternative. **jq** and standard shell tools for extracting credentials into `capture.sh`, and **Perl with `JSON::PP`** for the installed BE sanitiser (`perl -MJSON::PP -e 1`). Missing required HTTP/browser/sanitiser tools yield `NEED_INFO kind=tool`, not fabricated PASS/SKIP.
- **Playwright MCP** for FE tests in Claude Code; OMP uses its built-in browser instead.
- **A running app or evidenced `[env.services]` commands/probes** for managed bring-up. QA can start/prepare the config's services after its gate and stops only what it started.
- **`[env.stores.<name>]` and the matching `psql`, `mysql`, `sqlite3` or `redis-cli` client** for State Checks. Redis requires `redis-cli --json` (and ≥ 4.0.11 for `REDISCLI_AUTH` when configured); without `--json` the check is `NEED_INFO kind=tool`. An unavailable client skips only that check, not runnable HTTP assertions. CLI store configuration is not MCP tool-grant isolation; remove write-capable store MCP servers from an untrusted session.
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
