"""Account recipes, durable cleanup ledger and the narrow tester secrets channel."""
from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
import fcntl
from http.cookies import CookieError
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import secrets
import shlex
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from urllib.parse import urlencode
from urllib.request import Request

from av_config import ConfigError
from av_config import atomic_write
from av_config import canonical_hash
from av_config import parse_origin
from qa_engine.config import Config
from qa_engine.config import cookie_name
from qa_engine.config import mapping
from qa_engine.plan import PERSONA_FIELD
from qa_engine.plan import check_plan
from qa_engine.services import Runtime
from qa_engine.services import http_request
from qa_engine.services import require_trust

if TYPE_CHECKING:
    from qa_engine.state import Run

JSON = dict[str, Any]
EXTRACT_PART = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?")


def read_object(path: Path) -> JSON:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as error:
        raise ConfigError(f"{path.name}: private state unavailable") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: invalid private state")
    return data


def write_object(path: Path, data: object) -> None:
    atomic_write(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode(), 0o600)


class Ledger:
    """Cross-run, repository-scoped records; persisted before any create request."""

    def __init__(self, run: Run, config: Config) -> None:
        self.path = config.store.path.parent / "qa-accounts.json"
        self.repo = str(run.repo.resolve())
        self.data = read_object(self.path)
        records = self.data.setdefault(self.repo, [])
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ConfigError("qa-accounts.json: invalid ledger")
        self.records: list[JSON] = records

    def save(self) -> None:
        write_object(self.path, self.data)


@contextmanager
def ledger(run: Run, config: Config) -> Iterator[Ledger]:
    path = config.store.path.parent / "qa-accounts.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        path.chmod(0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield Ledger(run, config)


def origin_for(recipe: Mapping[str, object], targets: Mapping[str, str]) -> str | None:
    if recipe.get("kind") != "http":
        return None
    target = recipe.get("target")
    if not isinstance(target, str) or target not in targets:
        raise ConfigError("delete recipe target unavailable")
    scheme, host, port = parse_origin(targets[target], origin_only=True)
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}:{port}"


def extract(value: object, path: str) -> str | None:
    for match in EXTRACT_PART.finditer(path):
        if not isinstance(value, dict):
            return None
        value = value.get(match.group(1))
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(value, list) or index >= len(value):
                return None
            value = value[index]
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def output_fields(recipe: Mapping[str, object]) -> JSON:
    if recipe.get("kind") == "http":
        return {key: recipe[key] for key in ("id", "token", "cookies") if key in recipe}
    outputs = recipe.get("outputs", {})
    return {name: True for name in outputs} if isinstance(outputs, list) else cast(JSON, mapping(outputs))


def execute_recipe(runtime: Runtime, recipe: JSON, identity: JSON, name: str, targets: Mapping[str, str]) -> tuple[int, JSON]:
    """Run one recipe; collect outputs even on failed create for safe pending cleanup."""
    outputs = output_fields(recipe)
    found: JSON = {}
    if recipe.get("kind") == "command":
        command = str(recipe["run"])
        environ = runtime.command_environment(command)
        # A delete must not accidentally inherit another run's password.
        for key in ("QA_PERSONA", "QA_EMAIL", "QA_PASSWORD", "QA_ID"):
            environ.pop(key, None)
        for field in ("persona", "email", "id", "password"):
            if field == "password" and name == "delete":
                continue
            environ[f"QA_{field.upper()}"] = str(identity.get(field) or "")
        code, stdout, stderr = runtime.shell(command, environ, f"accounts {name}",
                                             timeout=float(runtime.run.budget["minutes"]) * 60)
        try:
            data = json.loads(stdout) if outputs else {}
        except ValueError as error:
            runtime.logs.append((f"accounts {name}", "***"))
            raise ConfigError(f"accounts {name}: invalid command JSON output") from error
        if not isinstance(data, dict) or set(data) != set(outputs):
            runtime.logs.append((f"accounts {name}", "***"))
            raise ConfigError(f"accounts {name}: command outputs do not match declaration")
        for field in ("id", "token"):
            value = data.get(field)
            if field in outputs and isinstance(value, (str, int)) and not isinstance(value, bool):
                found[field] = str(value)
        if "cookies" in outputs:
            command_cookies = data.get("cookies")
            if not isinstance(command_cookies, dict) or set(command_cookies) != set(outputs["cookies"]):
                runtime.logs.append((f"accounts {name}", "***"))
                raise ConfigError(f"accounts {name}: command cookies do not match declaration")
            found["cookies"] = {key: value for key, value in command_cookies.items() if isinstance(value, str)}
        runtime.remember(data)
        runtime.remember(stdout.rstrip("\n"))
        runtime.logs.append((f"accounts {name}", stdout + stderr if not code else "***"))
        return code, found

    path = runtime.substitute(recipe["path"], identity)
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path or "#" in path:
        raise ConfigError(f"accounts {name}: invalid rendered request path")
    headers = runtime.substitute(recipe.get("headers", {}), identity)
    body = None
    if "json" in recipe:
        body = json.dumps(runtime.substitute(recipe["json"], identity)).encode()
        headers.setdefault("Content-Type", "application/json")
    elif "form" in recipe:
        body = urlencode(runtime.substitute(recipe["form"], identity)).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    try:
        request = Request(targets[recipe["target"]] + path, data=body, headers=headers, method=recipe["method"])
        status, raw, cookie_headers = http_request(request)
    except (ValueError, UnicodeError) as error:
        raise ConfigError(f"accounts {name}: invalid HTTP request") from error
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        data = {}
    for field in ("id", "token"):
        if field in outputs:
            found[field] = extract(data, str(outputs[field]))
    if "cookies" in outputs:
        cookies: dict[str, str] = {}
        for header in cookie_headers:
            parsed = SimpleCookie()
            try:
                parsed.load(header)
            except CookieError:
                continue
            for cookie in outputs["cookies"]:
                if cookie in parsed:
                    cookies[cookie] = parsed[cookie].value
        found["cookies"] = cookies
    runtime.remember(found)
    return status, found


