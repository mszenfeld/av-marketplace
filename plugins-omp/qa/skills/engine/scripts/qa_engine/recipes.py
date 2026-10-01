"""Validated account recipes and their credential-safe execution contracts."""
from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from http.client import HTTPException
from http.cookies import CookieError
from http.cookies import SimpleCookie
import json
import math
import re
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler
from urllib.request import ProxyHandler
from urllib.request import Request
from urllib.request import build_opener

from av_config.errors import ConfigError
from av_config.files import Configuration
from av_config.sources import SOURCE_KINDS
from av_config.sources import flatten
from av_config.origins import parse_origin
from av_config.origins import require_secure_transport
from qa_engine.common import format_origin

if TYPE_CHECKING:
    from qa_engine.config import Config
    from qa_engine.services import Runtime

JSON = dict[str, Any]
PLACEHOLDER = re.compile(r"(?<!\$)\{([^{}]+)\}")
EXTRACTOR = re.compile(r"\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?)*\Z")
EXTRACT_PART = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?")


def cookie_name(name: str) -> str:
    """Normalize a cookie name for its tester environment variable."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def request_path(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and not value.startswith("//") and "\\" not in value and "#" not in value


def _status_codes(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(type(code) is int and 100 <= code <= 599 for code in value)


def _validate_cookies(validator: Configuration, cookies: object, key: str) -> None:
    if not isinstance(cookies, list) or not cookies or any(not isinstance(cookie, str) or not cookie or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", cookie) or not cookie_name(cookie) for cookie in cookies):
        validator.error(key, "expected cookie names")
        return
    normalized = [cookie_name(cookie) for cookie in cookies]
    if len(normalized) != len(set(normalized)):
        validator.error(key, "cookie names collide after normalization")


def _validate_json(validator: Configuration, value: object, key: str) -> None:
    if isinstance(value, dict):
        for field, item in value.items():
            _validate_json(validator, item, f"{key}.{field}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(validator, item, f"{key}.{index}")
    elif isinstance(value, str) and value.startswith(SOURCE_KINDS):
        validator.error(key, "a recipe string cannot be a value source")
    elif not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value):
        validator.error(key, "expected a JSON-compatible value")


def _validate_strings(validator: Configuration, body: Mapping[str, object], prefix: str) -> None:
    for field, value in body.items():
        if not isinstance(value, str):
            validator.error(f"{prefix}.{field}", "expected a string")
        elif value.startswith(SOURCE_KINDS):
            validator.error(f"{prefix}.{field}", "a recipe string cannot be a value source")


def _validate_placeholders(config: Config, value: object, key: str, name: str) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_placeholders(config, item, key, name)
    elif isinstance(value, dict):
        for field, item in value.items():
            _validate_placeholders(config, item, f"{key}.{field}", name)
    if not isinstance(value, str):
        return
    secrets = config.env.get("secrets", {})
    for placeholder in PLACEHOLDER.findall(value):
        if placeholder == "password" and name == "delete":
            config.shared.error(key, "delete cannot use a password placeholder")
        elif placeholder.startswith("secret."):
            if not isinstance(secrets, dict) or placeholder[7:] not in secrets:
                config.shared.error(key, "placeholder names an undefined secret")
        elif placeholder.startswith("value."):
            if placeholder[6:] not in config.values:
                config.shared.error(key, "placeholder names an undefined value")
        elif placeholder not in {"email", "password", "persona", "run", "id"}:
            config.shared.error(key, "unsupported recipe placeholder")


class NoRedirect(HTTPRedirectHandler):
    """Keep credentials on the configured origin, even on a redirect response."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def http_request(request: Request) -> tuple[int, bytes, list[str]]:
    """Return status/body/cookies without following redirects or echoing payloads."""
    require_secure_transport(request.full_url)
    try:
        response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=30)
    except HTTPError as error:
        response = error
    except (URLError, OSError, HTTPException, ValueError) as error:
        raise ConfigError("HTTP recipe or probe unavailable") from error

    try:
        with response:
            return response.code, response.read(), response.headers.get_all("Set-Cookie", [])
    except (OSError, HTTPException) as error:
        raise ConfigError("HTTP recipe or probe response unavailable") from error


class Recipe(ABC):
    """One compiled recipe; raw data remains unchanged for trust and ledger hashes."""

    outputs: JSON
    needs_id: bool

    def __init__(self, data: Mapping[str, object], name: str) -> None:
        self.data = cast(JSON, data)
        self.name = name

    @classmethod
    @abstractmethod
    def validate(cls, data: Mapping[str, object], name: str, config: Config) -> None:
        """Report schema errors without resolving any sources or running the recipe."""

    @abstractmethod
    def execute(self, runtime: Runtime, identity: JSON, targets: Mapping[str, str]) -> tuple[int, JSON]:
        """Collect outputs even on a failed create so pending cleanup remains safe."""

    @abstractmethod
    def succeeded(self, status: int) -> bool:
        """Interpret this recipe's transport-specific success status."""

    def conflicted(self, status: int) -> bool:
        return False

    def origin(self, targets: Mapping[str, str]) -> str | None:
        return None


