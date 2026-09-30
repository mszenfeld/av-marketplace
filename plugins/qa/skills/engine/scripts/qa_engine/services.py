"""Trusted source resolution, masked process logs and run-bound services."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from http.client import HTTPException
import json
import os
import re
import signal
import subprocess
from tempfile import TemporaryFile
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler
from urllib.request import ProxyHandler
from urllib.request import Request
from urllib.request import build_opener

from av_config import ConfigError
from av_config import ValueSources
from av_config import atomic_write
from av_config import flatten
from av_config import is_loopback
from qa_engine.config import PLACEHOLDER
from qa_engine.config import Config
from qa_engine.config import mapping

if TYPE_CHECKING:
    from qa_engine.state import Run

SHELL_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?:[^}]*\})|([A-Za-z_][A-Za-z0-9_]*))")


class NoRedirect(HTTPRedirectHandler):
    """Keep credentials on the configured origin, even on a redirect response."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def http_request(request: Request) -> tuple[int, bytes, list[str]]:
    """Return status/body/cookies without following redirects or echoing payloads."""
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


def require_trust(config: Config) -> None:
    if config.errors or config.state != "ok" or config.trust not in {"trusted", "not-required"}:
        raise ConfigError("trust required")


class Runtime:
    """Resolve only executed dependencies and delay logs until outputs can be masked."""

    def __init__(self, run: Run, config: Config, *, cleanup: bool = False) -> None:
        if not cleanup:
            require_trust(config)
        self.run = run
        self.config = config
        self.cache: dict[str, str] = {}
        self.resolving: set[str] = set()
        self.hidden: set[str] = {value for value in os.environ.values() if value}
        self.logs: list[tuple[str, str]] = []
        self.sources = ValueSources(run.repo, plugin_prefix="QA_")
        self.remember(flatten(config.data))
        for value in flatten(config.data).values():
            if isinstance(value, str) and value.startswith("literal:"):
                self.hidden.add(value[8:])
        for name in ("accounts.private.json", "secrets.json"):
            private = run.directory / name
            if private.exists():
                try:
                    value = json.loads(private.read_text())
                except (OSError, UnicodeError, ValueError) as error:
                    raise ConfigError(f"{name}: private state unavailable") from error
                self.remember(value)

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.logs:
            return
        path = self.run.directory / "engine.log"
        previous = path.read_text() if path.exists() else ""
        hidden = sorted(self.hidden, key=len, reverse=True)
        entries: list[str] = []
        for label, output in self.logs:
            for value in hidden:
                if value:
                    output = output.replace(value, "***")
            entries.append(f"{label}: {output[-8192:]}\n")
        text = "".join(entries)
        atomic_write(path, (previous + text).encode(), 0o600)

    def remember(self, value: object) -> None:
        if isinstance(value, str) and value:
            self.hidden.add(value)
            self.hidden.add(json.dumps(value, ensure_ascii=False)[1:-1])
        elif isinstance(value, Mapping):
            for item in value.values():
                self.remember(item)
        elif isinstance(value, list):
            for item in value:
                self.remember(item)

    def resolve(self, source: str, key: str) -> str:
        if key in self.cache:
            return self.cache[key]
        require_trust(self.config)
        local = self.config.provenance.get(key) == ".av/local.toml"
        secret = key.startswith("env.secrets.") or key.endswith(".password")
        literal_allowed = key == "env.database.password" and is_loopback(str(self.config.database.get("host", "")))
        problem = self.sources.validate(source, key, local=local, secret=secret, literal_allowed=literal_allowed)
        if problem:
            raise ConfigError(f"{key}: {problem}")
        if key in self.resolving:
            raise ConfigError(f"{key}: cyclic source dependency")
        self.resolving.add(key)
        try:
            if source.startswith("cmd:"):
                command = source[4:]
                environ = self.command_environment(command)
                code, stdout, stderr = self.shell(command, environ, key, timeout=30)
                if code:
                    self.logs.append((key, "***"))
                    raise ConfigError(f"{key}: command source failed")
                value = stdout.rstrip("\n")
                if not value:
                    self.logs.append((key, "***"))
                    raise ConfigError(f"{key}: empty cmd source")
                self.remember(value)
                self.logs.append((key, stdout + stderr))
            else:
                value = self.sources.resolve(source, key, trusted=True, local=local, secret=secret, literal_allowed=literal_allowed)
            self.cache[key] = value
            self.remember(value)
            return value
        finally:
            self.resolving.remove(key)

    def named(self, kind: str, name: str) -> str:
        table = self.config.values if kind == "value" else mapping(self.config.env.get("secrets"))
        key = f"env.{'values' if kind == 'value' else 'secrets'}.{name}"
        source = table.get(name)
        if not isinstance(source, str):
            raise ConfigError(f"{key}: source unavailable")
        return self.resolve(source, key)

    def substitute(self, value: object, identity: Mapping[str, object]) -> Any:
        if isinstance(value, str):
            def replacement(match: re.Match[str]) -> str:
                name = match.group(1)
                if name.startswith("secret."):
                    return self.named("secret", name[7:])
                if name.startswith("value."):
                    return self.named("value", name[6:])
                result = identity.get(name)
                if result is None or result == "":
                    raise ConfigError(f"recipe placeholder {name}: required output missing")
                return str(result)
            return PLACEHOLDER.sub(replacement, value)
        if isinstance(value, dict):
            return {key: self.substitute(item, identity) for key, item in value.items()}
        if isinstance(value, list):
            return [self.substitute(item, identity) for item in value]
        return value

    def command_environment(self, command: str) -> dict[str, str]:
        environ = dict(os.environ)
        names = {a or b for a, b in SHELL_VARIABLE.findall(command)}
        for name in self.config.values:
            if f"QA_{name.upper()}" in names:
                environ[f"QA_{name.upper()}"] = self.named("value", name)
        for name in mapping(self.config.env.get("secrets")):
            if f"AV_{name}" in names:
                environ[f"AV_{name}"] = self.named("secret", name)
        return environ

    def shell(self, command: str, environ: Mapping[str, str], label: str, *,
              timeout: float, file_output: bool = False) -> tuple[int, str, str]:
        """Bound source calls separately; service children must not hold pipes open."""
        with ExitStack() as stack:
            output = stack.enter_context(TemporaryFile(dir=self.run.directory)) if file_output else None
            try:
                process = subprocess.Popen(["/bin/sh", "-c", command], cwd=self.run.repo, env=dict(environ),
                                           stdout=output if output is not None else subprocess.PIPE,
                                           stderr=subprocess.STDOUT if output is not None else subprocess.PIPE,
                                           start_new_session=True)
            except OSError as error:
                raise ConfigError(f"{label}: command unavailable") from error
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                self.logs.append((label, "***"))
                raise ConfigError(f"{label}: command timed out") from error
            if output is not None:
                output.seek(0)
                # Mask complete values in __exit__ before taking the log's tail.
                stdout, stderr = output.read(), b""
            try:
                return process.returncode, (stdout or b"").decode(), (stderr or b"").decode()
            except UnicodeError as error:
                raise ConfigError(f"{label}: invalid command output") from error


