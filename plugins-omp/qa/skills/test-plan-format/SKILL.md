---
name: "qa:test-plan-format"
description: Test plan structure, naming conventions, edge case generation rules, and file saving conventions for QA test plans.
---

# Test Plan Format

## File Conventions

- **Location:** `docs/testing/plans/`
- **Naming:** `YYYY-MM-DD-<topic>-test-plan.md` where `<topic>` is a slugified summary (lowercase, hyphens, no spaces)
- **Create directory if needed:** `mkdir -p docs/testing/plans`

---

## Plan Structure

Every test plan MUST follow this structure; omit optional sections and `**Blocked-by:**` lines where they do not apply. Targets, personas, exposed values, database connections and service bring-up come from the project config, not the plan. Replace the illustrative target names, paths, expected results and citation tags with grounded project-specific details:

~~~markdown
# Test Plan: <title>

## Setup

<Optional human notes, such as a prerequisite to reverse BLK-01 before the run. No consumer parses this section; omit it when no notes are needed.>

## Source
- Type: <PR #N / branch <name> / last N commits / staged changes>
- Base: <main/master>
- Date: <YYYY-MM-DD>
- Branch: <current checkout branch>
- Head: <current checkout HEAD sha>

## Changes Summary

<Brief description of what changed and what needs testing. List affected areas.>

## Blockers / Findings

Defects in the code under test that obstruct how it must be tested. Mandatory — write `None found.` when there are none.

### BLK-01: <one-line defect> — `(file:line)`
- **Impact on testing:** <affected scenarios and the spurious result the defect forces>
- **Remediation (human Setup prerequisite):** <human action before the run>
- **Blocks:** <scenario IDs carrying `**Blocked-by:** BLK-01`>

## Detected Tools
- Playwright MCP: <available/unavailable>
- HTTP client: <curl/httpie/unavailable>
- Database access: <psql/sqlite3/mysql/unavailable>

## FE Test Scenarios

### FE-01: <scenario name>
**Blocked-by:** BLK-01
- **Area:** <component/page>
- **Target:** web
- **Preconditions:** <create any required data through browser (UI) actions as the scenario's persona; record the created identifiers>
- **Steps:**
  1. Open `/login` in the browser
  2. Fill Email with `$QA_USER_EMAIL` and Password with `$QA_USER_PASSWORD`
  3. Click "Sign In"
- **Expected:** <welcome text shown> (path:line)
- **Edge cases:**
  - Password `nope`: <validation message shown> (path:line)
  - Missing email: <validation message shown> (path:line)

## BE Test Scenarios

### BE-01: <scenario name>
**Blocked-by:** BLK-01
- **Area:** <endpoint/service>
- **Target:** api
- **Preconditions:** <HTTP requests that create the required data as the scenario's persona; record the created identifiers>
- **Method:** <HTTP method> <path>
- **Headers:** Authorization: Bearer $QA_USER_TOKEN
- **Payload:** `<JSON body>`
- **Expected:** <status code>, <response body description> (path:line)
- **DB Check:** `SELECT COUNT(*) FROM resources WHERE id = <created-resource-id>` — <expected count>
- **Edge cases:**
  - <edge case with expected status and response> (path:line)
  - <second edge case with expected status and response> (path:line)

## Out of harness scope
- <check the browser/HTTP/DB harness cannot observe> — <one-clause harness reason>
~~~

---

## Scenario Naming

- FE scenarios: `FE-01`, `FE-02`, ... `FE-NN` (zero-padded two digits)
- BE scenarios: `BE-01`, `BE-02`, ... `BE-NN` (zero-padded two digits)
- Numbering is sequential within each section, starting from 01

---

## Grounding tags & assertion style

Every `**Expected:**` assertion and each edge-case expectation needs its own evidence tag:

- `(path:line)` cites a producing source line the author actually read in this working tree. Put one citation on the most load-bearing line per assertion; cite each edge case separately. Never invent a citation or cite a test for behavior it does not assert.
- `(unverified — confirm at run time)` marks an assertion whose producer cannot be read (for example, a foreign PR or pasted diff). If the source is on disk, read it instead; an unverified tag on a readable assertion is a defect. A mismatch still fails at run time.
- `(exact text — brittle)` marks quoted human-readable text that must be matched as a substring, not for equality. Use only when status and body shape cannot disambiguate the behavior; include a source citation or unverified tag as well.