class HttpRecipe(Recipe):
    def __init__(self, data: Mapping[str, object], name: str) -> None:
        super().__init__(data, name)
        self.outputs = {key: data[key] for key in ("id", "token", "cookies") if key in data}
        self.needs_id = "{id}" in json.dumps(data)

    @classmethod
    def validate(cls, data: Mapping[str, object], name: str, config: Config) -> None:
        validator = config.shared
        prefix = f"qa.accounts.{name}"
        validator.keys(data, {"kind", "target", "method", "path", "headers", "json", "form", "expect", "conflict", "id", "token", "cookies"}, prefix)
        target = data.get("target")
        if not isinstance(target, str) or target not in config.targets:
            validator.error(f"{prefix}.target", "undefined target")
        elif isinstance(config.targets[target], str):
            try:
                require_secure_transport(config.targets[target])
            except ConfigError as error:
                validator.error(f"env.targets.{target}", str(error))
        if data.get("method") not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            validator.error(f"{prefix}.method", "expected an HTTP method")
        path = data.get("path")
        if isinstance(path, str) and path.startswith(SOURCE_KINDS):
            validator.error(f"{prefix}.path", "a request path cannot be a value source")
        elif not request_path(path):
            validator.error(f"{prefix}.path", "expected a target-relative request path")
        for key in ("expect", "conflict"):
            if key == "expect" or key in data:
                if not _status_codes(data.get(key)):
                    validator.error(f"{prefix}.{key}", "expected HTTP status codes")
        for key in ("headers", "form", "json"):
            if key not in data:
                continue
            body = validator.table(data[key], f"{prefix}.{key}")
            if key == "json":
                _validate_json(validator, body, f"{prefix}.{key}")
            else:
                _validate_strings(validator, body, f"{prefix}.{key}")
        if "json" in data and "form" in data:
            validator.error(prefix, "json and form are mutually exclusive")
        for key in ("id", "token"):
            extractor = data.get(key)
            if key in data and (not isinstance(extractor, str) or not EXTRACTOR.fullmatch(extractor)):
                validator.error(f"{prefix}.{key}", "expected a dotted JSON extractor")
        if "cookies" in data:
            _validate_cookies(validator, data["cookies"], f"{prefix}.cookies")
        for key, item in flatten(data, prefix).items():
            _validate_placeholders(config, item, key, name)

    def origin(self, targets: Mapping[str, str]) -> str:
        target = self.data.get("target")
        if not isinstance(target, str) or target not in targets:
            raise ConfigError("delete recipe target unavailable")
        return format_origin(parse_origin(targets[target], origin_only=True))

    def succeeded(self, status: int) -> bool:
        return status in self.data.get("expect", [])

    def conflicted(self, status: int) -> bool:
        return status in self.data.get("conflict", [])

    def execute(self, runtime: Runtime, identity: JSON, targets: Mapping[str, str]) -> tuple[int, JSON]:
        path = runtime.substitute(self.data["path"], identity)
        if not request_path(path):
            raise ConfigError(f"accounts {self.name}: invalid rendered request path")
        headers = runtime.substitute(self.data.get("headers", {}), identity)
        body = self._body(runtime, identity, headers)
        try:
            request = Request(targets[self.data["target"]] + path, data=body, headers=headers, method=self.data["method"])
            status, raw, cookie_headers = http_request(request)
        except ConfigError:
            raise
        except (ValueError, UnicodeError) as error:
            raise ConfigError(f"accounts {self.name}: invalid HTTP request") from error
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            data = {}
        found = {field: extract(data, str(self.outputs[field])) for field in ("id", "token") if field in self.outputs}
        if "cookies" in self.outputs:
            found["cookies"] = _http_cookies(cookie_headers, self.outputs["cookies"])
        runtime.secrets.remember(found)
        return status, found

    def _body(self, runtime: Runtime, identity: JSON, headers: JSON) -> bytes | None:
        if "json" in self.data:
            headers.setdefault("Content-Type", "application/json")
            return json.dumps(runtime.substitute(self.data["json"], identity)).encode()
        if "form" in self.data:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            return urlencode(runtime.substitute(self.data["form"], identity)).encode()
        return None


