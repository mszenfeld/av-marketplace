# Configuration

QA reads `.av/config.toml` from the project's repository root. On the first interactive run, `/qa:run` proposes it from repository evidence and asks two questions: whether the data is disposable and how to handle fixes.

## Minimal file

```toml
version = 1

[env.targets]
ui = "http://localhost:5173"
backend = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "rejections-only"
```

Add [services](#services) when QA should check or manage the running stack.
Add [secrets and values](#secrets-and-values) when recipes or scenarios need private inputs.
Add a [database](#database) when scenarios include database checks.
Add [test accounts](#test-accounts) when scenarios need authenticated personas.

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
| `ui` | Browser-facing origin; preferred for FE relative URLs. |
| `backend` | HTTP API origin; preferred for BE relative paths. |
| One target of any name | Serves both FE and BE. |
| Other names | Origins needed only by account recipes or health probes. |

If a section's reserved name is absent, it falls back to the other reserved name; if neither exists, only a single target can serve both sections. With multiple targets and no reserved name, `plan check` names the missing `ui` or `backend` origin.

Names match `[A-Za-z_][A-Za-z0-9_]*`. Origins are `http://host[:port]` or `https://host[:port]`, without userinfo, path (including a trailing `/`), query or fragment; omitted ports mean `80` or `443` for comparison.

Loopback means exactly `localhost`, `127.0.0.1`, `::1` or a host ending in `.localhost`, after lowercasing and stripping IPv6 brackets. It does not mean every address in `127.0.0.0/8`, a Compose service name or a bind address such as `0.0.0.0`.

Targets used by HTTP account recipes or health probes require HTTPS off loopback, even with a local override or trust approval.

## Services

`[env.services]` is optional. Commands run through `/bin/sh -c` in the repository root with the engine's inherited environment.

| Key | Type / default | Meaning |
|---|---|---|
| `health` | Array of strings / `[]` | `<target>:<path>` probes; a responding status below `500` counts as up. |
| `up` | Non-empty string / omitted | Command to bring services up. |
| `prepare` | Array of non-empty strings / `[]` | Preparation commands in order, such as migrations. |
| `down` | Non-empty string / omitted | Command to tear services down. |

A probe such as `backend:/health` uses a configured target and a path starting with `/`, not `//`. QA runs `down` only when it ran `up`.

## Secrets and values

`[env.secrets]` contains engine-only inputs, never exposed to testers. `[env.values]` contains inputs that a plan can reference as `QA_<NAME>`; names match `[A-Za-z_][A-Za-z0-9_]*` and uppercase for the tester channel.

Both tables, database passwords and static account fields use source strings:

| Source | Resolution |
|---|---|
| `cmd:<shell>` | Runs `/bin/sh -c` in the repository root with a 30-second timeout and the engine's inherited environment; strips trailing newlines from UTF-8 stdout. A non-zero exit, timeout or invalid/empty output stops resolution. |
| `env:<NAME>` | Reads the engine's inherited environment: what the harness had at startup, not exports made later in another shell. |
| `file:<path>#<KEY>` | Reads a named key from a dotenv file; relative paths start at the repository root. |
| `literal:<text>` | Uses the supplied text, subject to the rules below. |

In the committed `.av/config.toml`:

- `env:` names start with `AV_`, or `QA_` inside `[qa]`.
- `file:` paths are repository-relative, stay inside the repository and are git-ignored.
- No `literal:` secrets or static passwords are allowed; a database-password literal is allowed only on a loopback host.

`.av/local.toml` permits any valid environment name, absolute `file:` paths and secret literals; a relative `file:` path there must still stay inside the repository and be git-ignored.

`config` validates sources without resolving them. Resolution happens only after trust and only when needed; missing or empty values stop that operation. Source and trust displays mask declared literal payloads, but commands and recipe text remain visible: never put credentials directly in them.

## Database

`[env.database]` is optional; when configured, choose one database kind and its connection fields. Configuration validation does not test connectivity.

| Key | Type / default | Meaning |
|---|---|---|
| `kind` | String / required | `postgres`, `mysql` or `sqlite`. |
| `host` | Non-empty string / required for Postgres/MySQL | Connection host, not a source; omitted for SQLite. |
| `port` | Integer / `5432` for Postgres, `3306` for MySQL | Optional port from `1` to `65535`; specify a published non-default port explicitly; omitted for SQLite. |
| `user` | Non-empty string / required for Postgres/MySQL | Connection username, not a source; omitted for SQLite. |
| `name` | Non-empty string / required for Postgres/MySQL | Database name, not a source; omitted for SQLite. |
| `password` | Non-empty source string / required for Postgres/MySQL | Uses the source rules above; omitted for SQLite. |
| `path` | Non-empty string / required for SQLite | Database file path, resolved against the repository root when relative; omitted for Postgres/MySQL. |

For database checks, QA exposes the matching client variables: `PG*`, `MYSQL_*` or `SQLITE_DB`.

## QA policy

`[qa]` has two policy keys. Omitted keys default to `fix = "approve"` and `mutations = "rejections-only"`.

| Key / value | Meaning |
|---|---|
| `fix = "approve"` | Ask for one batch approval per fix iteration. |
| `fix = "auto"` | Apply eligible fixes after a scope banner; start configured services without a bring-up question. |
| `fix = "off"` | Test and report without source fixes. |
| `mutations = "allow"` | Guard nothing; use only when the data behind every target is disposable. |
| `mutations = "rejections-only"` | Guard writes except grounded rejection-only BE scenarios without other write steps or write preconditions. |
| `mutations = "deny"` | Guard all detected writes and forbid account provisioning. |

The dirty-tree gate follows `fix`: `approve` warns that fixes may overlap your work and asks Proceed/Abort (headless aborts), `auto` proceeds with the recorded baseline, and `off` skips the gate. Recovery never restores the whole tree or pre-existing dirty files.

Every failing assertion is a fix candidate, subject to the [fix guards](plugins/qa.md#safety). The engine's fixed limits are 3 iterations, 50 tester/fixer dispatches and 30 minutes; the authoritative final pass is counted but not limit-gated. `approve` without an interactive session tests and reports only.

`fix` does not gate HTTP writes or provisioning; `mutations` does. For a restrictive setup, combine `off` and `deny` and omit service bring-up/preparation commands, as in the [read-only example](#read-only-testreport).

## Test accounts

`[qa.accounts]` declares personas and shared recipes. Only personas referenced by the plan are used; provisioned passwords are always generated as 24 random URL-safe characters plus `Aa1!`.

| Key | Meaning |
|---|---|
| `personas` | Distinct names matching `[a-z][a-z0-9_]*`; defaults to `[]`. |
| `email` | Required for provisioning; template using `{run}` (eight-character run ID) and `{persona}`. |
| `static.<p>` | Pre-existing persona with `email` and `password` sources and an optional `id` source; adds the persona and wins over provisioning. |
| `create` | Recipe required to provision a non-static persona. |
| `confirm` | Optional post-create confirmation recipe. |
| `login` | Optional login recipe; required for token/cookie capabilities, including static personas. |
| `delete` | Optional cleanup recipe; without it, provisioned accounts are reported `left`. |

Recipes choose `kind = "http"` or `"command"`. Static accounts are never created or deleted. Reserve persona field names such as `USER_TOKEN` rather than using them as exposed-value names; plan checks distinguish credentials from other values using the longest configured persona prefix and a recognized field.

### HTTP recipes

| Key | Type / default | Meaning |
|---|---|---|
| `kind` | String / required | `http`. |
| `target` | String / required | Existing target name; HTTPS unless loopback. |
| `method` | String / required | `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE` or `OPTIONS`. |
| `path` | String / required | Target-relative path starting `/`, not `//`; query allowed, no backslash or fragment. |
| `headers` | Table of strings / `{}` | Request headers. |
| `json` | Table / omitted | JSON-compatible nested tables, arrays, strings, numbers and booleans. |
| `form` | Table of strings / omitted | URL-encoded form; mutually exclusive with `json`. |
| `expect` | Non-empty integer array / required | Successful HTTP statuses from `100` to `599`. |
| `conflict` | Non-empty integer array / omitted | Create statuses that prove nothing was created; one retry with a fresh email suffix. |
| `id` | String extractor / omitted | Dotted JSON path such as `.id`, `.data.user.id` or `.items[0].id`. |
| `token` | String extractor / omitted | Same extractor grammar; login exposes `QA_<P>_TOKEN`. |
| `cookies` | Non-empty string array / omitted | Set-Cookie names with distinct, non-empty normalized forms. |

String templates can use `{email}`, `{password}`, `{persona}`, `{run}`, `{id}`, `{secret.X}` and `{value.X}`. Delete cannot use `{password}` because earlier runs' private passwords are gone. Recipe strings are templates, not source strings; HTTP requests are sent by the engine without redirects or credentials on process argv.

### Command recipes

| Key | Type / default | Meaning |
|---|---|---|
| `kind` | String / required | `command`. |
| `run` | Non-empty string / required | Shell command run in the repository root. |
| `outputs` | Array or table / `{}` | Array entries `id`/`token`, or a table such as `{ id = true, token = true, cookies = ["sessionid", "csrftoken"] }`, declaring only needed fields. |

Commands get the engine's inherited environment plus `QA_PERSONA`, `QA_EMAIL`, `QA_PASSWORD` and `QA_ID`; delete receives no `QA_PASSWORD`. Credentials are not substituted into command text or argv. A helper needing an admin key must obtain it from its own settings or `.env`, not a configured source.

Declared outputs must be exactly one JSON object: `id`/`token` scalar strings or IDs, and a `cookies` map with precisely the declared names and non-empty values. With no outputs, no identity fields are collected; exit 0 means success. Every command delete requires a known ledger ID, while HTTP delete requires one only when its templates use `{id}`. Commands are trust-pinned, not sandboxed; multi-request session/CSRF login belongs here.

### Tester-visible names

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

## Examples

These are complete schema examples for the stated application contracts, not autodetection results or proof that your checkout exposes these routes. Match ports, extractors, service commands, cookie names, user models and settings to repository evidence before approval, and add the [private ignore entries](#files).

### JWT API: `/register` and `/login`

For an API where registration returns `201 {"id": ...}` and login returns `200 {"token": ...}`:

```toml
version = 1

[env.targets]
backend = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "allow"

[qa.accounts]
personas = ["user"]
email = "qa+{run}-{persona}@test.local"

[qa.accounts.create]
kind = "http"
target = "backend"
method = "POST"
path = "/register"
json = { email = "{email}", password = "{password}" }
expect = [201]
id = ".id"

[qa.accounts.login]
kind = "http"
target = "backend"
method = "POST"
path = "/login"
json = { email = "{email}", password = "{password}" }
expect = [200]
token = ".token"
```

Use `$QA_USER_TOKEN` in authenticated scenarios. This deliberately has no delete recipe: accounts are reported `left`. Add the app's evidenced cleanup API/management command rather than inventing a deletion route. Add `conflict = [409]` to create only if that status proves nothing was created; the engine retries once with a fresh email suffix, not on arbitrary failures.

### Django session stack: command recipe and CSRF cookie

This example assumes Django's standard username-based user model, `/accounts/login/`, `sessionid`/`csrftoken` cookies, a CSRF cookie (not `CSRF_USE_SESSIONS`), and `localhost` in `ALLOWED_HOSTS`. Run `manage.py` with the same settings, database and shared session backend as the target server. The login command calls the app through Django's in-process client with CSRF checks enabled: GET the form, POST with its CSRF token, retain the rotated cookies. It does not follow redirects. Custom models, forms or tenant/session settings need a repository-specific recipe instead.

```toml
version = 1

[env.targets]
app = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "allow"

[qa.accounts]
personas = ["user"]
email = "qa+{run}-{persona}@test.local"

[qa.accounts.create]
kind = "command"
run = '''python3 manage.py shell --verbosity 0 -c '
import json
import os
from django.contrib.auth import get_user_model

user = get_user_model().objects.create_user(
    username=os.environ["QA_EMAIL"],
    email=os.environ["QA_EMAIL"],
    password=os.environ["QA_PASSWORD"],
)
print(json.dumps({"id": str(user.pk)}))
' '''
outputs = { id = true }

[qa.accounts.login]
kind = "command"
run = '''python3 manage.py shell --verbosity 0 -c '
import json
import os
from django.test import Client

client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
client.get("/accounts/login/")
response = client.post("/accounts/login/", {
    "username": os.environ["QA_EMAIL"],
    "password": os.environ["QA_PASSWORD"],
    "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
})
if response.status_code != 302 or "sessionid" not in client.cookies:
    raise SystemExit("login failed")
print(json.dumps({"cookies": {
    name: client.cookies[name].value for name in ("sessionid", "csrftoken")
}}))
' '''
outputs = { cookies = ["sessionid", "csrftoken"] }

[qa.accounts.delete]
kind = "command"
run = '''python3 manage.py shell --verbosity 0 -c '
import os
from django.contrib.auth import get_user_model

get_user_model().objects.filter(
    pk=os.environ["QA_ID"], email=os.environ["QA_EMAIL"],
).delete()
print("{}")
' '''
```

A BE write sends `Cookie: $QA_USER_COOKIE` and `X-CSRFToken: $QA_USER_COOKIE_CSRFTOKEN`. In general the latter name is `QA_<P>_COOKIE_CSRFTOKEN`. Never copy cookie values into a plan or the command text. The command receives credentials privately through its environment and returns them to the engine, not chat. Its create/delete commands operate on the real configured database, so this setup requires disposable data.

### Explicit static accounts from `.av/secrets.local.env`

Pre-existing non-privileged test accounts can be used when signup is unavailable. This example assumes the JWT `/login` contract above; static accounts are not created or deleted by QA.

```toml
version = 1

[env.targets]
backend = "http://localhost:8000"

[qa]
fix = "approve"
mutations = "rejections-only"

[qa.accounts.static.user]
email = "file:.av/secrets.local.env#QA_USER_EMAIL"
password = "file:.av/secrets.local.env#QA_USER_PASSWORD"
id = "file:.av/secrets.local.env#QA_USER_ID"

[qa.accounts.login]
kind = "http"
target = "backend"
method = "POST"
path = "/login"
json = { email = "{email}", password = "{password}" }
expect = [200]
token = ".token"
```

Populate `QA_USER_EMAIL`, `QA_USER_PASSWORD` and `QA_USER_ID` privately in that git-ignored dotenv file. `id` is optional: remove the source if the plan does not need an ID. No exported harness variables or harness restart is needed. Static does not mean preauthenticated: the login still runs on provisioning and every tester dispatch.

### Read-only test/report

For anonymous read scenarios on an already-running app, with no account or service lifecycle commands:

```toml
version = 1

[env.targets]
backend = "http://localhost:8000"
ui = "http://localhost:5173"

[qa]
fix = "off"
mutations = "deny"
```

This disables source fixes, guards detected writes and prevents provisioning; `fix = "off"` skips the dirty-tree gate. Authenticated plans need explicit static accounts; configured login recipes can themselves mutate session state, so review them separately. Static mutation detection is not a guarantee of read-only behavior (GET side effects and implicit UI writes remain possible).

### Supabase local stack

Supabase's local stack exposes the GoTrue admin API on `:54321`; the service-role key stays engine-only. The anon key can reach testers when referenced by a plan. These sources require the Supabase CLI and `jq`; QA does not install them.

```toml
version = 1

[env.targets]
ui = "http://localhost:5173"
backend = "http://localhost:8000"
supabase = "http://127.0.0.1:54321"

[env.services]
health = ["backend:/health", "ui:/"]

[env.secrets]
SERVICE_KEY = "cmd:supabase status --output json | jq -r .SERVICE_ROLE_KEY"

[env.values]
SUPABASE_ANON_KEY = "cmd:supabase status --output json | jq -r .ANON_KEY"

[env.database]
kind = "postgres"
host = "127.0.0.1"
port = 54322
user = "postgres"
name = "postgres"
password = "literal:postgres"

[qa]
fix = "approve"
mutations = "allow"

[qa.accounts]
personas = ["user", "other"]
email = "qa+{run}-{persona}@test.local"

[qa.accounts.create]
kind = "http"
target = "supabase"
method = "POST"
path = "/auth/v1/admin/users"
headers = { apikey = "{secret.SERVICE_KEY}", Authorization = "Bearer {secret.SERVICE_KEY}" }
json = { email = "{email}", password = "{password}", email_confirm = true }
expect = [200, 201]
id = ".id"

[qa.accounts.login]
kind = "http"
target = "supabase"
method = "POST"
path = "/auth/v1/token?grant_type=password"
headers = { apikey = "{value.SUPABASE_ANON_KEY}" }
json = { email = "{email}", password = "{password}" }
expect = [200]
token = ".access_token"

[qa.accounts.delete]
kind = "http"
target = "supabase"
method = "DELETE"
path = "/auth/v1/admin/users/{id}"
headers = { apikey = "{secret.SERVICE_KEY}", Authorization = "Bearer {secret.SERVICE_KEY}" }
expect = [200, 204]
```

The database fields use the Supabase CLI's default local Postgres connection. A plan can send `Authorization: Bearer $QA_USER_TOKEN`, use `$QA_OTHER_TOKEN` for ownership checks and name `supabase` explicitly for auth requests. HTTP login/delete placeholders resolve even if the plan never references `$QA_SUPABASE_ANON_KEY`. Add repository-grounded service lifecycle commands only for managed bring-up; this example checks an already-running stack.

## Trust

QA pins every source string, every account recipe, `env.services`, non-loopback target origins and database hosts, and configured `qa.fix` and `qa.mutations`. Pins live outside branch content at `~/.local/state/av-marketplace/trust.json` (or `${XDG_STATE_HOME}/av-marketplace/trust.json`), keyed by repository realpath and plugin name; each worktree trusts separately.

| Reported `trust` | Meaning |
|---|---|
| `not-required` | The sensitive subset is empty. |
| `new` | This repository/plugin has no recorded pin for a non-empty subset. |
| `trusted` | The current subset matches the recorded hash. |
| `changed` | A pin exists, but the effective subset has changed. |

The hash covers full, unmasked canonical JSON, while `trust_subset` masks literal payloads. Review the complete subset for `new` or `changed`, then approve once; `trust accept <hash>` records it only if the current subset still matches. Pins cover source definitions, not command output or current dotenv/environment values, and never bypass other approval or mutation gates.

### Headless runners and CI

A headless runner cannot silently approve a new or changed configuration, and it cannot run the interactive bootstrap. It stops with the missing keys, failed recipe/probe or trust reason and a pointer to this page. Prepare `.av/config.toml` and private inputs ahead of time. In a controlled CI setup step, review `config`'s complete `trust_subset` and record the approved hash through the plugin engine's `trust accept <hash>` before running it. Do not auto-accept arbitrary branch content in the work step; the setup is the trust decision.

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
| `[qa]` | QA; [policy](#qa-policy) and [test accounts](#test-accounts). |

Register a new plugin's table here with a link to its full key reference; keep plugin-specific policies and recipes out of `[env]`.