Prefer stable status codes and response structure (keys/types) over exact message text. For a function-derived value (hash, slug, formatted filename), assert its generating rule and cite the producer rather than guessing an exact result; an exact literal needs a fixture or test that pins it. Replace the template's `(path:line)` with an actual cited path and line in each plan.
The BE tester sees only the output of `qa-redact`: it replaces values under sensitive name segments (`key`, `session`, etc.) and exposed values from the run's secrets channel with `***`. A key whose only sensitive segments are `url`/`uri` may retain the scheme, host, port and path of an absolute HTTP(S) URL; plans may assert those visible components, but not masked userinfo, query or fragment. A response URL on an origin outside `[env.targets]` is asserted by separate components (for example, scheme `http`, host `localhost:9000`, path prefix `/avatars/`), never written as a literal `scheme://host…` string anywhere in the scenario. One-time links (`reset`, `confirm`, `verify`, `invite`, `magic`, `recover`, `activate`, `callback`), keys with another sensitive segment (such as `sessionUrl`), and URL-named values that do not parse as absolute HTTP(S) URLs stay fully masked. Exposed-value masking still applies, including inside a retained URL path: never assert a component that became `***`. For fully masked fields, assert an observable property such as key presence or the type of an unaffected surrounding field/object, not the hidden value or its original type. For example, assert that `flags[0]` has a `key` field, not that `flags[0].key` equals `feature_x`; an assertion on masked data cannot establish PASS or FAIL and is reported as `SKIP — cannot confirm: value masked by qa-redact (<key>)` at run time. Keep other independently observable assertions, such as HTTP status codes, testable.

## Config references and preconditions

- `## Source` records the diff source, base, date, current checkout `Branch:` and `Head:`. These identify the branch and snapshot for plan selection and staleness checks.
- `## Setup` is optional human notes only, directly after the title when present. No consumer parses it. Do not declare URLs, credentials, database connections or service commands there; their configuration belongs in `.av/config.toml` and its local overrides.
- Persona names match `[a-z][a-z0-9_]*`. For persona `p`, use its upper-cased name in `QA_<P>_EMAIL`, `QA_<P>_PASSWORD`, `QA_<P>_ID`, `QA_<P>_TOKEN`, `QA_<P>_COOKIE` or `QA_<P>_COOKIE_<NAME>`. `EMAIL` and `PASSWORD` always exist; `ID`, `TOKEN` and cookie fields require the corresponding create/static-id or login capability. Cookie suffixes upper-case the cookie name, replace each run outside `A-Z0-9` with `_` and trim leading/trailing `_` (`__Host-session` → `HOST_SESSION`, `connect.sid` → `CONNECT_SID`). An `[env.values]` entry `X` is exposed as `QA_<X>`, with `X` upper-cased. `[env.secrets]` is never tester-visible.
- Credentials and exposed values are written as `$QA_NAME` or `${QA_NAME}` anywhere in a scenario: steps, headers, payloads, preconditions and edges. The name inside either form matches `QA_[A-Z0-9_]+`. No separate declaration is needed: `plan check` derives requirements from these tokens. A configured persona plus a field from `EMAIL`, `PASSWORD`, `ID`, `TOKEN`, `COOKIE`, `COOKIE_<NAME>` is a persona field; otherwise a configured value name is a value. An unknown token ending in a persona field is reported under `missing.personas`; other unknown tokens go under `missing.values`. Unavailable persona capabilities are gaps too. These are config gaps checked before dispatch, not runtime `NEED_INFO`.
- A credential in a header, cookie or login step (bearer token, API key, email/password) must use those names, never the project's own variable name (`API_KEY`), a literal or a placeholder such as `TOKEN`. A deliberately invalid input in a negative edge case is not a credential. Never copy secret values into the plan.
- Every absolute URL anywhere in a scenario, including `**Expected:**` and edge cases, must match an `[env.targets]` origin exactly (scheme, lower-cased host, explicit or default port). `plan check` reports any other origin as `off_target`, and `/qa:run` stops. Request and page URLs may instead be relative paths, which use `qa.defaults.be_target` / `qa.defaults.fe_target` for their section unless the scenario has `- **Target:** <name>`; write that line whenever the default does not apply. Userinfo is always refused, and redirects are never followed automatically.
- When config is absent or lacks a needed persona, value or target, retain the repository-grounded name. Write a missing target as `- **Target:** <name>` with relative paths, never an absolute URL on an unknown origin. `plan check` reports the missing names so `/qa:run` can fill the config gaps; do not replace them with literal credentials or omit the scenario.
- Data a scenario needs (a CV, an order, an uploaded file record) is created by that scenario's own preconditions as its persona, never assumed to exist. FE preconditions create data through browser (UI) actions only; API/HTTP preconditions belong to BE scenarios. Set up another persona's resource through that persona for ownership checks. A repository file may be an upload fixture. `NEED_INFO kind=fixture` is reserved for data the app cannot create itself. Precondition writes count toward the mutation policy, including when the main request expects rejection.
- `**DB Check:**` names no connection; it requires `[env.database]`, and `plan check` reports `missing.database` otherwise. The engine exposes `PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD` for Postgres, `MYSQL_HOST MYSQL_TCP_PORT MYSQL_USER MYSQL_DATABASE MYSQL_PWD` for MySQL or `SQLITE_DB` for SQLite through the private run channel. These are DB-client inputs, not plan declarations. Never put passwords or DSNs in process argv.
- Write DB checks against only the columns asserted; never put `SELECT *` in a plan. Row checks project just the needed columns to JSON and pass through the installed `qa-redact.pl` before inspection or reporting.