class CommandRecipe(Recipe):
    def __init__(self, data: Mapping[str, object], name: str) -> None:
        super().__init__(data, name)
        outputs = data.get("outputs", {})
        self.outputs = {field: True for field in outputs} if isinstance(outputs, list) else cast(JSON, outputs)
        self.needs_id = name == "delete"

    @classmethod
    def validate(cls, data: Mapping[str, object], name: str, config: Config) -> None:
        validator = config.shared
        prefix = f"qa.accounts.{name}"
        validator.keys(data, {"kind", "run", "outputs", "env"}, prefix)
        validator.command_env(data.get("env", []), f"{prefix}.env")
        command = data.get("run")
        if not isinstance(command, str) or not command:
            validator.error(f"{prefix}.run", "expected a non-empty command")
        elif command.startswith(SOURCE_KINDS):
            validator.error(f"{prefix}.run", "a command cannot be a value source")
        outputs = data.get("outputs", {})
        key = f"{prefix}.outputs"
        if isinstance(outputs, list):
            if any(item not in ("id", "token") for item in outputs):
                validator.error(key, "unsupported command output")
        elif isinstance(outputs, dict):
            validator.keys(outputs, {"id", "token", "cookies"}, key)
            for field in ("id", "token"):
                if field in outputs and outputs[field] is not True:
                    validator.error(f"{key}.{field}", "expected true")
            if "cookies" in outputs:
                _validate_cookies(validator, outputs["cookies"], f"{key}.cookies")
        else:
            validator.error(key, "expected an output declaration")

    def succeeded(self, status: int) -> bool:
        return status == 0

    def execute(self, runtime: Runtime, identity: JSON, targets: Mapping[str, str]) -> tuple[int, JSON]:
        command = str(self.data["run"])
        environ = runtime.command_environment(cast(list[str], self.data.get("env", [])))
        # A delete must not accidentally inherit another run's password.
        for key in ("QA_PERSONA", "QA_EMAIL", "QA_PASSWORD", "QA_ID"):
            environ.pop(key, None)
        for field in ("persona", "email", "id", "password"):
            if field == "password" and self.name == "delete":
                continue
            environ[f"QA_{field.upper()}"] = str(identity.get(field) or "")
        code, stdout, stderr = runtime.shell(command, environ, f"accounts {self.name}",
                                             timeout=float(runtime.run.budget["minutes"]) * 60)
        data = self._output(runtime, stdout)
        found = _command_fields(data, self.outputs)
        runtime.secrets.remember(data)
        runtime.secrets.remember(stdout.rstrip("\n"))
        runtime.log_command(f"accounts {self.name}", code, stdout, stderr)
        return code, found

    def _output(self, runtime: Runtime, stdout: str) -> JSON:
        try:
            data = json.loads(stdout) if self.outputs else {}
        except ValueError as error:
            runtime.logs.append((f"accounts {self.name}", "***"))
            raise ConfigError(f"accounts {self.name}: invalid command JSON output") from error
        if not isinstance(data, dict) or set(data) != set(self.outputs):
            runtime.logs.append((f"accounts {self.name}", "***"))
            raise ConfigError(f"accounts {self.name}: command outputs do not match declaration")
        if "cookies" in self.outputs:
            cookies = data.get("cookies")
            if not isinstance(cookies, dict) or set(cookies) != set(self.outputs["cookies"]):
                runtime.logs.append((f"accounts {self.name}", "***"))
                raise ConfigError(f"accounts {self.name}: command cookies do not match declaration")
        return data


RECIPE_TYPES: dict[str, type[Recipe]] = {"http": HttpRecipe, "command": CommandRecipe}


def build_recipe(data: Mapping[str, object], name: str, config: Config | None = None) -> Recipe | None:
    """Validate and compile config, or compile an already-validated run snapshot."""
    kind = data.get("kind")
    recipe_type = RECIPE_TYPES.get(kind) if isinstance(kind, str) else None
    if recipe_type is None:
        if config is not None:
            config.shared.error(f"qa.accounts.{name}.kind", "expected http or command")
        return None
    if config is not None:
        previous_errors = len(config.errors)
        recipe_type.validate(data, name, config)
        if len(config.errors) != previous_errors:
            return None
    return recipe_type(data, name)


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


def _http_cookies(headers: list[str], names: list[str]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for header in headers:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except CookieError:
            continue
        for cookie in names:
            if cookie in parsed:
                cookies[cookie] = parsed[cookie].value
    return cookies


def _command_fields(data: JSON, outputs: JSON) -> JSON:
    found: JSON = {}
    for field in ("id", "token"):
        value = data.get(field)
        if field in outputs and isinstance(value, (str, int)) and not isinstance(value, bool):
            found[field] = str(value)
    if "cookies" in outputs:
        found["cookies"] = {key: value for key, value in data["cookies"].items() if isinstance(value, str)}
    return found
