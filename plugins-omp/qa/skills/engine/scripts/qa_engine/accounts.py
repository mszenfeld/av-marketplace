"""Account recipes, durable cleanup ledger and the narrow tester secrets channel."""
from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
import fcntl
from pathlib import Path
import secrets
import shlex
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from av_config.errors import ConfigError
from av_config.files import atomic_write
from av_config.trust import canonical_hash
from qa_engine.common import read_object
from qa_engine.common import write_json
from qa_engine.config import Config
from qa_engine.config import DATABASE_NAMES
from qa_engine.config import mapping
from qa_engine.plan import check_plan
from qa_engine.plan import persona_token
from qa_engine.plan import run_plan
from qa_engine.recipes import build_recipe
from qa_engine.recipes import cookie_name
from qa_engine.services import Runtime
from qa_engine.services import require_trust

if TYPE_CHECKING:
    from qa_engine.models import Run

JSON = dict[str, Any]


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
        write_json(self.path, self.data, 0o600)


@contextmanager
def ledger(run: Run, config: Config) -> Iterator[Ledger]:
    path = config.store.path.parent / "qa-accounts.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        path.chmod(0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield Ledger(run, config)


def apply_outputs(identity: JSON, found: JSON, *, login: bool = False) -> None:
    if login:
        identity.pop("token", None)
        identity.pop("cookies", None)
    identity.update(found)
    if login:
        identity["issued_at"] = datetime.now(UTC).isoformat()


def login(runtime: Runtime, config: Config, identity: JSON) -> bool:
    recipe = config.recipes.get("login")
    if recipe is None:
        return False
    status, found = recipe.execute(runtime, identity, config.targets)
    if not recipe.succeeded(status):
        raise ConfigError(f"accounts login: unexpected status {status}")
    apply_outputs(identity, found, login=True)
    return True


def persona_requirements(run: Run, config: Config, section: str | None = None) -> dict[str, set[str]]:
    plan = run_plan(run)
    checked = check_plan(plan, config)
    if not checked["ok"]:
        raise ConfigError("plan requirements are not satisfied; run plan check")
    required: dict[str, set[str]] = {}
    configured = set(config.personas) | config.static.keys()
    for scenario in plan.scenarios:
        if section is not None and scenario.section != section:
            continue
        for token in scenario.tokens:
            recognized = persona_token(token, configured)
            if recognized:
                persona, field = recognized
                required.setdefault(persona, set()).add(field)
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
            login_recipe = config.recipes.get("login")
            cookie_fields = login_recipe.outputs.get("cookies", []) if login_recipe is not None else []
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
        return {name: str(path if path.is_absolute() else runtime.run.repo / path) for name in DATABASE_NAMES["sqlite"]}
    names = DATABASE_NAMES[str(kind)]
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
    write_json(directory / "secrets.json", dict(values), 0o600)
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


def _new_identity(runtime: Runtime, config: Config, persona: str) -> JSON:
    identity: JSON = {"persona": persona, "run": runtime.run.run_id, "static": persona in config.static}
    if persona in config.static:
        for field, source in mapping(config.static[persona]).items():
            identity[field] = runtime.resolve(str(source), f"qa.accounts.static.{persona}.{field}")
        return identity
    if config.policy["mutations"] == "deny":
        raise ConfigError("account provisioning forbidden under mutations=deny")
    if "create" not in config.recipes:
        raise ConfigError("qa.accounts.create: required to provision persona")
    template = config.accounts.get("email")
    if not isinstance(template, str) or not template:
        raise ConfigError("qa.accounts.email: required to provision persona")
    identity["email"] = template.replace("{run}", runtime.run.run_id).replace("{persona}", persona)
    password = str(config.accounts.get("password", "generate"))
    identity["password"] = secrets.token_urlsafe(18) + "Aa1!" if password == "generate" else runtime.resolve(password, "qa.accounts.password")
    runtime.secrets.remember(identity)
    return identity


def _retry_email(identity: JSON) -> None:
    local, separator, domain = str(identity["email"]).rpartition("@")
    suffix = secrets.token_hex(3)
    identity["email"] = f"{local}-{suffix}@{domain}" if separator else f"{identity['email']}-{suffix}"