## Harness scope

Scenario steps, including data preconditions, must stay within their section's harness against the already-running app: FE flows and preconditions use browser (UI) actions only; BE flows use HTTP requests / DB queries, with API/HTTP requests for data preconditions. Do not include `docker`, `make`, `npm`, migrations, file edits or infrastructure inspection as scenario steps. Bring-up belongs to the config's `env.services`, which `/qa:run` handles before dispatch; a reversible blocker's human prerequisite belongs in optional `## Setup` notes. List unobservable checks under optional `## Out of harness scope` as bullets with a one-clause harness reason (never scenario headings). A code defect that prevents a contract-correct result is a Blocker, not an out-of-scope check. If a tester nevertheless encounters an out-of-scope step, the scenario is `SKIP` with reason `out of harness scope: <step>`.

---

## Edge Case Generation Rules

For EVERY scenario, consider and include relevant edge cases from:

### Input boundaries
- Empty/null/missing values
- Maximum length strings
- Special characters (unicode, HTML entities, SQL metacharacters)
- Negative numbers, zero, boundary values (MAX_INT)

### Authentication & Authorization
- Unauthenticated request (no token)
- Expired token
- Valid token but insufficient permissions
- Another user's resource (IDOR)

### State
- Resource does not exist (404)
- Duplicate creation attempt (409)
- Concurrent modifications (race conditions)
- Resource in unexpected state (e.g., already deleted, already processed)

### Data integrity
- Required fields missing (422)
- Invalid data types (string where number expected)
- Referential integrity (foreign key does not exist)

### FE-specific
- Slow/no network connection
- Empty state (no data to display)
- Very long content (overflow, truncation)
- User not logged in
- Browser back/forward during operation

---

## Section Omission Rules

- If changes are **FE-only**: omit the `## BE Test Scenarios` section entirely
- If changes are **BE-only**: omit the `## FE Test Scenarios` section entirely
- If a tool is **unavailable**: note it in `## Detected Tools`; the tester reports `NEED_INFO kind=tool` at run time rather than labeling scenarios as skipped
- `## Setup` and `## Out of harness scope` are optional; `## Blockers / Findings` is mandatory for generated plans (`None found.` if none). Consumers ignore its absence in a hand-written plan

---

## Plan Quality Checklist

Before saving the plan, verify:

- [ ] Every scenario has at least 2 edge cases
- [ ] Every BE scenario has an expected status code
- [ ] Every FE scenario has concrete steps (not "test the form")
- [ ] DB Checks use actual table/column names from the codebase, name no connection and require `[env.database]`
- [ ] API paths match actual routes from the codebase
- [ ] No placeholder text (TBD, TODO, fill in later)
- [ ] `## Source` records the current checkout `Branch:` and `Head:`
- [ ] `## Setup`, if present, holds only optional human notes; targets, values, database connections and bring-up are config-owned
- [ ] Every credential and exposed value uses a `$QA_NAME` or `${QA_NAME}` token, never a literal secret, a non-`QA_` name or a placeholder such as `TOKEN`
- [ ] Every absolute URL anywhere in a scenario, including Expected and edge cases, is on a config target origin; off-target response URLs are asserted by separate components, not literal URLs. Request/page paths have `- **Target:** <name>` where the section default does not apply
- [ ] Missing config names remain explicit; a missing target is named with relative paths, not an absolute URL on an unknown origin
- [ ] Each scenario creates its required data as its persona through browser (UI) actions for FE or API/HTTP requests for BE; only data the app cannot create requires an external fixture
- [ ] Every `**Expected:**` and edge-case expectation has a grounded `(path:line)` or `(unverified — confirm at run time)` tag
- [ ] BE expectations and edge cases assert only visible data after `qa-redact`: scheme/host/port/path may be observable for URL-only keys, but masked userinfo/query/fragment, one-time links and any masked path component are not
- [ ] `## Blockers / Findings` is present (or reads `None found.`)
- [ ] No scenario step is outside the browser / HTTP / DB harness scope
- [ ] For ≥2 independent boolean inputs, a `state-combination-planning` 2^N table appears above affected scenarios with a disposition for every row
