# Marketplace Configuration

This is the shared configuration reference for users and plugin authors. QA is the first consumer; any marketplace plugin that becomes configurable uses the same files and shared environment schema.

## Files and overrides

All paths below are relative to the project's repository root, not the installed marketplace plugin.

| File | Commit it? | Purpose |
|---|---|---|
| `.av/config.toml` | Yes | Team-wide environment and plugin settings. |
| `.av/local.toml` | No; add it to `.gitignore` | Personal overrides, using the same schema. |
| `.av/secrets.local.env` | No; add it to `.gitignore` | Private dotenv values referenced through `file:` sources. It is not loaded automatically. |

The engine reads `.av/config.toml`, then merges `.av/local.toml` over it recursively, key by key. A local leaf replaces the shared leaf; other keys survive. Arrays are replaced, not concatenated. Source permissions follow the file that supplies each effective key. The shared file must exist and contain the consuming plugin's table: a local-only file or table does not count as configuring the plugin.

Every configurable plugin reports:

| Field | Meaning |
|---|---|
| `state = "missing-file"` | `.av/config.toml` does not exist. |
| `state = "missing-table"` | The shared file exists and validates, but lacks this plugin's top-level table. |
| `state = "invalid"` | A file cannot be parsed, or the effective shared environment/plugin settings fail validation. Inspect `errors`. |
| `state = "ok"` | The shared file contains the plugin's table and the effective settings validate. |
| `provenance` | A map from effective dotted keys to `.av/config.toml` or `.av/local.toml`, identifying which file supplies them. |

For an existing shared file, validation errors take precedence over `missing-table`. Errors and warnings identify the file and key, not a resolved value. The bootstrap writes only `.av/config.toml` and `.gitignore`, **never `.av/local.toml`**. If a proposed addition or repair is supplied by the local file, it stops and names that key; the user must resolve the personal override before a shared change can take effect.

## Value sources

`[env.secrets]`, `[env.values]` and `env.database.password` contain source strings, not unprefixed values. A plugin may use the same source format in its own table.

| Source | Resolution |
|---|---|
| `cmd:<shell>` | Runs `/bin/sh -c` in the repository root with a 30-second timeout. Uses UTF-8 stdout with trailing newlines stripped; a non-zero exit, timeout, invalid output or empty result is an error. |
| `env:<NAME>` | Reads the engine's inherited environment: what the harness had **at startup**. A variable exported later in another shell is not visible. |
| `file:<path>#<KEY>` | Reads a named key from a dotenv file. Relative paths are rooted at the repository; an absolute path is permitted in a personal override. |
| `literal:<text>` | Uses the supplied text, subject to the restrictions below. |

Environment and dotenv key names must match `[A-Za-z_][A-Za-z0-9_]*`. Every source needs a non-empty payload. A missing or empty environment/dotenv value is an error, not a fallback to another source. Dotenv supports `KEY=value`, optional `export`, single- or double-quoted values and comments; it is read as data, not sourced as a shell script.

`config` validates source syntax and permissions **without executing or resolving sources**. The engine resolves only the values an operation needs, after trust has been accepted. Engines never print a resolved source value: errors identify keys and source kinds, while configuration and trust displays mask `literal:` payloads. Do not put credentials directly in a `cmd:` command; its text is displayed for approval.

### Restrictions on committed sources

`.av/config.toml` is branch content: whoever controls a branch can change its commands and source references. It must not silently gain access to arbitrary credentials in the user's environment or tracked files.

| Source or field | `.av/config.toml` | `.av/local.toml` |
|---|---|---|
| `env:` in `[env]` | Name must start with `AV_`. | Any valid environment name. |
| `env:` in a plugin table | `AV_` or the consuming plugin's registered prefix; QA permits `QA_`. | Any valid environment name. |
| `file:` | Repository-relative, cannot escape the repository, and must be git-ignored according to Git's effective ignore rules. Absolute paths are rejected. | Absolute paths are allowed. Relative paths must still stay inside the repository and be git-ignored. |
| `literal:` in `[env.secrets]` | Rejected. | Allowed. |
| `literal:` for a static account password | Rejected by QA. | Allowed. |
| `literal:` for `env.database.password` | Allowed only when `env.database.host` is loopback. | Allowed. |
| `literal:` for other exposed values | Allowed. | Allowed. |

Loopback means exactly `localhost`, `127.0.0.1`, `::1` or a host ending in `.localhost`, after lowercasing and stripping IPv6 brackets. It does not mean every address in `127.0.0.0/8`, a Compose service name or a bind address such as `0.0.0.0`.