def _create_account(runtime: Runtime, config: Config, accounts: Ledger, identity: JSON) -> None:
    create = config.recipes["create"]
    delete = config.recipes.get("delete")
    for attempt in range(2):
        record = {"persona": identity["persona"], "email": identity["email"], "id": None, "run_id": runtime.run.run_id,
                  "origin": delete.origin(config.targets) if delete is not None else None,
                  "recipe_hash": canonical_hash(delete.data if delete is not None else {}), "deleted": False, "status": "pending"}
        accounts.records.append(record)
        accounts.save()
        status, found = create.execute(runtime, identity, config.targets)
        record["id"] = found.get("id")
        accounts.save()
        if create.conflicted(status):
            record.update({"status": "conflict", "deleted": True})
            accounts.save()
            if attempt == 0:
                _retry_email(identity)
                continue
        if not create.succeeded(status):
            raise ConfigError(f"accounts create: unexpected status {status}")
        apply_outputs(identity, found)
        record["status"] = "created"
        accounts.save()
        break
    confirm = config.recipes.get("confirm")
    if confirm is not None:
        status, found = confirm.execute(runtime, identity, config.targets)
        if not confirm.succeeded(status):
            raise ConfigError(f"accounts confirm: unexpected status {status}")
        apply_outputs(identity, found)


def provision(run: Run, config: Config) -> JSON:
    """Provision only plan personas, always writing a private tester channel on success."""
    require_trust(config)
    required = persona_requirements(run, config)
    private = read_object(run.directory / "accounts.private.json")
    with Runtime(run, config) as runtime, ledger(run, config) as accounts:
        for persona in required:
            if persona in private:
                continue
            identity = _new_identity(runtime, config, persona)
            if not identity["static"]:
                _create_account(runtime, config, accounts, identity)
            login(runtime, config, identity)
            private[persona] = identity
        values = validate_required(required, private, config)
        checked = check_plan(run_plan(run), config)
        for name in cast(list[str], checked["values"]):
            values[f"QA_{name.upper()}"] = runtime.named("value", name)
        if checked["db_checks"]:
            values.update(database_values(runtime, config))
        runtime.secrets.remember(values)
        write_json(run.directory / "accounts.private.json", private, 0o600)
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
        runtime.secrets.remember(channel)
        write_json(run.directory / "accounts.private.json", private, 0o600)
        write_channel(run.directory, channel)
    return authenticated


def teardown(run: Run, config: Config) -> JSON:
    """Delete only matching ledger records; never trust a rebound target or recipe."""
    deleted: list[str] = []
    left: list[str] = []
    unresolved: list[str] = []
    trusted = not config.errors and config.state == "ok" and config.trust in {"trusted", "not-required"}
    recorded = run.record["config"]
    recorded_delete = build_recipe(mapping(mapping(recorded["qa"].get("accounts")).get("delete")), "delete")
    with ledger(run, config) as accounts:
        for record in accounts.records:
            if record.get("deleted"):
                continue
            persona = str(record["persona"])
            current = record.get("run_id") == run.run_id
            recipe = recorded_delete if current else config.recipes.get("delete")
            targets = recorded["env"].get("targets", {}) if current else config.targets
            if not trusted or recipe is None or canonical_hash(recipe.data) != record.get("recipe_hash") or recipe.origin(targets) != record.get("origin"):
                left.append(persona)
                continue
            if recipe.needs_id and not record.get("id"):
                unresolved.append(persona)
                continue
            identity = {"persona": persona, "run": record["run_id"], "email": record["email"], "id": record.get("id")}
            try:
                with Runtime(run, config) as runtime:
                    status, _ = recipe.execute(runtime, identity, targets)
                    if not recipe.succeeded(status):
                        left.append(persona)
                        continue
            except ConfigError:
                left.append(persona)
                continue
            record.update({"deleted": True, "status": "deleted"})
            accounts.save()
            deleted.append(persona)
    return {"deleted": deleted, "left": left, "unresolved": unresolved}