def succeeded(recipe: JSON, status: int) -> bool:
    return status in recipe.get("expect", []) if recipe["kind"] == "http" else status == 0


def apply_outputs(identity: JSON, found: JSON, *, login: bool = False) -> None:
    if login:
        identity.pop("token", None)
        identity.pop("cookies", None)
    identity.update(found)
    if login:
        identity["issued_at"] = datetime.now(UTC).isoformat()


def login(runtime: Runtime, config: Config, identity: JSON) -> bool:
    recipe = mapping(config.accounts.get("login"))
    if not recipe:
        return False
    status, found = execute_recipe(runtime, recipe, identity, "login", config.targets)
    if not succeeded(recipe, status):
        raise ConfigError(f"accounts login: unexpected status {status}")
    apply_outputs(identity, found, login=True)
    return True


def persona_requirements(run: Run, config: Config, section: str | None = None) -> dict[str, set[str]]:
    plan = run.plan()
    checked = check_plan(plan, config)
    if not checked["ok"]:
        raise ConfigError("plan requirements are not satisfied; run plan check")
    required: dict[str, set[str]] = {}
    configured = sorted(set(config.personas) | config.static.keys(), key=lambda name: (-len(name), name))
    for scenario in plan.scenarios:
        if section is not None and scenario.section != section:
            continue
        for token in scenario.tokens:
            for persona in configured:
                prefix = f"QA_{persona.upper()}_"
                field = token.removeprefix(prefix)
                if token.startswith(prefix) and PERSONA_FIELD.fullmatch(field):
                    required.setdefault(persona, set()).add(field)
                    break
    return required