Source validation errors include `expected a value source`, `empty value source`, `invalid environment source name`, `environment source name has a forbidden prefix`, `expected file source with a key`, `absolute file source is forbidden in shared config`, `file source escapes the repository`, `repository file source must be git-ignored` and `secret literal is forbidden in shared config`. Resolution can additionally fail because the file or command is unavailable, dotenv quoting is invalid, a command fails or times out, command output is not UTF-8, or the result is empty. Such failures never include the value or command output.

## Shared `[env]` schema

The root key `version` is required and must be the integer `1` (not a string or boolean). The shared loader uses Python 3.11 or newer and stdlib TOML parsing. Invalid text encoding or TOML is reported against the file's `document` key; an absent, mistyped or unsupported version reports `expected schema version 1`.

`[env]` is optional and defaults to an empty table. Its known sub-tables are `targets`, `services`, `secrets`, `values` and `database`. They must be tables when present (`expected a table` otherwise). Unknown `[env]` sub-tables produce an `unknown environment sub-table` **warning**, not an error, so a later plugin can add shared capabilities without breaking an installed consumer. Other plugins' top-level tables are ignored.

The tables below list all shared keys. "Required" means required only when the enclosing optional table or database kind is configured; omission is not an implicit credential or connection setting.

### Targets

| Key | Type | Default | Validation and meaning |
|---|---|---|---|
| `env.targets` | Table of named origins | `{}` | Each name must match `[A-Za-z_][A-Za-z0-9_]*`. |
| `env.targets.<name>` | String | No target | An `http://host[:port]` or `https://host[:port]` origin, with no userinfo, path (including a trailing `/`), query or fragment. |

Invalid names, non-string values or malformed origins report `expected an HTTP origin without userinfo, path, query or fragment`. Origins must have a host; whitespace, control characters, backslashes, percent escapes in hosts and empty, zero or out-of-range ports are rejected. Omitted HTTP/HTTPS ports mean `80`/`443` for origin comparison. Non-loopback origins require trust before use.

### Services

| Key | Type | Default | Validation and meaning |
|---|---|---|---|
| `env.services` | Table | No lifecycle configuration | Only `health`, `up`, `prepare` and `down` are accepted. An extra key reports `unknown key`. |
| `env.services.health` | Array of strings | `[]` | Each probe is `<target>:<path>`. The target must exist in `[env.targets]`; the path must start with `/`, not `//`. A responding HTTP status below `500` counts as up; a connection failure does not. |
| `env.services.up` | Non-empty string | Omitted; no command | Shell command to bring services up. A wrong type or empty string reports `expected a non-empty command`. |
| `env.services.prepare` | Array of non-empty strings | `[]` | Preparation commands in order, such as migrations. A wrong type or empty/non-string item reports `expected a command list`. |
| `env.services.down` | Non-empty string | Omitted; no command | Shell command to tear services down. A wrong type or empty string reports `expected a non-empty command`. |

A non-array `health` reports `expected a probe list`; a non-string probe or one without `:` reports `expected target:path probes`; an undefined target or invalid path reports `probe has undefined target or invalid path`. These commands are shell commands, not `cmd:` value sources: they run in the repository root through `/bin/sh -c`, under the consuming plugin's lifecycle gates. QA runs `down` only if that run executed `up`; see its [configuration guide](plugins/qa.md#configuration) for when it asks to start services.

### Secrets and values

| Key | Type | Default | Validation and meaning |
|---|---|---|---|
| `env.secrets` | Table of named source strings | `{}` | Engine-only values, never exposed to agents. |
| `env.secrets.<name>` | Value-source string | No value | Name must match `[A-Za-z_][A-Za-z0-9_]*`; committed literals are forbidden. |
| `env.values` | Table of named source strings | `{}` | Values the consuming plugin may expose to its agents. QA exposes referenced names as `QA_<NAME>`. |
| `env.values.<name>` | Value-source string | No value | Name must match `[A-Za-z_][A-Za-z0-9_]*`; source restrictions still apply. |