def services(run: Run, config: Config, operation: str) -> dict[str, object]:
    """Run checks/bring-up against the pinned config; down ignores current drift."""
    if operation != "down":
        require_trust(config)
    recorded = run.record["config"]["env"]
    settings = mapping(recorded.get("services"))
    if operation == "check":
        probes: list[dict[str, object]] = []
        for probe in cast(list[str], settings.get("health", [])):
            target, path = probe.split(":", 1)
            try:
                status, _, _ = http_request(Request(recorded["targets"][target] + path))
            except ConfigError:
                status = None
            probes.append({"target": target, "path": path, "status": status})
        return {"up": all(isinstance(p["status"], int) and p["status"] < 500 for p in probes), "probes": probes}
    if operation == "down" and not run.record.get("services_up"):
        return {"ran": False, "exit": None}
    configured = settings.get(operation, [])
    commands = cast(list[str], configured) if isinstance(configured, list) else [cast(str, configured)]
    if not commands:
        return {"ran": False, "exit": None}
    with Runtime(run, config, cleanup=operation == "down") as runtime:
        code = 0
        for command in commands:
            # Teardown runs the already-trusted command even after config repair.
            environ = dict(os.environ) if operation == "down" else runtime.command_environment(command)
            if operation == "up":
                run.record["services_up"] = True
                atomic_write(run.directory / "run.json", json.dumps(run.record).encode(), 0o600)
            code, stdout, stderr = runtime.shell(command, environ, f"services {operation}",
                                                 timeout=float(run.budget["minutes"]) * 60, file_output=True)
            runtime.logs.append((f"services {operation}", stdout + stderr))
            if code:
                raise ConfigError(f"services {operation}: command failed (exit {code})")
        return {"ran": True, "exit": code}
