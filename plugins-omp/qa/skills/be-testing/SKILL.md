---
name: "qa:be-testing"
description: Backend testing patterns — API request construction, response verification, database state checks, error handling testing, and adaptive tool detection.
allowed-tools: Bash(curl:*), Bash(httpie:*), Bash(http:*), Bash(wget:*), Bash(psql:*), Bash(sqlite3:*), Bash(mysql:*), Bash(mongosh:*), Bash(redis-cli:*), Bash(command:*), Bash(printf:*), Bash([:*), Bash(cut:*), Bash(jq:*), Bash(grep:*), Bash(cat:*), Bash(head:*), Bash(tail:*), Read, Write, Bash(mkdir:*)
---

# Backend Testing Patterns

## Tool Detection

**ALWAYS run this check first:**

```bash
# HTTP clients
command -v curl >/dev/null 2>&1 && printf 'curl: available\n' || printf 'curl: unavailable\n'
command -v http >/dev/null 2>&1 && printf 'httpie: available\n' || printf 'httpie: unavailable\n'

# Database clients
command -v psql >/dev/null 2>&1 && printf 'psql: available\n' || printf 'psql: unavailable\n'
command -v sqlite3 >/dev/null 2>&1 && printf 'sqlite3: available\n' || printf 'sqlite3: unavailable\n'
command -v mysql >/dev/null 2>&1 && printf 'mysql: available\n' || printf 'mysql: unavailable\n'
command -v mongosh >/dev/null 2>&1 && printf 'mongosh: available\n' || printf 'mongosh: unavailable\n'
command -v redis-cli >/dev/null 2>&1 && printf 'redis-cli: available\n' || printf 'redis-cli: unavailable\n'

# JSON processing and fail-closed response sanitiser
command -v jq >/dev/null 2>&1 && printf 'jq: available\n' || printf 'jq: unavailable\n'
perl -MJSON::PP -e 1 >/dev/null 2>&1 && printf 'perl: available\n' || printf 'perl: unavailable\n'
```

Use an available HTTP client, but use `curl` for any request containing a credential: HTTPie's inline header/body arguments expose secret values in process argv. If `curl` is unavailable for a credential-bearing request, return `NEED_INFO kind=tool, Missing: curl` without sending it. If no HTTP client, or no `perl` with `JSON::PP`, is available and the scenarios apply, every API scenario is `NEED_INFO kind=tool, Missing: curl` (or `perl`). A DB client missing only blocks its DB check.

### Connection selection

Tool availability alone does not establish a connection to the test database. The dispatch's `Database: postgres|mysql|sqlite|none` selects the only permitted DB client connection, loaded from the run directory. `Database: none` → `**DB check:** SKIP — Database: none`, while HTTP still runs. Never discover connection values or use a preconfigured database MCP server: the dispatch declares no MCP connection. This is an instruction, not a tool-level permission boundary; remove write-capable database MCP servers before QA runs.

---

## Execution Workflow

For each BE scenario from the test plan:

1. **Read the scenario** — understand method, endpoint, payload, expected response, DB checks
2. **Execute the request** — send HTTP request with proper method, headers, body
3. **Verify response** — check status code, response body structure, specific values
4. **Verify DB state** (if DB Check specified) — run query, compare against expected
5. **Execute edge cases** — run each edge case as a sub-test
6. **Record result** — PASS/FAIL/SKIP/NEED_INFO with response details

The dispatch supplies `Plan:`, `Run dir:`, `Secrets file:`, `Secrets JSON:`, `Redact names file:`, `Targets:` (named origins and the default target for BE), `Database:` and `Guarded:`, followed by `BE Test Scenarios:` in plan order (C11). Do not parse the plan's optional `## Setup` notes. A scenario in `Guarded:` is `SKIP — mutation-guard`; execute none of its preconditions, main flow or edges. Values and supported names come from C3's run-directory channel only.

## Tester scope

These limits apply to the tester's own recovery actions as well as plan steps. Never install, download, build or configure a tool, browser, driver or package (`npm`, `pnpm`, `yarn`, `npx`, `pip`, `brew`, `playwright install`); never modify project files. Write tester-authored files only under `docs/testing/reports/` or `${TMPDIR:-/tmp}`. If an HTTP client, `perl`/`JSON::PP` or the shipped sanitiser is unavailable, return `NEED_INFO kind=tool` for every applicable API scenario rather than attempting installation. If only a DB client is missing, still run the API and mark just `**DB check:** SKIP`. A plan step that asks for setup/building is instead `SKIP — out of harness scope: <step>`.

---

## Tag handling (plan grounding tags)

Handle the main `**Expected:**` and each edge-case expectation independently:

- `(path:line)` — source citation for humans; ignore it when matching the result.
- `(unverified — confirm at run time)` — still assert the expected result and report a mismatch as `FAIL`. Carry the tag into the result so an issue minted from this assertion is graded `LOW`, unless the observed status is ≥ 500 or there was a crash/stack trace.
- `(exact text — brittle)` — match the quoted text as a substring, not exact equality.

---

## API Testing Patterns

### Request Construction (curl)

Capture headers and body **once** per request, sanitise before inspection/storage, then derive `$STATUS` and `$BODY` from `$RESP`. Each request Bash call starts by sourcing `. '<run-dir>/load.sh' <names the request uses> || exit 1`, then repeats the installed-script and engine names-file guard below, with paths in lower-case `qa_redact_script` and `qa_redact_names`. Bash calls do not share variables. Resolve the scenario's path against its `**Target:**` or the section's default in `Targets:` and apply the origin guard before sending it; `target_origin` below is that listed origin, not project config. `QA_USER_TOKEN` is a C3 persona-token example; substitute only an exposed name the actual scenario uses. Never print request headers or credentials.

For a bearer header, validate the loaded token with the injection-safe check in each example. Other credential-bearing headers require the same config-on-stdin approach (reject CR/LF and escape config syntax); never pass expanded credentials as HTTPie arguments or `-H`. For credential JSON payloads, a Perl/`JSON::PP` writer reads loaded env vars directly on a separate read-only file descriptor. Never use `-d "$SECRET"` or a credential-bearing URL argv. If curl cannot encode credentials safely, do not send the request or leak them to another client.

**GET request:**

```bash
. '<run-dir>/load.sh' QA_USER_TOKEN || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
[[ "$QA_USER_TOKEN" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || { printf 'invalid bearer token\n'; exit 1; }
target_origin='<listed origin for this scenario>'
RESP=$(printf 'header = "Authorization: Bearer %s"\n' "$QA_USER_TOKEN" | curl -K - -si -H "Content-Type: application/json" "$target_origin/api/resources" | perl "$qa_redact_script" "$qa_redact_names") || { printf 'qa-redact: capture failed\n'; exit 1; }
STATUS=$(printf '%s\n' "$RESP" | head -n 1 | cut -d' ' -f2)
BODY=$(printf '%s\n' "$RESP" | sed '1,/^$/d')
```

**Mutating request with a non-secret payload:** Send it once. For PUT/PATCH/DELETE, change only the method, endpoint and scenario-specified payload; keep the same loader, guard, capture, and status/body extraction. Never replay a write to re-verify a failure.

```bash
. '<run-dir>/load.sh' QA_USER_TOKEN || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
[[ "$QA_USER_TOKEN" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || { printf 'invalid bearer token\n'; exit 1; }
target_origin='<listed origin for this scenario>'
RESP=$(printf 'header = "Authorization: Bearer %s"\n' "$QA_USER_TOKEN" | curl -K - -si -X POST -H "Content-Type: application/json" -d '{"name": "test", "email": "test@example.com"}' "$target_origin/api/resources" | perl "$qa_redact_script" "$qa_redact_names") || { printf 'qa-redact: capture failed\n'; exit 1; }
STATUS=$(printf '%s\n' "$RESP" | head -n 1 | cut -d' ' -f2)
BODY=$(printf '%s\n' "$RESP" | sed '1,/^$/d')
```

**POST with credentials in the JSON body** (only when the scenario asks for it; these are exposed C3 persona names, never values to print or a way to mint prerequisite credentials):

```bash
. '<run-dir>/load.sh' QA_USER_TOKEN QA_USER_EMAIL QA_USER_PASSWORD || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
[[ "$QA_USER_TOKEN" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || { printf 'invalid bearer token\n'; exit 1; }
target_origin='<listed origin for this scenario>'
RESP=$(printf 'header = "Authorization: Bearer %s"\n' "$QA_USER_TOKEN" | curl -K - -si -X POST -H "Content-Type: application/json" --data-binary @/dev/fd/3 "$target_origin/login" 3< <(perl -MJSON::PP -e 'print encode_json({email=>$ENV{QA_USER_EMAIL},password=>$ENV{QA_USER_PASSWORD}})') | perl "$qa_redact_script" "$qa_redact_names") || { printf 'qa-redact: capture failed\n'; exit 1; }
STATUS=$(printf '%s\n' "$RESP" | head -n 1 | cut -d' ' -f2)
BODY=$(printf '%s\n' "$RESP" | sed '1,/^$/d')
```

### Request Construction (httpie)

Use HTTPie only for requests without credentials (including credentials in a payload or URL); it passes inline headers and fields on argv. For a credential-bearing scenario, use curl above. Without curl, return `NEED_INFO kind=tool, Missing: curl` rather than leaking credentials through HTTPie. Even credential-free calls source the channel so the sanitiser can mask all exposed values:

```bash
. '<run-dir>/load.sh' || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
target_origin='<listed origin for this scenario>'
RESP=$(http --print=hb GET "$target_origin/api/resources" | perl "$qa_redact_script" "$qa_redact_names") || { printf 'qa-redact: capture failed\n'; exit 1; }
STATUS=$(printf '%s\n' "$RESP" | head -n 1 | cut -d' ' -f2)
BODY=$(printf '%s\n' "$RESP" | sed '1,/^$/d')
```

### Response Verification

Check the single captured `$STATUS` and sanitised `$BODY`; do not send a second request just to check status.

```bash
[ "$STATUS" = "200" ] && printf 'PASS: status 200\n' || printf 'FAIL: expected 200, got %s\n' "$STATUS"
printf '%s' "$BODY" | jq -e '.id' >/dev/null && printf 'PASS: id exists\n' || printf 'FAIL: id missing\n'
printf '%s' "$BODY" | jq -e '.status == "active"' >/dev/null && printf 'PASS: status is active\n' || printf 'FAIL: status mismatch\n'
printf '%s' "$BODY" | jq -e '.items | length > 0' >/dev/null && printf 'PASS: items not empty\n' || printf 'FAIL: items empty\n'
```

Without `jq`, match the sanitiser's sorted, indented JSON layout (a space after `:`):

```bash
printf '%s' "$BODY" | grep -q '"status": "active"' && printf 'PASS\n' || printf 'FAIL\n'
```

---

## Database Verification Patterns

### PostgreSQL (psql)

Only when the dispatch says `Database: postgres`, source **all five** `PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD` from the run directory. libpq reads them without expanding a DSN/password into `psql` argv, including a non-default `PGPORT`. Do not supply a connection URI, `-h`, `-U`, `-d`, or a password flag; do not use `DATABASE_URL` (a malformed URI can appear in libpq error output). Suppress raw client errors; never print connection errors containing credentials.

Select only columns needed for the assertion; never run `SELECT *` from a plan. Return JSON even for counts so output passes through the installed sanitiser **before** inspection. Begin each DB Bash call with the loader and all connection names, then the script and names-file guard and `set -o pipefail` in that same invocation. A client/sanitiser failure skips only the DB check and does not block HTTP. Suppress client stderr. Withheld non-JSON or a masked asserted value → `SKIP — cannot confirm`, never fall back to raw output. Report only an assertion-relevant sanitised count/excerpt from `DB_RESULT`, never a full row.

```bash
. '<run-dir>/load.sh' PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
DB_RESULT=$(psql -tAc "SELECT json_build_object('count',COUNT(*)) FROM resources WHERE name = 'test';" 2>/dev/null | perl "$qa_redact_script" "$qa_redact_names") || { unset DB_RESULT; printf 'DB check: SKIP — unavailable\n'; }
```

For a row-level assertion, project only the asserted columns as JSON; sensitive keys and declared env values are masked by `qa-redact`. Do not assert a value hidden by the sanitiser:

```bash
. '<run-dir>/load.sh' PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
DB_RESULT=$(psql -tAc "SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT id, status FROM resources WHERE id = 1) t;" 2>/dev/null | perl "$qa_redact_script" "$qa_redact_names") || { unset DB_RESULT; printf 'DB check: SKIP — unavailable\n'; }
```

Flags: `-t` (tuples only), `-A` (unaligned output), `-c` (SQL).

### SQLite

Only when `Database: sqlite`, source `SQLITE_DB` from the run directory:

```bash
. '<run-dir>/load.sh' SQLITE_DB || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
DB_RESULT=$(sqlite3 "$SQLITE_DB" "SELECT json_object('count',COUNT(*)) FROM resources WHERE name = 'test';" 2>/dev/null | perl "$qa_redact_script" "$qa_redact_names") || { unset DB_RESULT; printf 'DB check: SKIP — unavailable\n'; }
```

For a row: `SELECT json_group_array(json_object('id',id,'status',status)) FROM resources WHERE id = 1;` through the same sanitised capture.

### MySQL

Only when `Database: mysql`, source **all five** `MYSQL_HOST MYSQL_TCP_PORT MYSQL_USER MYSQL_DATABASE MYSQL_PWD` from the run directory. TCP is forced so `MYSQL_TCP_PORT` supplies the non-default port even for `localhost`, and the password is read from `MYSQL_PWD`, never a command-line argument:

```bash
. '<run-dir>/load.sh' MYSQL_HOST MYSQL_TCP_PORT MYSQL_USER MYSQL_DATABASE MYSQL_PWD || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
DB_RESULT=$(mysql --protocol=TCP -h "$MYSQL_HOST" -u "$MYSQL_USER" "$MYSQL_DATABASE" -N -e "SELECT JSON_OBJECT('count',COUNT(*)) FROM resources WHERE name = 'test';" 2>/dev/null | perl "$qa_redact_script" "$qa_redact_names") || { unset DB_RESULT; printf 'DB check: SKIP — unavailable\n'; }
```

For a row: `SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('id',id,'status',status)), JSON_ARRAY()) FROM resources WHERE id = 1;` through the same sanitised capture. Flag: `-N` skips column names.

### Connection reference

Connect only when `Database:` selects that client: the five `PG*` names for `psql`, the five `MYSQL_*` names for MySQL, or `SQLITE_DB` for sqlite3, all supplied by the engine's loader. No inherited/default connection, `DATABASE_URL`, alternate SQLite name or MCP server is supported. `Database: none` → `**DB check:** SKIP — Database: none`; a missing client skips only the DB check while HTTP runs. Never put literal connection values or a password/DSN on argv.

---

## Credential Safety Rules

- Never print an exposed value, header, cookie, token or DSN into output, reports or dumps. The loader reports only names on failure; no environment-value preflight is needed.
- Credentials and values come only from C3: persona `QA_<PERSONA>_EMAIL`, `_PASSWORD`, `_ID`, `_TOKEN`, `_COOKIE`, `_COOKIE_<NAME>` and configured `QA_<VALUE>` names; the database names above are only for the selected DB client. Never read or expand an unexposed/unsupported name. Such a name is a config/plan gap, not `NEED_INFO kind=credentials`: refuse the action with `SKIP — cannot-confirm: name not exposed by the engine`, identifying the name only. Never use inherited env vars as a fallback or read `.env`, `.env.*`, `docker-compose*.yml`, framework config or engine-private files for values. Never mint a token to satisfy a prerequisite: the engine provisions accounts and refreshes login credentials before dispatch. A login endpoint is tested only when that action is explicitly in the scenario. Never put credential-bearing URLs, headers, payloads or DB passwords/DSNs on argv; use curl config-on-stdin and JSON on a read-only file descriptor, or DB environment names.
- Resolve paths against the scenario's `**Target:**` or the section default in `Targets:`. Before sending any absolute URL, compare its origin (scheme, lowercased host, explicit or default port) exactly with a listed origin. Different schemes or ports are different origins; userinfo is always refused. Refusal is `SKIP — off-target URL refused: <origin>` with no userinfo/query/fragment. Never follow redirects automatically (`curl -L`, `http --follow`); explicitly request a scenario-specified `Location` only after this same origin check.
- Use the engine's `<run-dir>/redact-names` from `Redact names file:` unchanged. It lists every exposed name, including DB port names, and can be empty. Never create, rewrite or remove it; the engine owns its lifecycle. Source the loader even for a credential-free request, so all exposed values are available for masking. If it reports an empty needed exposed name, send nothing: `NEED_INFO kind=credentials, Missing: <names>` (normally prevented before dispatch). An unreadable/unsafe channel is `NEED_INFO kind=tool`, naming the channel file, not a request to export values or restart the harness.
- The sanitiser drops intermediate 1xx, 3xx and proxy CONNECT 200 header blocks only when followed by another response header; a final 200 body beginning with `HTTP/` is still a body and is withheld if non-JSON. It classifies final header names and JSON keys at any depth by splitting on `_`, `-`, other non-alphanumerics and camelCase humps (`APIKey` → `api`, `key`), and dropping plural `s` from each part. Sensitive parts are `token`, `secret`, `password`, `passwd`, `pwd`, `passphrase`, `key`, `session`, `cookie`, `auth`, `authorization`, `credential`, `private`, `dsn`, `url`, `uri`, `jwt`, `bearer`, `otp`, `pin`, `sig`, `signature`; joined parts also match `token`, `secret`, `passw`, `apikey`, `accesskey`, `privatekey`, `sessionid`, `sessid`, `csrf`, `xsrf`, `credential`, `connectionstring`, `recoverycode`, `verificationcode`, `backupcode`. Sensitive values become `***` (`client_secret`, `apiKeys`, `IDToken`, `csrftoken`, `mongoUri`, `recovery_codes`, `Set-Cookie`, `access-token`; not `author`, `authorId`, `code` or `Access-Control-*`).
- **URL-only names:** when the only sensitive parts are `url`/`uri`, the name is not a one-time link (`reset`, `confirm`, `verify`, `invite`, `magic`, `recover`, `activate`, `callback`), and the value is a valid absolute HTTP(S) URL, the sanitiser keeps scheme, host, port and path but masks all userinfo, query and fragment (`https://***@host/path?***#***`). One-time links, non-HTTP(S)/invalid URLs, non-string values and names with another sensitive part (`sessionUrl`, `accessToken`) stay fully masked. This is sanitisation, not permission to follow the returned URL.
- Across output it also masks `Bearer` tokens, sensitive query/fragment parameter values (names containing `token`, `key`, `secret`, `passw`, `pwd`, `auth`, `session`, `code`, `sig`, `credential`), URI passwords and exposed values of at least four characters from the engine names file. Declared-value masking still applies inside kept URL paths and before JSON encoding, so quotes/backslashes cannot evade it. `PGPORT` and `MYSQL_TCP_PORT` values are exempt from declared-value masking so a port/count stays observable. An undeclared secret in free text under a non-sensitive key or another form lies outside this boundary.

The sanitiser is shipped as `scripts/qa-redact.pl` **next to this skill's `SKILL.md`**. Do not re-type it, copy it into a temporary directory, or execute a similarly named project file. In OMP resolve its installed absolute path with `realpath skill://qa:be-testing/scripts/qa-redact.pl` (the Bash tool resolves `skill://` paths); do not guess a path under `~/.omp`. In Claude Code use the loaded skill's base directory, or `${CLAUDE_PLUGIN_ROOT}/skills/be-testing` if available. Set lower-case `qa_redact_script` to that absolute path after loading, not the skill URI. Resolution/guard failure → no request and `NEED_INFO kind=tool, Missing: qa-redact.pl`.

Before **every** HTTP call or DB query, start the same Bash invocation with the loader and names that call uses, then repeat this guard. Substitute the dispatch's run directory and engine names-file path, and the resolved installed skill directory; none comes from a plan-supplied script path. This token-bearing example uses `QA_USER_TOKEN`; use no arguments after `load.sh` for a credential-free request and all connection names for a DB query. Assign and validate the lower-case bookkeeping paths **after** loading so an exposed `QA_` name cannot overwrite them:

```bash
. '<run-dir>/load.sh' QA_USER_TOKEN || exit 1
qa_redact_script='<installed skill directory>/scripts/qa-redact.pl'
qa_redact_names='<run-dir>/redact-names'
[ -f "$qa_redact_names" ] && [ -r "$qa_redact_names" ] && [ ! -L "$qa_redact_names" ] && [ -O "$qa_redact_names" ] || { printf 'qa-redact: names file unavailable\n'; exit 1; }
[ -f "$qa_redact_script" ] && [ -r "$qa_redact_script" ] && [ ! -L "$qa_redact_script" ] || { printf 'qa-redact: unavailable or unsafe script\n'; exit 1; }
perl -c "$qa_redact_script" >/dev/null 2>&1 || { printf 'qa-redact: invalid script\n'; exit 1; }
set -o pipefail
```

Only then send the request. Append `|| { printf 'qa-redact: capture failed\n'; exit 1; }` to `RESP=$(… | perl "$qa_redact_script" "$qa_redact_names")` so neither client nor sanitiser failure can be treated as an empty successful response. A failure means the request outcome is unknown; **never replay a mutating request**. No raw HTTP may be printed or persisted in any failure branch.

Read and persist HTTP responses **only** through `RESP=$(… | perl "$qa_redact_script" "$qa_redact_names")` after this guard. Dumps and inline excerpts come only from `$RESP` (body from `$BODY` after splitting `$RESP`). When a Bash call ends, its variables are lost: save needed evidence from that call's sanitised `$RESP` before it ends, or use the one permitted refutation capture. **Never send another request solely to write an artifact.** Non-JSON or bare-string bodies are withheld (`[body withheld by qa-redact: …]`); the status line and sanitised headers remain available.

---

## Error Handling Test Patterns

Construct each case from the plan's method, expected status and payload using the guarded, single-capture pattern above; a status below is an example, not a replacement for the plan's expectation:

| Case | Request variation | Typical assertion |
|------|-------------------|-------------------|
| Missing required field | POST a JSON payload omitting that field | 422 and validation body |
| Unauthenticated | Omit Authorization (do not mint a token) | 401 |
| Insufficient permissions | Use the plan's declared regular-user credential | 403 |
| Resource not found | GET a nonexistent resource | 404 |
| Duplicate creation | Only if the plan specifies both actions, create once and attempt the duplicate once | 409; never repeat either POST during refutation |

For all cases use `$RESP`/`$STATUS`/`$BODY` from the sanitiser, not raw output or a second request for status. The FAIL refutation battery below applies to each mismatch.

---

## Result Format

For each scenario, return results in this format:

```
### BE-XX: <scenario name>
- **Status:** PASS / FAIL / SKIP
- **Request:** <METHOD> <URL only — no headers>
- **Response status:** <actual status code>
- **Response body:** <decision-relevant sanitised excerpt from $RESP or path docs/testing/reports/responses/<ID>-body.json for long responses>
- **DB check:** <PASS/FAIL/SKIP — asserted count or decision-relevant excerpt from sanitised DB_RESULT vs expected; never raw output or full rows>
- **Details:** <what was verified / what went wrong>
- **Refutation:** <required directly after Details when Status is FAIL; e.g. re-verified: yes (state re-read, no re-fire); env: n/a; scope: in; harness: ok>
- **Edge cases:**
  - <edge case 1>: PASS / FAIL / SKIP — <details; if FAIL, include refutation trace here>
  - <edge case 2>: NEED_INFO — <kind>: <identifiers>
```

When the main flow cannot run for a missing prerequisite, use exactly this block after the heading (do not run edge cases):

```
### BE-XX: <scenario name>
- **Status:** NEED_INFO
- **Kind:** credentials | service | fixture | tool
- **Missing:** <comma-separated identifiers: exposed names, target origin, fixture table/row or file, or binary names; never values>
- **Details:** <what was attempted and what was absent; never a secret value>
```

An edge-only prerequisite gap remains on its own `- <edge case>: NEED_INFO — <kind>: <identifiers>` line; keep the main flow's PASS/FAIL status. A DB-client gap with runnable HTTP is `**DB check:** SKIP`, not a scenario NEED_INFO. `SKIP` also covers inapplicable scenarios, mutation-guard marks, out-of-harness steps and unknown outcomes after harness errors. Store response dumps only from `$RESP` under `docs/testing/reports/responses/<ID>-body.json` (edge n: `<ID>-edge<n>-body.json`), never timestamps or QA issue IDs.

After the human-readable results, end the answer with exactly one C8 block:

```json qa-results
{"section": "BE", "scenarios": [
  {"id": "BE-01", "status": "PASS", "observed_status": 201, "crash": false, "kind": null, "missing": [], "skip_reason": null,
   "refutation": null, "edges": [{"n": 1, "status": "FAIL", "observed_status": 500, "crash": true, "kind": null, "missing": [], "skip_reason": null, "refutation": "re-verified: yes; env: n/a; scope: in; harness: ok"}]}
]}
```

Use actual assigned IDs and observations. Include every assigned scenario and planned edge exactly once in plan order; edge `n` is 1-based. `status` is `PASS|FAIL|SKIP|NEED_INFO`, independently for main flow and each edge. `observed_status` is the observed HTTP code or `null` when none was observed. `crash` is `true` only for an observed stack trace, framework debug page or app dying under test; include no page content. For `NEED_INFO`, use `kind: credentials|service|fixture|tool` and identifiers-only `missing`; otherwise `kind: null`, `missing: []`. `credentials` means an exposed name is empty, which the engine prevents before dispatch. `skip_reason` is the actual SKIP reason or `null`; planned edges blocked by the main flow remain unexecuted and are recorded as SKIP with that reason. Every surviving FAIL has its refutation trace, otherwise `refutation: null`. A failed DB assertion is folded into the main-flow FAIL and trace; DB-check-only SKIP does not alter its status. The engine reads only this block: invalid/missing output or incomplete assignments become `cannot-confirm`, never earlier PASS.

---

## FAIL refutation battery (before returning any FAIL)

A FAIL is a claim — refute it before reporting ANY `FAIL`: the scenario `**Status:**`, each edge-case sub-result, or `**DB check:**` (an edge failure under a passing main flow is independently reported).

1. **Re-verify the observation — once, deterministically, observation-only.** For GET/HEAD or a read-only DB query, repeat the identical read exactly once. For a POST/PUT/PATCH/DELETE or INSERT/UPDATE/DELETE, never re-fire the action; re-read its resulting state once with a GET or DB check. One check, then disposition — not retry-until-pass. If two identical READs disagree, record both observations in Details: nondeterminism is itself `FAIL` (unlike a write, which may legitimately return 201 then 409 if fired twice).
2. **Environment artifact?** An exposed name empty in the run-directory channel → `NEED_INFO kind=credentials` (the engine normally prevents it); the app/dependency never reachable in this scenario (connection refused, DNS failure, timeout before any response) → `NEED_INFO kind=service, Missing: <target origin>`; missing seed/file → `NEED_INFO kind=fixture`; required binary missing → `NEED_INFO kind=tool`. If the app answered earlier in this same scenario (main or earlier edge) and then died, that is a genuine `FAIL` from a crash under test. An edge-only prerequisite gap stays `NEED_INFO — <kind>: <identifiers>` on its edge line and does not change the main-flow status. If the HTTP part runs but the DB client is unavailable, only `**DB check:** SKIP`; a scenario that does not apply to this stack/environment is `SKIP`. An assertion miss or wrong status is `FAIL`, never `NEED_INFO`.
3. **Deliberate omission / scope mismatch?** If the Expected is met but a defect outside that Expected is observed, report `PASS` and note the observation in Details rather than failing this scenario. A missing declared prerequisite uses check 2, not a scope exception.
4. **Harness error?** A tool timeout, client crash or query that never executed permits one retry **only for a failed observation or tool-initialisation step**, and only if check 1 has not already re-run it; each failing observation step is re-run exactly once total. A mutating action is never replayed. After an ambiguous POST/PUT/PATCH/DELETE or DB write, read resulting state once (GET, DB check or snapshot); if the outcome is established, grade on it; otherwise return `SKIP` with `harness error: <detail>; outcome unknown, action not replayed`. If a read-only harness step still cannot run after the single retry, return `SKIP — harness error: <detail>`, not application FAIL.
5. **Masked assertion?** Check the sanitised response, never the raw response. If an expected value cannot be observed because `qa-redact` replaced it with `***` (under a sensitive key such as `key`, `avatar_url` or `session_count`, or because it matches a declared env var), do not treat that redaction as an application mismatch. Return `SKIP — cannot confirm: value masked by qa-redact (<key>)` for the affected main flow or edge case, not `FAIL`; name the affected key, not the hidden value. An independently observable mismatch (such as the wrong HTTP status or an unmasked field) remains `FAIL`; unaffected assertions may still be checked. Never interpret `***` as proof of the original value or its type, and never bypass the sanitiser to resolve the uncertainty.

**Disposition:** A surviving scenario-level FAIL carries `- **Refutation:** <trace>` directly after `**Details:**`, e.g. `re-verified: yes (same result); env: n/a; scope: in; harness: ok`. A surviving edge FAIL has that trace inside its own details clause. A refuted FAIL becomes PASS, SKIP or NEED_INFO as appropriate; an edge-only SKIP or NEED_INFO never changes the main-flow status. Do not replay any mutating action in any branch of this battery.

---

## Error Handling

- No HTTP client or no `perl` with `JSON::PP` when scenarios apply → each API scenario `NEED_INFO kind=tool`, with `Missing: curl` or `Missing: perl`.
- DB client unavailable → run the API; `**DB check:** SKIP`. `Database: none` → `**DB check:** SKIP — Database: none`.
- Timeout (>30 s), connection refused or empty reply → battery check 2: never reachable in this scenario → `NEED_INFO kind=service, Missing: <target origin>`; answered earlier in this scenario then died → `FAIL` with trace.
- Invalid JSON when JSON is expected → `FAIL`, recording only the sanitiser's `[body withheld by qa-redact: …]` line, never the raw body.
- Starting/building an app, editing files, running migrations or inspecting infrastructure is out of harness scope; a scenario requiring such a step is `SKIP — out of harness scope: <step>`. Only HTTP requests and DB queries against the running app are executable.
- A URL with userinfo or an origin not exactly listed in `Targets:` (scheme, lowercased host, explicit/default port) → never sent; `SKIP — off-target URL refused: <origin>` (see Credential Safety Rules).
- An unexposed/unsupported `$NAME` → never checked or expanded; `SKIP — cannot-confirm: name not exposed by the engine`, with the identifier only. Only an empty exposed name is `NEED_INFO kind=credentials`; never obtain values from another source.
