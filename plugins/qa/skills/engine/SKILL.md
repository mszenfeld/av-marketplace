---
name: engine
description: QA engine CLI reference — resolve the installed script, read config and plan requirements, manage services and accounts, and let the engine own run state, verdicts, issue IDs and reports.
---

# QA Engine

Load this skill before calling the engine from `/qa:run`, `/qa:create-plan` or `qa:plan-authoring`. The engine uses only the Python standard library and requires Python 3.11 or newer. On an older interpreter it exits 2 with `{"error": "Error: the qa engine needs Python 3.11 or newer (found X.Y)"}`.

## Resolve the installed script

Never use a project's own copy of `qa.py` or assume the marketplace checkout is installed in the current repository.

- **Claude Code:** `${CLAUDE_PLUGIN_ROOT}/skills/engine/scripts/qa.py`; run it exactly as `python3 ${CLAUDE_PLUGIN_ROOT}/skills/engine/scripts/qa.py <subcommand> ...`, without quotes around the script path, to match the command's permission pre-approval.
- **OMP:** run `realpath skill://qa:engine/scripts/qa.py` with Bash and invoke `python3 '<resolved-script>' <subcommand> ...` using the returned absolute path. An OMP shell does not define `CLAUDE_PLUGIN_ROOT`; do not substitute a guessed path. Skill references here resolve with the `qa:` prefix in OMP.

