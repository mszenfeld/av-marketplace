"""Validated cleanup recipes and their credential-safe execution contracts."""
from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from http.client import HTTPException
import json
import math
import os
import re
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler
from urllib.request import ProxyHandler
from urllib.request import Request
from urllib.request import build_opener

from av_config.errors import ConfigError
from av_config.files import Configuration
from av_config.sources import SOURCE_KINDS
from av_config.origins import parse_origin
from av_config.origins import require_secure_transport
from qa_engine.common import format_origin

if TYPE_CHECKING:
    from qa_engine.config import Config
    from qa_engine.services import Runtime

JSON = dict[str, Any]
PLACEHOLDER = re.compile(r"(?<!\$)\{([^{}]+)\}")


def request_path(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and not value.startswith("//") and "\\" not in value and "#" not in value


def _status_codes(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(type(code) is int and 100 <= code <= 599 for code in value)


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


def _validate_placeholders(config: Config, value: object, key: str) -> None:
    if isinstance(value, dict):
        for field, item in value.items():
            _validate_placeholders(config, item, f"{key}.{field}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_placeholders(config, item, f"{key}.{index}")
        return
    if not isinstance(value, str):
        return
    secrets = config.env.get("secrets", {})
    for placeholder in PLACEHOLDER.findall(value):
        if placeholder.startswith("secret."):
            if not isinstance(secrets, dict) or placeholder[7:] not in secrets:
                config.shared.error(key, "placeholder names an undefined secret")
        elif placeholder.startswith("value."):
            if placeholder[6:] not in config.values:
                config.shared.error(key, "placeholder names an undefined value")
        elif placeholder not in {"email", "id", "tag"}:
            config.shared.error(key, "unsupported recipe placeholder")


class NoRedirect(HTTPRedirectHandler):
    """Keep credentials on the configured origin, even on a redirect response."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def http_request(request: Request) -> int:
    """Return only status without following redirects or retaining response data."""
    require_secure_transport(request.full_url)
    try:
        response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=30)
    except HTTPError as error:
        response = error
    except (URLError, OSError, HTTPException, ValueError) as error:
        raise ConfigError("HTTP recipe or probe unavailable") from error

    try:
        with response:
            return response.code
    except (OSError, HTTPException) as error:
        raise ConfigError("HTTP recipe or probe response unavailable") from error


class Recipe(ABC):
    """One compiled recipe; raw data remains unchanged for trust and ledger hashes."""

    needs_id: bool

    def __init__(self, data: Mapping[str, object], name: str) -> None:
        self.data = cast(JSON, data)
        self.name = name

    @classmethod
    @abstractmethod
    def validate(cls, data: Mapping[str, object], name: str, config: Config) -> None:
        """Report schema errors without source resolution or recipe execution."""

    @abstractmethod
    def execute(self, runtime: Runtime, identity: JSON, targets: Mapping[str, str]) -> int:
        """Execute the cleanup and return its transport-specific status."""

    @abstractmethod
    def succeeded(self, status: int) -> bool:
        """Interpret this recipe's transport-specific success status."""

    def destination(self, config: Config) -> str | None:
        return None


class HttpRecipe(Recipe):
    def __init__(self, data: Mapping[str, object], name: str) -> None:
        super().__init__(data, name)
        self.needs_id = "{id}" in json.dumps(data)

    @classmethod
    def validate(cls, data: Mapping[str, object], name: str, config: Config) -> None:
        validator = config.shared
        prefix = f"qa.{name}"
        validator.keys(data, {"kind", "target", "method", "path", "headers", "json", "form", "expect"}, prefix)
        target = data.get("target")
        if not isinstance(target, str) or not isinstance(config.targets.get(target), str):
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
        if not _status_codes(data.get("expect")):
            validator.error(f"{prefix}.expect", "expected HTTP status codes")
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
        _validate_placeholders(config, data, prefix)
        if "{email}" not in json.dumps(data, default=str):
            validator.error(prefix, "cleanup recipe must use {email}")

    def destination(self, config: Config) -> str:
        return format_origin(parse_origin(config.targets[str(self.data["target"])], origin_only=True))

    def succeeded(self, status: int) -> bool:
        return status in self.data.get("expect", [])

    def execute(self, runtime: Runtime, identity: JSON, targets: Mapping[str, str]) -> int:
        encoded = {key: quote(str(value), safe="") for key, value in identity.items()}
        path = runtime.substitute(self.data["path"], encoded)
        if not request_path(path):
            raise ConfigError(f"{self.name}: invalid rendered request path")
        headers = runtime.substitute(self.data.get("headers", {}), identity)
        body = self._body(runtime, identity, headers)
        try:
            request = Request(targets[self.data["target"]] + path, data=body, headers=headers, method=self.data["method"])
            status = http_request(request)
        except (ValueError, UnicodeError) as error:
            raise ConfigError(f"{self.name}: invalid HTTP request") from error
        return status

    def _body(self, runtime: Runtime, identity: JSON, headers: JSON) -> bytes | None:
        if "json" in self.data:
            headers.setdefault("Content-Type", "application/json")
            return json.dumps(runtime.substitute(self.data["json"], identity)).encode()
        if "form" in self.data:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            return urlencode(runtime.substitute(self.data["form"], identity)).encode()
        return None


class CommandRecipe(Recipe):
    needs_id = False

    @classmethod
    def validate(cls, data: Mapping[str, object], name: str, config: Config) -> None:
        prefix = f"qa.{name}"
        config.shared.keys(data, {"kind", "run"}, prefix)
        command = data.get("run")
        if not isinstance(command, str) or not command:
            config.shared.error(f"{prefix}.run", "expected a non-empty command")
        elif command.startswith(SOURCE_KINDS):
            config.shared.error(f"{prefix}.run", "a command cannot be a value source")

    def succeeded(self, status: int) -> bool:
        return status == 0

    def execute(self, runtime: Runtime, identity: JSON, targets: Mapping[str, str]) -> int:
        environ = {key: value for key, value in os.environ.items() if not key.startswith("QA_")}
        environ.update({f"QA_{field.upper()}": str(identity.get(field) or "") for field in ("email", "id", "tag")})
        code, stdout, stderr = runtime.shell(str(self.data["run"]), environ, self.name, timeout=60)
        runtime.log_command(self.name, code, stdout, stderr)
        return code


RECIPE_TYPES: dict[str, type[Recipe]] = {"http": HttpRecipe, "command": CommandRecipe}


def build_recipe(data: Mapping[str, object], name: str, config: Config | None = None) -> Recipe | None:
    """Validate and compile config, or compile an already-validated run snapshot."""
    kind = data.get("kind")
    recipe_type = RECIPE_TYPES.get(kind) if isinstance(kind, str) else None
    if recipe_type is None:
        if config is not None:
            config.shared.error(f"qa.{name}.kind", "expected http or command")
        return None
    if config is not None:
        previous_errors = len(config.errors)
        recipe_type.validate(data, name, config)
        if len(config.errors) != previous_errors:
            return None
    return recipe_type(data, name)