An invalid name reports `invalid value name`. Each entry also undergoes the [value-source checks](#value-sources). Consumers validate their exposed-name collisions: QA rejects values that collide after uppercasing, or with a configured persona's exposed fields, with `value name collides with an exposed name`. Use `secrets`, not `values`, for administrative credentials.

### Database

`[env.database]` is optional; when omitted, no database is configured. An empty table is not a usable database: `kind` is required. Only the keys below are accepted; any other key reports `unknown key`.

| Key | Type | Default | Validation and meaning |
|---|---|---|---|
| `env.database.kind` | String | Required | `postgres`, `mysql` or `sqlite`; otherwise `expected postgres, mysql or sqlite`. |
| `env.database.host` | Non-empty string | Required for Postgres/MySQL | Connection host, not a value source. Non-loopback hosts are trust-pinned. Forbidden for SQLite. |
| `env.database.port` | Integer | Omitted; QA uses `5432` for Postgres, `3306` for MySQL | If present, `1`–`65535` inclusive; booleans are not integers here. Forbidden for SQLite. Specify a published non-default host port explicitly. |
| `env.database.user` | Non-empty string | Required for Postgres/MySQL | Connection username, not a value source. Forbidden for SQLite. |
| `env.database.name` | Non-empty string | Required for Postgres/MySQL | Database name, not a value source. Forbidden for SQLite. |
| `env.database.password` | Non-empty value-source string | Required for Postgres/MySQL | All source restrictions apply; a committed literal requires a loopback `host`. Forbidden for SQLite. |
| `env.database.path` | Non-empty string | Required for SQLite | Database file path, not a value source. QA resolves a relative path against the repository root. Forbidden for Postgres/MySQL. |

Missing, empty or mistyped required strings report `expected a non-empty string`. An invalid port reports `expected a port between 1 and 65535`. SQLite rejects each of `host`, `port`, `user`, `name` and `password` with `not supported for sqlite`; Postgres/MySQL reject `path` with `only supported for sqlite`. Passwords also undergo value-source validation. The shared loader does not test database connectivity during `config`.

### Example shared environment

This demonstrates the schema, not environment autodetection. Replace the origins, names and probes with those established by your repository, add the plugin's table from its guide, and supply the private dotenv keys before running work that needs them.

```toml
version = 1

[env.targets]
api = "http://localhost:8000"
web = "http://localhost:5174"

[env.services]
health = ["api:/health", "web:/"]

[env.secrets]
SERVICE_KEY = "file:.av/secrets.local.env#SERVICE_KEY"

[env.values]
PUBLIC_KEY = "env:AV_PUBLIC_KEY"

[env.database]
kind = "postgres"
host = "127.0.0.1"
port = 54322
user = "postgres"
name = "app"
password = "file:.av/secrets.local.env#DB_PASSWORD"
```

Add these entries to the project's `.gitignore`:

```gitignore
.av/local.toml
.av/secrets.local.env
```

## Trust

Each plugin pins the effective settings it reads that can execute commands, read the user's environment/files or widen its own gates. The shared subset includes:

- Every source string in `[env]`, including literals.
- The configured `env.services` table, including probes and lifecycle commands.
- Every non-loopback target origin.
- A non-loopback `env.database.host`.

The plugin adds its own sources, recipes and gate-setting keys. QA's pinned keys are documented in its [configuration guide](plugins/qa.md#configuration). Changing another plugin's table does not invalidate QA's trust; changing shared sensitive settings does affect each consumer's pin.

The hash is SHA-256 over the **full, unmasked canonical JSON** of that subset. The displayed `trust_subset` masks every `literal:` payload as `literal:***`; recipe headers and bodies keep source placeholders instead of resolved values. Masking is only for display: changing a literal changes the hash even though both previews show `literal:***`. The pin covers source definitions, not the output of a command or the current contents of an environment variable/dotenv key.

Pins live outside branch content at `~/.local/state/av-marketplace/trust.json`, or `${XDG_STATE_HOME}/av-marketplace/trust.json` when that variable is set. They are keyed by the repository root's **realpath and plugin name**; each worktree trusts separately.

| Reported `trust` | Meaning |
|---|---|
| `not-required` | The sensitive subset is empty. |
| `new` | This repository/plugin has no recorded pin for a non-empty subset. |
| `trusted` | The current subset matches the recorded hash. |
| `changed` | A pin exists, but the effective subset has changed. |

For `new` or `changed`, display the complete subset and ask once before running anything it authorizes. On approval, `trust accept <hash>` records the displayed `trust_hash` only if the current subset still hashes to it; otherwise it stops and requires a fresh preview. Declining leaves trust unchanged. Accepting a pin does not bypass the plugin's other approval or mutation gates.

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
| `[env]` | Shared environment; [schema above](#shared-env-schema). |
| `[qa]` | QA; [configuration keys, defaults, recipes and pinned gates](plugins/qa.md#configuration). |

Only QA is configurable in this release. Register future plugin tables here with a link to their full key reference; do not put plugin-specific policies or recipes in `[env]`.

## Making a plugin configurable

1. **Own exactly one top-level table named after the plugin**, such as `[qa]`. Use the root `version` and shared `[env]` schema; read settings only from `.av/config.toml` and `.av/local.toml`, with private values obtained through sources. Do not introduce a plugin-specific config directory or parallel file convention.
2. **Validate only your table plus the shared schema.** Ignore other plugins' top-level tables; preserve the warning-only treatment of unknown `[env]` sub-tables. Report `state`, `provenance`, file/key diagnostics and masked trust metadata through your engine.
3. **Pin capabilities before use.** Combine the shared sensitive subset with your sources, executable recipes and keys that widen gates. Scope acceptance by repository and plugin; never resolve a source or run an untrusted command while inspecting config.
4. **Use the bootstrap protocol below** from the plugin's entry command in `create`, `extend` and `repair` situations. Reuse the generic loader and environment detector, adding only your table's validation, recipes and policy questions.
5. **Register and document the change together.** Add your table and key-reference link to this page's registry, and document every plugin-owned key, type, default, allowed value, validation error and trust-pinned gate in the plugin guide. Any new shared `[env]` keys must be documented here as well. Update this page in the same change that makes the plugin configurable.

## Bootstrap protocol

The bootstrap belongs in a configurable plugin's entry command, not in a separate init command. Its modes are:

| Mode | Trigger | Input |
|---|---|---|
| `create` | The shared file or the plugin's table is missing. | Required names from the work document, if one exists, and plugin creation-time policy choices. |
| `extend` | The requested work needs configuration keys that are absent. | Exactly those missing keys. |
| `repair` | A configured recipe or health probe fails. | The failing keys, probes and safe engine error. |

An invalid config is reported and stopped, not overwritten as a missing config. A headless caller stops with a pointer to `docs/configuration.md`; it does not invent answers or write a proposal without approval.

1. **Read-only proposal.** The author uses Read, Grep and Glob with the shared environment skill and the plugin-specific layer. It never runs candidate commands or reads secret values, and never reads or edits `.av/local.toml`. It uses provenance to stop on blocking local overrides. It preserves existing names, unrelated keys, other plugins' sections and comments byte-for-byte. Every proposed target, command and recipe cites its repository evidence in a TOML comment. Unknown facts become questions, not guessed ports, credentials or commands.
2. **Transaction and questions.** Return `{proposal, questions[]}`. The proposal contains `config_text` (the full proposed `.av/config.toml`), `gitignore_add` (only needed absent ignore entries) and `allowed_keys` (the smallest authorized dotted-key scopes). In `create`, add only the shared keys the work needs plus the plugin's table, and collect unresolved prerequisites and creation-time policy choices together. The bootstrap never creates or fills `.av/secrets.local.env`; unresolved values become `file:.av/secrets.local.env#NAME` references.
3. **Preview without writes.** `config preview <proposal>` validates the merged config and proposed ignore rules, checks that only `allowed_keys` change and protected sections/comments remain byte-identical, and returns a diff, the **complete resulting trust subset**, its hash and a snapshot of `.av/config.toml` and `.gitignore`. Include sensitive settings already in `[env]`, even if another plugin added them; mask literals. On preview errors, allow one corrected read-only proposal round, then stop if errors remain.
4. **One approval.** Show the diff, ignore additions and complete trust subset together and ask for one confirmation of the write and trust hash. Declining writes nothing. Creation-time policy questions precede this confirmation; they do not replace it.
5. **Compare-and-swap apply.** `config apply <proposal> --snapshot S --approved-hash H` applies only the approved preview. If either file changed since preview, it writes nothing and reports a conflict. Otherwise it replaces the files atomically, revalidates and records trust **only for the approved hash**. Validation/hash failures restore the prior files only while they still contain the engine's own writes; concurrent edits are left intact and reported. Do not bypass preview or accept a different hash during apply.
6. **Missing private inputs.** If the work needs dotenv keys that are not populated, name the keys for the user to fill and stop without printing or asking for their values. Then re-check the work against the resulting config. A plugin repairing an active run must follow its own cleanup/restart contract before using the changed configuration.

## Shared code

The plugin-neutral implementation currently lives in QA:

- [`plugins/qa/skills/engine/scripts/av_config.py`](../plugins/qa/skills/engine/scripts/av_config.py): file loading/merge, provenance, value sources and restrictions, origins/loopback, shared `[env]` validation, per-plugin trust and guarded transactions. It has no QA imports.
- [`plugins/qa/skills/env-config/`](../plugins/qa/skills/env-config/SKILL.md): read-only repository detection and proposal rules for `[env]`, with no QA policy or recipe logic.

When the **second configurable plugin** is introduced, extract this generic layer into a small core plugin the consumers require, or ship byte-identical copies checked in CI. Choose that packaging then; do not create a second loader or `[env]` detector. Each plugin keeps only its call site and its own table's validation, recipes and policy choices.