Claude Code examples:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/engine/scripts/qa.py config
python3 ${CLAUDE_PLUGIN_ROOT}/skills/engine/scripts/qa.py tools
```

Each subcommand accepts `--repo <root>`; without it the engine uses `git rev-parse --show-toplevel`. A multiword change source is one quoted argument, for example `plan resolve 'last 3 commits'`. The engine's switches below are internal CLI arguments, not options accepted by the flagless `/qa:run` or `/qa:create-plan` commands.

## Output and exits

Except for a successful `summary`, stdout is exactly one JSON object. Read it and the exit code together; never infer success from an empty `errors` list alone. No resolved password, token, cookie, secret or exposed value is printed. Provisioning output includes the persona names, account emails and IDs so the user can identify created accounts; these are not the tester's credentials channel.

| Exit | Meaning | Caller action |
|---|---|---|
| `0` | The operation completed; inspect its `state`, `ok` or `decision` as applicable. | Follow the command workflow. `config` can report a missing file/table with this exit; that is not a valid config. |
| `1` | Domain stop, normally `{"error": "<reason>"}` with optional diagnostic fields. `plan check` instead returns its complete `ok: false` gaps/guard result. | Route the stop or gaps through `/qa:run`; after `run start`, still run teardown. Never patch state to bypass the stop. |
| `2` | Usage error or invalid config. `config` lists every validation error with its file/key; preview validation returns `ok: false` and `errors`. Other failures return `{"error": "<reason>"}`. | Show the error; do not run a config recipe. `/qa:create-plan` may author with `Config: none` when `config` returned a recognized non-`ok` state. |

Sources are validated, not executed, by `config`. Resolution happens only after trust, through accounts, services and tester dispatch. Errors name keys/sources, never their values. Command and recipe output tails belong in the run's redacted `engine.log`, not in the orchestrator's answer.

## Subcommands and JSON shapes

The shapes below name object fields; `[]` means an array, `{}` a map, `?` an optional field, and `null` an absent value. Paths and IDs returned by the engine are authoritative.

### Configuration and trust

| Subcommand | JSON output |
|---|---|
| `config` | `{state, errors[], warnings[], provenance{}, trust, trust_hash, trust_subset, targets{}, defaults{}, policy{}, budget{}, services{health[], up, prepare[], down}, personas[], static_personas[], values[], exposed[], database}`. On invalid config the metadata after `trust_subset` may be absent. |
| `trust accept <hash>` | `{trusted: hash}`. Exit 1 if the current trust subset hashes differently. |
| `config preview <proposal>` | `{ok, errors[], diff, trust_subset, trust_hash, snapshot}`. Writes nothing. |
| `config apply <proposal> --snapshot S --approved-hash H` | `{applied}`. Uses the preview's snapshot and approved hash. |
| `tools` | `{curl, jq, perl_json_pp, psql, mysql, sqlite3, httpie}`; every field is a boolean. Browser availability requires a separate browser probe. |

`state` is `missing-file`, `missing-table`, `invalid` or `ok`. `trust` is `not-required`, `new`, `trusted` or `changed`; only `trusted`/`not-required` permits `run start`. `provenance` identifies `.av/config.toml` or `.av/local.toml` for effective keys. `values` and `exposed` contain names only; `database` is masked metadata or `null`. Show `trust_subset` for the one trust question, not the raw config or a source's output.

`<proposal>` is a path to a JSON transaction:

```json
{"config_text": "<full proposed .av/config.toml>", "gitignore_add": [".av/local.toml", ".av/secrets.local.env"], "allowed_keys": ["<keys this transaction may change>"]}
```

Preview validates the merged proposal, treating `gitignore_add` paths as ignored. Only `allowed_keys` may change; other table sections, including comments, remain byte-identical. Its opaque `snapshot` captures the current config and `.gitignore` bytes. Apply aborts without writes if either differs from that snapshot; otherwise it writes both atomically, revalidates and records trust only if the result hashes to `H`. A failed validation/hash check restores the original bytes only while the files still equal the engine's own writes; concurrent edits are left intact and reported as a conflict. Never apply without the preview approval.

### Plans

| Subcommand | JSON output |
|---|---|
| `plan resolve [ARG]` | `{action: "reuse"|"stale"|"generate", plan, branch, head, source, changed_files[]}`. `plan` is `null` when generation is needed. |
| `plan check <plan>` | `{ok, sections{FE[], BE[]}, personas[], values[], missing{personas[], values[], targets[], database}, off_target[], guarded[], exempt[], db_checks[]}`. `missing.database` is a boolean; persona gaps include their reason. |

An existing argument path is reused; a change source requests generation. With no argument, resolution selects the newest plan whose Source `Branch:` matches the current branch, otherwise generation. Detached HEAD generates. A selected plan is stale if its Source `Head:` is not an ancestor of HEAD, or the intervening diff touches a file outside `docs/`; `changed_files` explains that decision.

Plan checks derive personas and values from `$QA_NAME` or `${QA_NAME}` tokens anywhere in scenarios, including preconditions and edges. They check persona field capabilities, scenario/default target names, every absolute URL's exact origin, DB checks and the mutation policy. Unknown names are config gaps, not a tester's runtime credential request. `off_target` entries identify the scenario, origin and reason; `guarded` and `exempt` are scenario IDs. Guards include writes in data preconditions: `deny` guards all writes; `rejections-only` exempts only grounded, rejection-only scenarios without other writes; `allow` guards nothing.

### Run, services and accounts

| Subcommand | JSON output |
|---|---|
| `run start <plan> [--takeover <run_id>]` | `{run, dir, sidecar, report, idempotency: "reuse"|"adopt"|"fresh"|"rebaseline"}`. |
| `run end --run ID` | `{released}`; releases origin locks and deletes this run's private directory. |
| `services check --run ID` | `{up, probes[{target, path, status}]}`. |
| `services up --run ID` | `{ran, exit}`. |
| `services prepare --run ID` | `{ran, exit}`. |
| `services down --run ID` | `{ran, exit}`; does nothing unless this run executed `up`. |
| `accounts provision --run ID` | `{personas[{name, email, id, static}], exposed[], secrets_env, secrets_json, redact_names, loader}`. |
| `accounts refresh --run ID` | `{refreshed[]}`. |
| `accounts teardown --run ID` | `{deleted[], left[], unresolved[]}`. |

`run start` refuses untrusted config with `trust required`. It records the effective config, its hash and its accepted trust hash in the run directory; public metadata masks literal sources, and no resolved value goes in the sidecar. It creates a mode-0700 `${TMPDIR:-/tmp}/qa-run-<run-id>/`, removes this repository's private run directories older than 24 hours and takes sorted, per-origin locks. Overlapping target sets cannot run concurrently. A live lock lasts until release or `qa.budget.minutes` + 15 minutes; explicit, approved `--takeover <holder-run-id>` also removes that holder's private directory, but its account ledger survives.

`accounts provision|refresh`, `services check|up|prepare` and `dispatch` compare the current config against the run-bound config. `config changed during run` or a dispatch login failure stops the run; teardown is still mandatory. `accounts teardown`, `services down` and `run end` skip that comparison and use the recorded, previously trusted targets, recipes and down command. Cleanup placeholders use current sources only when current config is trusted; otherwise accounts stay `left`. The recorded down command needs no sources and can still run.

Provisioning runs even with zero personas, so every tester has a secrets channel. Only plan-referenced persona fields/values and required database names are exposed. `secrets.env`, `secrets.json` and `load.sh` are mode 0600; `redact-names` lists exposed names without values. Testers source `. '<run-dir>/load.sh' NAME...` in each request/DB Bash call, or the OMP FE cell reads `secrets.json`; never rely on inherited harness credentials or print the files. Engine-only secrets never reach testers. Private account state and the external account ledger are engine-owned. Teardown reports every undeleted or unresolved identity rather than concealing it.

### Dispatch, results and iterations

| Subcommand | JSON output |
|---|---|
| `dispatch --run ID tester --section FE|BE --phase baseline|retry|iteration|final` | `{dispatch, scenarios[], edges{}, guarded[], refreshed[], dispatch_count, budget_left}`. |
| `dispatch --run ID fix --qa QA-NNN` | `{dispatch, qa, scenarios[], edges{}, refreshed[], dispatch_count, budget_left}`. |
| `fix done --run ID --dispatch D --result fixed|partial|failed` | `{warnings[]}`. |
| `ingest --run ID --dispatch D <file>` | `{verdicts{}, need_info{}, new_failures[], regressions[], now_passing[], incomplete[], error?}`. |
| `issues --run ID` | `{assign[{qa, key, scenario, observed_status, unverified, severity_floor}], open[]}`. |
| `candidates --run ID` | `{fix[{qa, key, flags[]}], dropped[{qa, reason}]}`. |
| `iteration open --run ID` | `{decision: "iterate"|"final"|"stop", iteration, reason}`. |
| `iteration close --run ID` | `{decision: "continue"|"final", reason, now_passing[], regressions[], fix_touched_files[], overlap[]}`. |

Call engine dispatches sequentially, even when FE and BE tester agents will run in parallel. A tester dispatch logs in afresh for its section's personas, atomically replaces only their token/cookie entries, preserves other personas/values/database names and `redact-names`, and records `authenticated[]` internally before assigning scenario IDs and planned edge counts. A fix dispatch records the engine-assigned QA ID; `fix done` records its outcome and computes warnings for added literals matching the failed BE request payload.

Save a tester's complete answer to a file in `<run-dir>/results/` and pass it to `ingest`. Only its final fenced `json qa-results` block is parsed. The section must match the dispatch; every assigned scenario and planned edge must appear exactly once. Missing items become `cannot-confirm`, never an earlier retained PASS; duplicates or a wrong section invalidate the block. Invalid/missing blocks yield cannot-confirm results plus `error`, not permission to invent results. Tester status is `PASS|FAIL|SKIP|NEED_INFO`; a need-info kind is `credentials|service|fixture|tool` or `null`. Let `ingest` compute precedence, reasons, auth classification and regressions.

For an expected 2xx main flow returning 401/403, the engine records `auth-unverified` unless that flow sends a credential of a persona this dispatch authenticated. The latter is a FAIL flagged `auth`: `approve` may offer it for a reviewed fix; `auto` drops it, as it drops an unverified assertion. Never weaken an authentication or authorization check to make a scenario pass.

`issues` owns key-based QA-ID allocation. Status >= 500 or `crash` forces `severity_floor: "CRITICAL"`; an unverified assertion otherwise forces LOW; other floors are `null` for the orchestrator's evidence-based severity. `candidates` owns prefilters, flags and guards. `iteration open` rehashes the plan, checks failure severity and all budgets, snapshots verdicts and increments only on `iterate`. `iteration close` compares against that snapshot and writes exactly one history row with fixes and warnings; `fix_touched_files` excludes pre-existing dirty files and `overlap` names those that also changed. Route `stop` to teardown, `final` to the final test pass and `continue` to the next open; never increment counters by hand.

### Reports and summary

| Subcommand | Output |
|---|---|
| `report --run ID --issues <file> [--final]` | JSON `{report, fixed[], written}`. |
| `report --run ID --accounts` | JSON `{report}`; updates only the Summary's `- Accounts:` line from the ledger, not verdicts or Status lines. Do not combine it with `--final`. |
| `summary --run ID` | Markdown, not JSON: `**Result:** Pass|Fail|Budget Exhausted|Stopped`, counts, Coverage, unlock hints, budget usage and scoped recovery. Relay it without recomputing the result. |

The orchestrator writes **only issue prose** as a separate JSON list, using IDs from `issues`:

```json
[{"qa": "QA-001", "title": "<issue>", "severity": "HIGH", "location": "<path:line>", "actual": "<sanitized observation>", "impact": "<effect>", "remediation": "<fix guidance>"}]
```

Optional fields are `severity_reason`, `response` and `screenshot`. Use CRITICAL beyond a mechanical floor only with `severity_reason: "security-bypass"|"data-loss"` and supporting evidence in `actual`. `report` rejects unassigned IDs, a lowered mechanical CRITICAL, an unsupported CRITICAL or an unverified issue raised above LOW without >= 500/crash. It copies Expected with its grounding tag and Refutation itself; never fabricate those in issue prose. An empty list is valid when there are no new issue prose entries.

The engine preserves existing `**Status:**`, `**Decision:**`, `**Decision-retired:**`, `**Verification-plan:**`, `**Decision-pin:**`, `**Dispatch:**`, `**Verification:**` and a rewritten `**Location:**` by QA token. Final write-back requires a whole-scenario PASS, never overwrites `🚫 Rejected` and never marks a partial fix as fixed.

The summary's result is engine-computed: plan drift/explicit stops take priority; with failures at the severity floor, no-progress/regression stops are `Stopped`, exhausted budgets are `Budget Exhausted`, otherwise `Fail`. With no such failures it is `Pass`, except an all-SKIP/NEED_INFO human-authored plan is `Stopped`. Pass is not a claim of full verification: always relay Coverage and unlock hints for unverified or shallow coverage. Fail routes to the reported remaining issues; Budget Exhausted routes to the named config budgets and a rerun; Stopped routes to its reason before any rerun.

## Ownership boundary

**The orchestrator never writes or edits the sidecar or the report by hand.** This includes counters, verdicts, baseline, need-info maps, QA IDs, iteration history, Status lines and preserved decision fields. No `jq` patch, wholesale state rewrite, direct report edit or invented PASS is allowed. Read them when needed; call the appropriate engine subcommand to change them.

The sidecar remains `docs/testing/reports/<topic>-loop-state.json` and carries `run_id`; account cleanup lives in the external ledger, independently of report or sidecar rebaselining. The model coordinates planning, testing, fixing and sanitized issue prose; the engine owns all deterministic bookkeeping and report rendering. Once `run start` succeeds, every stop/error/abort path still tears down accounts, refreshes the Accounts report line, stops services this run started, prints the summary and calls `run end`.