def persona_values(persona: str, identity: JSON) -> dict[str, str]:
    prefix = f"QA_{persona.upper()}_"
    result = {prefix + field.upper(): str(identity[field]) for field in ("email", "password", "id", "token") if identity.get(field) not in (None, "")}
    cookies = mapping(identity.get("cookies"))
    if cookies:
        result[prefix + "COOKIE"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        result.update({prefix + "COOKIE_" + cookie_name(name): value for name, value in cookies.items() if isinstance(value, str) and value})
    return result


def validate_required(required: Mapping[str, set[str]], private: JSON, config: Config) -> dict[str, str]:
    result: dict[str, str] = {}
    for persona, fields in required.items():
        values = persona_values(persona, mapping(private.get(persona)))
        if "COOKIE" in fields:
            cookie_fields = output_fields(mapping(config.accounts.get("login"))).get("cookies", [])
            cookies = mapping(mapping(private.get(persona)).get("cookies"))
            if any(not cookies.get(name) for name in cookie_fields):
                raise ConfigError(f"QA_{persona.upper()}_COOKIE: required output missing or empty")
        for field in sorted(fields):
            name = f"QA_{persona.upper()}_{field}"
            if not values.get(name):
                raise ConfigError(f"{name}: required output missing or empty")
            result[name] = values[name]
    return result


def database_values(runtime: Runtime, config: Config) -> dict[str, str]:
    database = config.database
    kind = database["kind"]
    if kind == "sqlite":
        path = Path(str(database["path"]))
        return {"SQLITE_DB": str(path if path.is_absolute() else runtime.run.repo / path)}
    names = ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD") if kind == "postgres" else ("MYSQL_HOST", "MYSQL_TCP_PORT", "MYSQL_USER", "MYSQL_DATABASE", "MYSQL_PWD")
    port = database.get("port", 5432 if kind == "postgres" else 3306)
    password = runtime.resolve(str(database["password"]), "env.database.password")
    return dict(zip(names, (str(database["host"]), str(port), str(database["user"]), str(database["name"]), password), strict=True))


def write_channel(directory: Path, values: Mapping[str, str], *, initial: bool = False) -> None:
    """Atomic replacements; a refresh never widens names or touches the names file."""
    lines: list[str] = []
    for name, value in sorted(values.items()):
        escaped = value.replace("'", "'\\''")
        lines.append(f"export {name}='{escaped}'\n")
    atomic_write(directory / "secrets.env", "".join(lines).encode(), 0o600)
    write_object(directory / "secrets.json", dict(values))
    if initial:
        atomic_write(directory / "redact-names", ("".join(f"{name}\n" for name in sorted(values))).encode(), 0o600)
        atomic_write(directory / "load.sh", loader_script(directory).encode(), 0o600)


def loader_script(directory: Path) -> str:
    path = shlex.quote(str(directory / "secrets.env"))
    return f'''# Private run channel. Source with the names this request needs.
for qa_channel_name in $(set | cut -d= -f1); do
    case "$qa_channel_name" in
        *[!A-Za-z0-9_]*) continue ;;
        QA_*|PG*|MYSQL_*|SQLITE_DB) unset "$qa_channel_name" ;;
    esac
done
qa_channel_file={path}
if [ -L "$qa_channel_file" ] || [ ! -f "$qa_channel_file" ] || [ ! -r "$qa_channel_file" ] || [ ! -O "$qa_channel_file" ]; then
    printf '%s\\n' 'secrets.env: private readable regular file required' >&2
    return 1
fi
case "$-" in *a*) qa_channel_allexport=yes ;; *) qa_channel_allexport=no ;; esac
set -a
. "$qa_channel_file"
qa_channel_status=$?
[ "$qa_channel_allexport" = yes ] || set +a
[ "$qa_channel_status" = 0 ] || return 1
for qa_channel_name in "$@"; do
    case "$qa_channel_name" in
        ''|*[!A-Z0-9_]*) printf '%s\\n' 'invalid required channel name' >&2; return 1 ;;
    esac
    eval 'qa_channel_value=${{'"$qa_channel_name"'-}}'
    if [ -z "$qa_channel_value" ]; then
        printf '%s: required value missing\\n' "$qa_channel_name" >&2
        unset qa_channel_value
        return 1
    fi
done
unset qa_channel_name qa_channel_file qa_channel_allexport qa_channel_status qa_channel_value
'''


def provision(run: Run, config: Config) -> JSON:
    """Provision only plan personas, always writing a private tester channel on success."""
    require_trust(config)
    required = persona_requirements(run, config)
    private = read_object(run.directory / "accounts.private.json")
    with Runtime(run, config) as runtime, ledger(run, config) as accounts:
        for persona in required:
            if persona in private:
                continue
            identity: JSON = {"persona": persona, "run": run.run_id, "static": persona in config.static}
            if persona in config.static:
                for field, source in mapping(config.static[persona]).items():
                    identity[field] = runtime.resolve(str(source), f"qa.accounts.static.{persona}.{field}")
            else:
                if config.policy["mutations"] == "deny":
                    raise ConfigError("account provisioning forbidden under mutations=deny")
                create = mapping(config.accounts.get("create"))
                if not create:
                    raise ConfigError("qa.accounts.create: required to provision persona")
                template = config.accounts.get("email")
                if not isinstance(template, str) or not template:
                    raise ConfigError("qa.accounts.email: required to provision persona")
                identity["email"] = template.replace("{run}", run.run_id).replace("{persona}", persona)
                password = str(config.accounts.get("password", "generate"))
                identity["password"] = secrets.token_urlsafe(18) + "Aa1!" if password == "generate" else runtime.resolve(password, "qa.accounts.password")
                runtime.remember(identity)
                delete = mapping(config.accounts.get("delete"))
                for attempt in range(2):
                    record = {"persona": persona, "email": identity["email"], "id": None, "run_id": run.run_id,
                              "origin": origin_for(delete, config.targets), "recipe_hash": canonical_hash(delete), "deleted": False, "status": "pending"}
                    accounts.records.append(record)
                    accounts.save()
                    status, found = execute_recipe(runtime, create, identity, "create", config.targets)
                    record["id"] = found.get("id")
                    accounts.save()
                    if create["kind"] == "http" and status in cast(list[int], create.get("conflict", [])):
                        record.update({"status": "conflict", "deleted": True})
                        accounts.save()
                        if attempt == 0:
                            local, separator, domain = str(identity["email"]).rpartition("@")
                            suffix = secrets.token_hex(3)
                            identity["email"] = f"{local}-{suffix}@{domain}" if separator else f"{identity['email']}-{suffix}"
                            continue
                    if not succeeded(create, status):
                        raise ConfigError(f"accounts create: unexpected status {status}")
                    apply_outputs(identity, found)
                    record["status"] = "created"
                    accounts.save()
                    break
                confirm = mapping(config.accounts.get("confirm"))
                if confirm:
                    status, found = execute_recipe(runtime, confirm, identity, "confirm", config.targets)
                    if not succeeded(confirm, status):
                        raise ConfigError(f"accounts confirm: unexpected status {status}")
                    apply_outputs(identity, found)
            login(runtime, config, identity)
            private[persona] = identity
        values = validate_required(required, private, config)
        checked = check_plan(run.plan(), config)
        for name in cast(list[str], checked["values"]):
            values[f"QA_{name.upper()}"] = runtime.named("value", name)
        if checked["db_checks"]:
            values.update(database_values(runtime, config))
        runtime.remember(values)
        write_object(run.directory / "accounts.private.json", private)
        write_channel(run.directory, values, initial=True)
    return {"personas": [{"name": name, "email": private[name]["email"], "id": private[name].get("id"), "static": private[name]["static"]} for name in required],
            "exposed": sorted(values), "secrets_env": str(run.directory / "secrets.env"), "secrets_json": str(run.directory / "secrets.json"),
            "redact_names": str(run.directory / "redact-names"), "loader": str(run.directory / "load.sh")}


def refresh(run: Run, config: Config, section: str | None = None) -> list[str]:
    """Re-login section personas, preserving all non-auth channel entries and names."""
    require_trust(config)
    required = persona_requirements(run, config, section)
    private = read_object(run.directory / "accounts.private.json")
    channel = read_object(run.directory / "secrets.json")
    if not (run.directory / "secrets.env").is_file() or not (run.directory / "secrets.json").is_file():
        raise ConfigError("secrets channel missing; run accounts provision")
    authenticated: list[str] = []
    with Runtime(run, config) as runtime:
        for persona in required:
            if persona not in private:
                raise ConfigError(f"{persona}: account not provisioned")
            if login(runtime, config, private[persona]):
                authenticated.append(persona)
        all_required = {persona: fields for persona, fields in persona_requirements(run, config).items() if persona in required}
        values = validate_required(all_required, private, config)
        for persona, fields in all_required.items():
            for field in fields:
                name = f"QA_{persona.upper()}_{field}"
                if name in channel and (field == "TOKEN" or field == "COOKIE" or field.startswith("COOKIE_")):
                    channel[name] = values[name]
        runtime.remember(channel)
        write_object(run.directory / "accounts.private.json", private)
        write_channel(run.directory, channel)
    return authenticated


def teardown(run: Run, config: Config) -> JSON:
    """Delete only matching ledger records; never trust a rebound target or recipe."""
    deleted: list[str] = []
    left: list[str] = []
    unresolved: list[str] = []
    trusted = not config.errors and config.state == "ok" and config.trust in {"trusted", "not-required"}
    recorded = run.record["config"]
    with ledger(run, config) as accounts:
        for record in accounts.records:
            if record.get("deleted"):
                continue
            persona = str(record["persona"])
            current = record.get("run_id") == run.run_id
            recipe = mapping(recorded["qa"].get("accounts", {})).get("delete") if current else config.accounts.get("delete")
            recipe = mapping(recipe)
            targets = recorded["env"].get("targets", {}) if current else config.targets
            if not trusted or not recipe or canonical_hash(recipe) != record.get("recipe_hash") or origin_for(recipe, targets) != record.get("origin"):
                left.append(persona)
                continue
            needs_id = "{id}" in json.dumps(recipe) if recipe["kind"] == "http" else bool(re.search(r"\$(?:QA_ID\b|\{QA_ID[}:])", str(recipe.get("run", ""))))
            if needs_id and not record.get("id"):
                unresolved.append(persona)
                continue
            identity = {"persona": persona, "run": record["run_id"], "email": record["email"], "id": record.get("id")}
            try:
                with Runtime(run, config) as runtime:
                    status, _ = execute_recipe(runtime, recipe, identity, "delete", targets)
                    if not succeeded(recipe, status):
                        left.append(persona)
                        continue
            except ConfigError:
                left.append(persona)
                continue
            record.update({"deleted": True, "status": "deleted"})
            accounts.save()
            deleted.append(persona)
    return {"deleted": deleted, "left": left, "unresolved": unresolved}
