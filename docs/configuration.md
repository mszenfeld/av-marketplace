# Configuration

QA reads `.av/config.toml` from the project's repository root. On the first interactive run, `/qa:run` proposes it from repository evidence and asks three questions: whether the data is disposable, how to handle fixes, and whether to start configured services automatically.

## Minimal file

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

Add [services](#services) when QA should check or manage the running stack.
Add [secrets and values](#secrets-and-values) when recipes or scenarios need private inputs.
Add [stores](#stores) when scenarios include State Checks.
Add [users](#users) when the plan needs existing accounts.
Add a [cleanup recipe](#cleanup) to delete accounts registered by testers.

## Files

All paths below are relative to the project's repository root, not the installed marketplace plugin.

| File | Commit it? | Purpose |
|---|---|---|
| `.av/config.toml` | Yes | Team-wide environment and plugin settings. |
| `.av/local.toml` | No; add it to `.gitignore` | Personal overrides, using the same schema. |
| `.av/secrets.local.env` | No; add it to `.gitignore` | Private dotenv values referenced through `file:` sources. It is not loaded automatically. |

The engine merges local overrides leaf-by-leaf over the shared file, replacing arrays rather than concatenating them; the shared file must still exist and contain `[qa]`. The local file must be untracked and git-ignored, and source permissions follow the file supplying each effective key.

Add these entries to the project's `.gitignore`:

```gitignore
.av/local.toml
.av/secrets.local.env
```

## Targets

`[env.targets]` names the origins QA may use. A scenario's `- **Target:** <name>` overrides its section origin.

| Target | Role |
|---|---|
| `ui` | Browser-facing origin for FE relative URLs. |
| `backend` | HTTP API origin for BE relative paths. |
| One target of any name | Serves both FE and BE. |
| Other names | Additional configured origins for explicit scenario targets, registration/login requests, cleanup or health probes. |

With multiple targets, FE requires `ui` and BE requires `backend` unless the scenario explicitly names another target. A missing reserved name is a `plan check` gap; the other section's origin is not substituted. A monolith can declare the same origin under both `ui` and `backend`.

Names match `[A-Za-z_][A-Za-z0-9_]*`. Origins are `http://host[:port]` or `https://host[:port]`, without userinfo, path (including a trailing `/`), query or fragment; omitted ports mean `80` or `443` for comparison.

Loopback means exactly `localhost`, `127.0.0.1`, `::1` or a host ending in `.localhost`, after lowercasing and stripping IPv6 brackets. It does not mean every address in `127.0.0.0/8`, a Compose service name or a bind address such as `0.0.0.0`.

Targets used by HTTP cleanup recipes or health probes require HTTPS off loopback, even with a local override or trust approval. A scenario using user fields, `$QA_TAG` or `$QA_NEW_PASSWORD` must use loopback or HTTPS for every origin it touches; `plan check` reports `credentials over cleartext origin <origin>` otherwise. At runtime testers refuse any credential-bearing request or form submission on non-loopback HTTP with `SKIP — cleartext origin refused: <origin>`.

Postgres/MySQL and Redis stores require exact-loopback hosts. The store schema has no TLS settings, so non-loopback store hosts are rejected with `non-loopback stores require TLS`, even with a local override or trust approval.

## Services

`[env.services]` is optional. Commands run through `/bin/sh -c` in the repository root with the engine's inherited environment.

| Key | Type / default | Meaning |
|---|---|---|
| `health` | Array of strings / `[]` | `<target>:<path>` probes; a responding status below `500` counts as up. |
| `up` | Non-empty string / omitted | Command to bring services up. |
| `prepare` | Array of non-empty strings / `[]` | Preparation commands in order, only after QA's own `up`. |
| `down` | Non-empty string / omitted | Command to tear services down, only if QA ran `up`. |

A probe such as `backend:/health` uses a configured target and a path starting with `/`, not `//`. The validator rejects `up` without a probe (`up requires at least one health probe`), `prepare` without `up` (`prepare requires up`), and `down` without `up` (`down requires up`).

Preparation runs against data that may persist, so commands must be idempotent. QA does not apply branch migrations on an already-running stack; the developer is responsible for those migrations. When probes show services are down, `qa.start_services = "ask"` asks once before `up` and `prepare` (headless stops); `"auto"` prints the scope and runs them. This choice is independent of `qa.fix`.

## Secrets and values

`[env.secrets]` contains engine-only inputs, never exposed to testers. `[env.values]` contains inputs that a plan can reference as `QA_<NAME>`; names match `[A-Za-z_][A-Za-z0-9_]*` and uppercase for the tester channel.

Both tables, store passwords and configured user fields use source strings:

| Source | Resolution |
|---|---|
| `cmd:<shell>` | Runs `/bin/sh -c` in the repository root with a 30-second timeout and the engine's inherited environment; strips trailing newlines from UTF-8 stdout. A non-zero exit, timeout or invalid/empty output stops resolution. |
| `env:<NAME>` | Reads the engine's inherited environment: what the harness had at startup, not exports made later in another shell. |
| `file:<path>#<KEY>` | Reads a named key from a dotenv file; relative paths start at the repository root. |
| `literal:<text>` | Uses the supplied text, subject to the rules below. |

In the committed `.av/config.toml`:

- `env:` names start with `AV_`, or `QA_` inside `[qa]`.
- `file:` paths are repository-relative, stay inside the repository and are git-ignored.
- No `literal:` secrets or user passwords are allowed; a store-password literal is allowed only on a loopback host.

`.av/local.toml` permits any valid environment name, absolute `file:` paths and secret literals; a relative `file:` path there must still stay inside the repository and be git-ignored.

`config` validates sources without resolving them. Resolution happens only after trust and only when needed; missing or empty values stop that operation. Source and trust displays mask declared literal payloads, but commands and recipe text remain visible: never put credentials directly in them.

## Stores

`[env.stores.<name>]` declares a named SQL or Redis endpoint for State Checks. Names match `[A-Za-z_][A-Za-z0-9_]*` and must be unique case-insensitively. Configuration validation does not test connectivity.

Network stores are loopback-only because the engine does not configure verified TLS for store clients. A remote/LAN endpoint is a configuration error, not a trust question. Ground a local endpoint in repository evidence; do not relabel a remote host as loopback. Inherited TLS settings such as `PGSSLMODE` or `PGSSLROOTCERT` are not a workaround: the loader and SQL cleanup discard inherited client settings.

| Key | SQL | Redis |
|---|---|---|
| `kind` | Required: `"sql"` | Required: `"redis"` |
| `engine` | Required: `"postgres"`, `"mysql"` or `"sqlite"` | Not supported |
| `host` | Required exact-loopback host for Postgres/MySQL; not supported for SQLite | Required exact-loopback host |
| `port` | Optional integer `1`–`65535`; defaults to `5432` / `3306`; not supported for SQLite | Optional integer `1`–`65535`; defaults to `6379` |
| `user`, `name` | Required non-empty connection username/database strings for Postgres/MySQL; not supported for SQLite | Not supported |
| `password` | Required source for Postgres/MySQL; not supported for SQLite | Optional source |
| `path` | Required for SQLite; relative paths resolve against the repository root; not supported for Postgres/MySQL | Not supported |
| `db` | Not supported | Optional non-negative integer; defaults to `0` |

Host, username, database name and file path are connection metadata, not value-source strings. Include a published non-default port explicitly rather than using a container's internal port.

BE scenarios can repeat `- **State Check:** <store>: <query> → <expected>`. An unprefixed check resolves only when exactly one store is configured; with zero or several stores it is a plan error. An unknown explicit store name is `missing.stores`. State Checks in FE scenarios are plan errors.

Only stores referenced by State Checks enter the private channel, under `STORE_<NAME_UPPER>_<CLIENT>` names. Each store has these native client aliases:

| Store | Native names |
|---|---|
| Postgres | `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `PGPASSWORD`, `PGOPTIONS` |
| MySQL | `MYSQL_HOST`, `MYSQL_TCP_PORT`, `MYSQL_USER`, `MYSQL_DATABASE`, `MYSQL_PWD` |
| SQLite | `SQLITE_DB` (absolute path) |
| Redis | `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, plus `REDISCLI_AUTH` only with a password source |

Source `qa_load_store='<name>' qa_load_require='<required-native-names>' . '<run-dir>/load.sh' || exit 1` before a check. These lower-case control assignments select the store and a space-separated list of required names without colliding with exposed `QA_*` values; use empty `qa_load_require` when none are needed. Do not pass arguments to the dot script: dash ignores them. The loader exports only exact `STORE_<NAME_UPPER>_<CLIENT>` variables for the fixed native client names listed above, requires a non-empty exported endpoint, then checks required names. Store names that share a prefix, such as `main` and `main_audit`, remain distinct. An unknown store fails with `<NAME_UPPER>: unknown store`; an empty endpoint fails with `<NAME_UPPER>: store not exported`. Without `qa_load_store`, native aliases are not exported. The loader consumes its controls, then clears inherited `QA_*`, `PG*`, `MYSQL_*`, `SQLITE_DB`, `REDIS*` and `STORE_*`, so stale client settings cannot choose an endpoint.

State Checks are read-only: Postgres exports `PGOPTIONS="-c default_transaction_read_only=on"`; SQLite uses `sqlite3 -readonly`; MySQL uses `--init-command="SET SESSION TRANSACTION READ ONLY"`. Redis checks use `redis-cli --json` and only `GET MGET EXISTS TTL TYPE STRLEN HGET HLEN LLEN LRANGE SCARD SISMEMBER ZCARD ZSCORE XLEN SCAN`. A client without `--json` cannot run the Redis check. Recommend a read-only SQL role or restricted Redis ACL as an additional boundary; client flags and tester instructions are not permission isolation. Cleanup may need a separate writable role or another recipe kind.

Run locks include every configured store endpoint as well as target origins: `sql:<host lower>:<port>/<name>` for Postgres/MySQL, `sql:<absolute path>` for SQLite, and `redis:<host lower>:<port>/<db>`. Locks coordinate identical configured keys only; undeclared stores and host aliases such as `localhost` versus `127.0.0.1` are not coordinated.

## QA policy

`[qa]` has three policy keys. Omitted keys default to `fix = "approve"`, `mutations = "deny"` and `start_services = "ask"`.

| Key / value | Meaning |
|---|---|
| `fix = "approve"` | Ask for one batch approval per fix iteration. |
| `fix = "auto"` | Apply eligible fixes after a scope banner. |
| `fix = "off"` | Test and report without source fixes. |
| `mutations = "allow"` | Guard nothing; use only when the data behind every target is disposable. |
| `mutations = "deny"` | Guard scenarios whose `- **Writes:**` line is `yes`, missing or invalid, and any scenario the syntactic scan sees writing. |
| `start_services = "ask"` | Ask once before configured bring-up; headless stops when bring-up is needed. |
| `start_services = "auto"` | Print the service scope, then run configured `up` and `prepare` when needed. |

Every FE and BE scenario must carry `- **Writes:** yes|no`. A `no` declaration does not override detected writes in methods, preconditions, steps, edges or State Checks. Registration counts as a write. Guarded scenarios are SKIP, not fix candidates; `"allow"` is a disposable-data decision, not just permission to test.

The dirty-tree gate follows `fix`: `approve` warns that fixes may overlap your work and asks Proceed/Abort (headless aborts), `auto` proceeds with the recorded baseline, and `off` skips the gate. Recovery never restores the whole tree or pre-existing dirty files.

Every failing assertion is a fix candidate, subject to the [fix guards](plugins/qa.md#safety). The engine's fixed limits are 3 iterations, 50 tester/fixer dispatches and 30 minutes; the authoritative final pass is counted but not limit-gated. `approve` without an interactive session tests and reports only.

`fix` does not gate application writes or service bring-up: `mutations` and `start_services` own those decisions. For a restrictive setup, combine `off` and `deny` and omit service bring-up/preparation commands, as in the [read-only example](#read-only-testreport).

## Users

`[qa.users.<name>]` configures **existing** accounts only. Names match `[a-z][a-z0-9_]*`; `new`, `captured` and names starting with `captured_` are reserved.

| Key | Meaning |
|---|---|
| `email` | Required source for the existing user's email. |
| `password` | Required secret source for its password. |
| `id` | Optional source; required only when the plan references that user's ID. |
| `description` | Required non-empty description of the user's role or state. |

The plan declares its users between `## Users` and the next level-two heading:

```markdown
## Users
- owner: registered — plain user who owns a CV
- admin: existing — administrator (qa.users.admin)
```

A plain user signup can create is `registered`; a role or state signup cannot produce is `existing`. Only existing users need configuration. A configured user must be declared `existing`, not `registered`; referencing an undeclared configured user is a plan error. Registered users' email/ID references are the tester's own values from its registration step, not channel names: the email is rebuilt from the dispatch `Tag:` and never passed through `capture.sh`, and BE testers capture the returned ID; using them before registration is `NEED_INFO kind=fixture` naming the user.

### Tester-visible names

| Input | Tester-visible names |
|---|---|
| Plan-referenced existing user `u` | Only the referenced fields among `QA_<U>_EMAIL`, `QA_<U>_PASSWORD` and `QA_<U>_ID` (ID only when configured); a login needs both the email and the password token |
| `[env.values]` entry `X` referenced by the plan | `QA_<X>` |
| Registration password | `QA_NEW_PASSWORD`, always written even with zero users |
| Dispatch tag | `Tag:` in the dispatch text, not a channel variable |
| Referenced store | `STORE_<NAME_UPPER>_<CLIENT>`; native aliases through `qa_load_store='<name>' . load.sh` |
| `[env.secrets]` | Never exposed to testers |

`QA_NEW_PASSWORD` is generated once per run as 24 random URL-safe characters plus `Aa1!` and reused by later provisioning. Each tester dispatch gets a distinct eight-character hexadecimal tag. Testers register through the application's signup API/form in preconditions, using an address such as `qa+<Tag>-owner@test.local` and the run password, and log in themselves. The engine does neither.

The engine scans `$QA_NAME` and `${QA_NAME}` throughout scenarios. `TAG` and `NEW_PASSWORD` are engine-issued; token/cookie suffixes are always plan errors because testers obtain them at runtime. `EMAIL|PASSWORD|ID` resolves against the longest declared/configured user prefix before ordinary values. An existing user without configuration is `missing.users`; an absent ID source is also a user gap. Undeclared user-shaped references are plan errors; other unknown references are `missing.values`. Value names cannot use credential suffixes, `TAG`, `NEW_PASSWORD`, `CAPTURED_*` or an exposed user's field name.

BE testers capture credentials through `capture.sh` into `results/<dispatch>/captured.env`, then source it before sanitising that same response. Immediately after successful registration, testers call `sh '<run-dir>/capture.sh' <dispatch> --account <email> [<id>]` to record the account in the durable ledger before any other step. A non-zero exit is `NEED_INFO kind=fixture` naming the email. Optional `accounts: [{email, id}]` in `qa-results` is a secondary recording path, not permission to skip immediate recording when the helper works.

Recorded emails must match `[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,253}` in full, contain the dispatch tag in the local part case-insensitively, and differ from every configured email in the run's channel. IDs are absent or match `[A-Za-z0-9._:-]{1,128}` in full. Repeated recording is idempotent on `(run_id, dispatch, email)` and only fills a missing ID; it never resets cleanup attempts or status.

## Cleanup

Optional `[qa.cleanup]` defines exactly one recipe for **registered accounts**, never configured existing users. Prefer an evidenced SQL deletion by email; otherwise use an evidenced HTTP endpoint or management command. SQL and HTTP recipes must reference `{email}` and may not delete by ID alone.

### SQL recipe

| Key | Meaning |
|---|---|
| `kind` | `"sql"` |
| `store` | Configured store of kind `"sql"` |
| `query` | Non-empty SQL text containing `{email}` |

Only `{email}`, `{id}` and `{tag}` are allowed. Each substitutes a single-quoted SQL literal with `'` doubled; an absent ID becomes `NULL`. Do not add your own quotes around placeholders. Cleanup runs the store's client in a sanitised environment without State Check read-only overrides; success is exit 0. Postgres cleanup discards inherited `PSQLRC` and uses `psql -X` to skip system and user startup files. MySQL cleanup uses `--no-defaults --no-login-paths` to skip option files, including `.mylogin.cnf`. Check the application's foreign-key cascades and deletion contract before approving the query.

### HTTP recipe

| Key | Type / default | Meaning |
|---|---|---|
| `kind` | String / required | `"http"` |
| `target` | String / required | Existing target name; HTTPS unless loopback |
| `method` | String / required | `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE` or `OPTIONS` |
| `path` | String / required | Target-relative path starting `/`, not `//`; query allowed, no backslash or fragment |
| `headers` | Table of strings / `{}` | Request headers |
| `json` | Table / omitted | JSON-compatible nested tables, arrays, strings, numbers and booleans |
| `form` | Table of strings / omitted | URL-encoded form; mutually exclusive with `json` |
| `expect` | Non-empty integer array / required | Successful HTTP statuses from `100` to `599` |

Templates allow `{email}`, `{id}`, `{tag}`, `{secret.X}` and `{value.X}`; secret/value names must exist in their environment tables. Identity placeholders in the path/query are percent-encoded; body/header substitutions retain raw values. Requests never follow redirects. If the recipe uses `{id}`, an account without an ID counts as a failed cleanup attempt without sending a request.

### Command recipe

| Key | Meaning |
|---|---|
| `kind` | `"command"` |
| `run` | Non-empty shell command run in the repository root with a 60-second timeout |

Commands have no placeholders or credential outputs. They receive the inherited environment with every `QA_*` variable removed, plus the ledger record's `QA_EMAIL`, `QA_ID` (empty when unknown) and `QA_TAG`. Success is exit 0. Commands are trust-pinned, not sandboxed.

For an admin key, use an inherited `AV_<NAME>` variable and also declare it under `[env.secrets]` as `env:AV_<NAME>`. For example, `SERVICE_KEY = "env:AV_SUPABASE_SERVICE_ROLE_KEY"` lets `engine.log` mask the value while the helper reads `$AV_SUPABASE_SERVICE_ROLE_KEY` from its inherited environment. The runtime collects every `env:`-referenced value for masking even when no recipe resolves the source; this declaration does not inject it into the helper. A helper fetching a key any other way, including its own settings or `.env`, must never print it, including on failure.

### Missing cleanup and durable outcomes

Registered users without a recipe set `missing.cleanup = true`, but this soft gap does not make `plan check` fail. Interactive runs offer one scoped extension; no grounded proposal, preview errors after the single retry, a declined gate or a headless run keeps the valid config and continues with `No cleanup recipe: registered accounts will remain in the application.` Required gaps and invalid config still stop.

The ledger at `${XDG_STATE_HOME:-~/.local/state}/av-marketplace/qa-accounts.json` is keyed by repository realpath and survives private-run-directory deletion. Records hold email, optional ID, run ID, dispatch tag/ID, cleanup destination, `deleted`, `status` and `attempts`, never passwords or tokens. Status is `pending`, `deleted` or `manual-cleanup`.

Teardown uses only the **current** valid, trusted (or `not-required`) cleanup config, and an HTTP origin or SQL store endpoint must be among this run's locked keys. An unavailable/untrusted recipe or unlocked destination leaves accounts without counting attempts. A record's non-null destination must match the current destination; mismatches stay `left` without attempts. A null destination adopts the current destination on its first attempt; command recipes have no destination.

Before rendering a recipe, teardown revalidates persisted email/ID safety, dispatch tags, destinations and attempt counters. Legacy QA 3.1.0 records lacking `tag` or `destination`, and malformed current records, stay unchanged without an attempt; their non-empty email strings are listed in `left`, with indexed stderr diagnostics that do not echo unsafe values. A record without a usable email string gets only the diagnostic. A missing destination is not a null destination and never adopts the current recipe or derives authorization from legacy `origin`. Deliberately clean up legacy accounts in their original application; other eligible records continue processing.

A usable recipe attempts eligible undeleted records from any run of this repository. Success marks them `deleted`; failure increments attempts. At 3 failed attempts the record becomes `manual-cleanup` and is reported once under `manual`, never retried automatically. Earlier failures are `left`; later teardowns omit already-deleted/manual records. Review the reported `deleted`, `left` and `manual` emails and deliberately remove manual leftovers in the correct application.

## Examples

These are complete schema examples for the stated application contracts, not autodetection results or proof that your checkout exposes these routes. Match ports, service commands, user models and settings to repository evidence before approval, and add the [private ignore entries](#files).

### JWT API: `/register` and `/login`

For an API where registration returns `201 {"id": ...}` and login returns `200 {"token": ...}`, the tester calls `/register` with `qa+$QA_TAG-owner@test.local` and `$QA_NEW_PASSWORD`, immediately records the returned ID, then obtains and captures the token through `/login`. The plan declares `owner: registered` and marks these scenarios `Writes: yes`.

```toml
version = 1

[env.targets]
backend = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "allow"
start_services = "ask"

[qa.cleanup]
kind = "command"
run = "sh scripts/delete-qa-user.sh"
```

This example assumes the application's management helper `scripts/delete-qa-user.sh` deletes by `QA_EMAIL` and returns exit 0 on success. Use the actual evidenced helper, or a SQL recipe on a configured SQL store; do not invent a deletion route. Tokens stay in the tester's capture file, never in the plan.

### Django session stack: signup, login and cleanup

This example assumes a signup form, Django's standard user model, and the same settings/database as the target server. Testers register and log in through the app, carrying CSRF/session cookies in their own session. Custom user models or tenant settings need a repository-specific cleanup command.

```toml
version = 1

[env.targets]
ui = "http://localhost:8000"
backend = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "allow"
start_services = "ask"

[qa.cleanup]
kind = "command"
run = '''python3 manage.py shell --verbosity 0 -c '
import os
from django.contrib.auth import get_user_model

get_user_model().objects.filter(email=os.environ["QA_EMAIL"]).delete()
' '''
```

The cleanup command deletes by the registered email, without needing an ID or password. The management command operates on the app's real configured data, so this setup requires disposable data.

### Existing accounts from `.av/secrets.local.env`

Pre-existing non-privileged users can be used when signup is unavailable; privileged roles should be configured only when the scenario requires them. Existing users are never enrolled for deletion.

```toml
version = 1

[env.targets]
backend = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "deny"
start_services = "ask"

[qa.users.admin]
email = "file:.av/secrets.local.env#QA_ADMIN_EMAIL"
password = "file:.av/secrets.local.env#QA_ADMIN_PASSWORD"
id = "file:.av/secrets.local.env#QA_ADMIN_ID"
description = "Administrator; can inspect every CV"
```

Declare `admin: existing — administrator` under the plan's `## Users`. Populate `QA_ADMIN_EMAIL`, `QA_ADMIN_PASSWORD` and `QA_ADMIN_ID` privately in that git-ignored dotenv file; remove the optional ID source if the plan does not need it. Testers log in using the referenced email/password. No exported harness variables or harness restart is needed. Login itself may write session state and must be marked accordingly; `deny` guards such scenarios.

### Read-only test/report

For anonymous read scenarios on an already-running app, with no stores, users or service lifecycle commands:

```toml
version = 1

[env.targets]
backend = "http://localhost:8000"
ui = "http://localhost:5173"

[qa]
fix = "off"
mutations = "deny"
start_services = "ask"
```

Mark each read scenario `Writes: no`. This disables source fixes and guards declared or detected writes, including registration; `fix = "off"` skips the dirty-tree gate. Static mutation detection is not a guarantee of read-only behavior (GET side effects and implicit UI writes remain possible).

### Supabase local stack

The local stack exposes signup on `:54321` and Postgres on `:54322`. Testers register through `/auth/v1/signup` on the explicitly configured `supabase` origin, with the plan-referenced anon key. These sources require the Supabase CLI and `jq`; QA does not install them.

```toml
version = 1

[env.targets]
ui = "http://localhost:5173"
backend = "http://localhost:8000"
supabase = "http://127.0.0.1:54321"

[env.services]
health = ["backend:/health", "ui:/"]

[env.values]
SUPABASE_ANON_KEY = "cmd:supabase status --output json | jq -r .ANON_KEY"

[env.stores.supabase]
kind = "sql"
engine = "postgres"
host = "127.0.0.1"
port = 54322
user = "postgres"
name = "postgres"
password = "literal:postgres"

[qa]
fix = "approve"
mutations = "allow"
start_services = "ask"

[qa.cleanup]
kind = "sql"
store = "supabase"
# Application contract: public.cvs.user_id references auth.users(id) ON DELETE CASCADE.
query = "DELETE FROM auth.users WHERE email = {email}"
```

The `cvs` cascade in this example is an application requirement, not an assumed Supabase default: verify and cite the migration defining it before approving cleanup. If related rows or storage objects do not cascade, deletion of the auth user alone does not prove they were removed. Plans declare `user` and `other` as registered and use captured IDs for ownership checks; repeated `State Check: supabase: …` lines can verify CV and storage state. Add repository-grounded lifecycle commands only for managed bring-up; this example checks an already-running stack.

## Trust

QA pins every value source (including configured users and store passwords), `env.services`, non-loopback target origins, the whole `qa.cleanup` table, and configured `qa.fix`, `qa.mutations` and `qa.start_services`. Store hosts must be loopback; trust cannot authorize a remote store. A SQL cleanup also pins its store's **whole table**, so changing the engine, path, database name or port re-asks for trust. Pins live outside branch content at `~/.local/state/av-marketplace/trust.json` (or `${XDG_STATE_HOME}/av-marketplace/trust.json`), keyed by repository realpath and plugin name; each worktree trusts separately.

| Reported `trust` | Meaning |
|---|---|
| `not-required` | The sensitive subset is empty. |
| `new` | This repository/plugin has no recorded pin for a non-empty subset. |
| `trusted` | The current subset matches the recorded hash. |
| `changed` | A pin exists, but the effective subset has changed. |

The hash covers full, unmasked canonical JSON, while `trust_subset` masks literal payloads. Review the complete subset for `new` or `changed`, then approve once; `trust accept <hash>` records it only if the current subset still matches. Pins cover source definitions, not command output or current dotenv/environment values, and never bypass other approval or mutation gates.

### Headless runners and CI

A headless runner cannot silently approve a new or changed configuration, and it cannot run the interactive bootstrap. Required config gaps, failed service setup or unaccepted trust stop with keys/errors and a pointer to this page; missing cleanup alone continues with registered accounts left in the application. Bring-up with `start_services = "ask"` stops headless; pre-approved `"auto"` can run configured services. Prepare `.av/config.toml` and private inputs ahead of time. In a controlled CI setup step, review `config`'s complete `trust_subset` and record the approved hash through the plugin engine's `trust accept <hash>` before running it. Do not auto-accept arbitrary branch content in the work step; the setup is the trust decision.

For QA, using the **installed** engine path resolved by the [engine skill](../plugins/qa/skills/engine/SKILL.md#resolve-the-installed-script):

```bash
python3 "$QA_ENGINE" config --repo "$PROJECT_ROOT"
# After review, set APPROVED_TRUST_HASH to that configuration's trust_hash.
python3 "$QA_ENGINE" trust accept "$APPROVED_TRUST_HASH" --repo "$PROJECT_ROOT"
```

`QA_ENGINE`, `PROJECT_ROOT` and `APPROVED_TRUST_HASH` are shell variables for this example, not configuration sources. The engine rechecks the hash at acceptance. A new checkout/worktree or changed sensitive subset needs its own setup approval.

## Tables by plugin

| Table | Owner and key reference |
|---|---|
| `[env]` | Shared environment; [this page](#configuration). |
| `[qa]` | QA; [policy](#qa-policy), [users](#users) and [cleanup](#cleanup). |

Register a new plugin's table here with a link to its full key reference; keep plugin-specific policies and recipes out of `[env]`.
