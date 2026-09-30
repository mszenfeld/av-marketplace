"""Marketplace configuration, source restrictions, trust pins, and guarded writes.

This module is plugin-independent. Consumers validate their own table and supply
its environment prefix and trust subset; loading never resolves a value source.
"""
from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from difflib import unified_diff
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import tomllib
from typing import cast
from urllib.parse import urlsplit

Origin = tuple[str, str, int]
Diagnostic = dict[str, str]
SOURCE_KINDS = ("cmd:", "env:", "file:", "literal:")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TABLE = re.compile(r"^\s*(\[\[?[^\]\n]+\]\]?)\s*(?:#.*)?$")


class ConfigError(ValueError):
    """A safe, consumer-facing configuration or transaction error."""


class InvalidConfig(ConfigError):
    """Invalid configuration or transaction input (CLI usage exit 2)."""



def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def mask(value: object) -> object:
    """Hide literal sources recursively, without changing the pinned input."""
    if isinstance(value, str):
        return "literal:***" if value.startswith("literal:") else value
    if isinstance(value, dict):
        return {key: mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask(item) for item in value]
    return value


def flatten(value: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
            result.update(flatten(item, path))
        else:
            result[path] = item
    return result


def source_subset(value: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}

    def visit(item: object, key: str) -> None:
        if isinstance(item, str) and item.startswith(SOURCE_KINDS):
            result[key] = item
        elif isinstance(item, dict):
            for name, child in item.items():
                visit(child, f"{key}.{name}" if key else name)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{key}.{index}")

    visit(value, prefix)
    return result


def parse_origin(url: str, *, origin_only: bool = False) -> Origin:
    """Return the exact HTTP origin, rejecting credentials and malformed URLs.

    ``origin_only`` also rejects paths (including a trailing slash), query and
    fragment; the default accepts resource URLs for consumer origin guards.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ConfigError("invalid origin") from error
    if (
        parts.scheme not in {"http", "https"} or not host or not parts.netloc
        or parts.username is not None or parts.password is not None
        or any(character.isspace() or ord(character) < 32 for character in url)
        or "\\" in url or "%" in host
        or (origin_only and (parts.path or "?" in url or "#" in url))
        or port == 0 or parts.netloc.endswith(":")
    ):
        raise ConfigError("invalid origin")
    return parts.scheme.lower(), host.lower(), port if port is not None else (443 if parts.scheme == "https" else 80)


def is_loopback(host: str) -> bool:
    normalized = host.lower().removeprefix("[").removesuffix("]")
    return normalized in {"localhost", "127.0.0.1", "::1"} or normalized.endswith(".localhost")


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError("configuration file is not readable") from error


def atomic_write(path: Path, content: bytes, mode: int | None = None) -> None:
    """Replace a file atomically, preserving permissions unless specified."""
    path.parent.mkdir(parents=True, exist_ok=True)
    permissions = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o644)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(permissions)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ValueSources:
    """Validate sources without resolving them; require trust before resolution."""

    def __init__(self, repo: Path, *, plugin_prefix: str, environ: Mapping[str, str] | None = None, timeout: float = 30, gitignore_text: str | None = None) -> None:
        self.repo = repo.resolve()
        self.plugin_prefix = plugin_prefix
        self.environ = dict(os.environ if environ is None else environ)
        self.timeout = timeout
        self.gitignore_text = gitignore_text

    def ignored(self, path: Path) -> bool:
        relative = path.resolve().relative_to(self.repo)
        if self.gitignore_text is None:
            result = subprocess.run(["git", "check-ignore", "-q", "--", str(relative)], cwd=self.repo, capture_output=True, check=False)
            return result.returncode == 0
        # Ask git itself to interpret the virtual ignore file: its negation,
        # anchoring, directory and wildcard rules are not shell glob rules.
        gitdir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=self.repo, capture_output=True, text=True, check=False)
        if gitdir.returncode != 0:
            return False
        with tempfile.TemporaryDirectory(prefix="av-ignore-") as temporary:
            worktree = Path(temporary)
            (worktree / ".gitignore").write_text(self.gitignore_text)
            parent = relative.parent
            for ancestor in (*reversed(parent.parents), parent):
                if ancestor == Path("."):
                    continue
                original = self.repo / ancestor / ".gitignore"
                if original.is_file():
                    destination = worktree / ancestor / ".gitignore"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(original.read_bytes())
            result = subprocess.run(["git", f"--git-dir={gitdir.stdout.strip()}", f"--work-tree={worktree}", "check-ignore", "-q", "--", str(relative)], cwd=worktree, capture_output=True, check=False)
            return result.returncode == 0

    def validate(self, source: object, key: str, *, local: bool = False, secret: bool = False, literal_allowed: bool = False) -> str | None:
        if not isinstance(source, str) or not source.startswith(SOURCE_KINDS):
            return "expected a value source"
        kind, payload = source.split(":", 1)
        if not payload:
            return "empty value source"
        if kind == "env":
            if not NAME.fullmatch(payload):
                return "invalid environment source name"
            prefixes = ("AV_", self.plugin_prefix) if not key.startswith("env.") else ("AV_",)
            if not local and not payload.startswith(prefixes):
                return "environment source name has a forbidden prefix"
        if kind == "file":
            filename, separator, name = payload.rpartition("#")
            if not separator or not filename or not NAME.fullmatch(name):
                return "expected file source with a key"
            path = Path(filename)
            if path.is_absolute():
                if not local:
                    return "absolute file source is forbidden in shared config"
            else:
                path = self.repo / path
                if not path.resolve().is_relative_to(self.repo):
                    return "file source escapes the repository"
                if not self.ignored(path):
                    return "repository file source must be git-ignored"
        if kind == "literal" and secret and not local and not literal_allowed:
            return "secret literal is forbidden in shared config"
        return None

    def resolve(self, source: str, key: str, *, trusted: bool, local: bool = False, secret: bool = False, literal_allowed: bool = False) -> str:
        """Resolve one validated source; errors contain keys and kinds, not data."""
        if not trusted:
            raise ConfigError(f"{key}: trust required")
        problem = self.validate(source, key, local=local, secret=secret, literal_allowed=literal_allowed)
        if problem:
            raise ConfigError(f"{key}: {problem}")
        kind, payload = source.split(":", 1)
        if kind == "literal":
            result = payload
        elif kind == "env":
            result = self.environ.get(payload, "")
        elif kind == "file":
            filename, _, name = payload.rpartition("#")
            path = Path(filename)
            if not path.is_absolute():
                path = self.repo / path
            try:
                text = path.read_text()
            except (OSError, UnicodeError) as error:
                raise ConfigError(f"{key}: file source unavailable") from error
            result = ""
            for line in text.splitlines():
                match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
                if match and match.group(1) == name:
                    raw = match.group(2).strip()
                    if raw.startswith(('"', "'")):
                        quote = raw[0]
                        end = raw.rfind(quote)
                        if end == 0 or (raw[end + 1:].strip() and not raw[end + 1:].lstrip().startswith("#")):
                            raise ConfigError(f"{key}: invalid file source")
                        result = raw[1:end]
                    else:
                        result = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        else:
            try:
                process = subprocess.Popen(["/bin/sh", "-c", payload], cwd=self.repo, env=self.environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            except OSError as error:
                raise ConfigError(f"{key}: command source unavailable") from error
            try:
                stdout, _ = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired as error:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise ConfigError(f"{key}: command source timed out") from error
            if process.returncode:
                raise ConfigError(f"{key}: command source failed")
            try:
                result = stdout.decode().rstrip("\n")
            except UnicodeError as error:
                raise ConfigError(f"{key}: invalid command source output") from error
        if not result:
            raise ConfigError(f"{key}: empty {kind} source")
        return result


class Configuration:
    """Load the marketplace files; plugin presence belongs to the shared file."""

    def __init__(self, repo: Path, *, plugin_prefix: str, config_text: str | None = None, gitignore_text: str | None = None) -> None:
        self.repo = repo.resolve()
        self.data: dict[str, object] = {}
        self._shared_tables: set[str] = set()
        self.provenance: dict[str, str] = {}
        self.errors: list[Diagnostic] = []
        self.warnings: list[Diagnostic] = []
        self.exists = False
        self.sources = ValueSources(self.repo, plugin_prefix=plugin_prefix, gitignore_text=gitignore_text)
        for name in (".av/config.toml", ".av/local.toml"):
            if name == ".av/config.toml" and config_text is not None:
                raw = config_text
                self.exists = True
            else:
                content = read_bytes(self.repo / name)
                if content is None:
                    continue
                if name == ".av/config.toml":
                    self.exists = True
                try:
                    raw = content.decode()
                except UnicodeError:
                    self.errors.append({"file": name, "key": "document", "error": "invalid text encoding"})
                    continue
            try:
                parsed = tomllib.loads(raw)
            except tomllib.TOMLDecodeError:
                self.errors.append({"file": name, "key": "document", "error": "invalid TOML"})
                continue
            if name == ".av/config.toml":
                self._shared_tables = {key for key, value in parsed.items() if isinstance(value, dict)}
            self._merge(self.data, parsed, name)
        if self.exists:
            self._validate_env()

    def _merge(self, destination: dict[str, object], source: Mapping[str, object], filename: str, prefix: str = "") -> None:
        for key, item in source.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                existing = destination.get(key)
                if not isinstance(existing, dict):
                    existing = {}
                    destination[key] = existing
                    for old in list(self.provenance):
                        if old.startswith(path + "."):
                            del self.provenance[old]
                self.provenance[path] = filename
                self._merge(existing, item, filename, path)
            else:
                destination[key] = item
                for old in list(self.provenance):
                    if old.startswith(path + "."):
                        del self.provenance[old]
                self.provenance[path] = filename

    def file_for(self, key: str) -> str:
        path = key
        while path:
            if path in self.provenance:
                return self.provenance[path]
            path = path.rpartition(".")[0]
        return ".av/config.toml"

    def error(self, key: str, message: str) -> None:
        self.errors.append({"file": self.file_for(key), "key": key, "error": message})

    def table(self, value: object, key: str) -> dict[str, object]:
        if not isinstance(value, dict):
            self.error(key, "expected a table")
            return {}
        return value

    def keys(self, table: Mapping[str, object], allowed: set[str], prefix: str) -> None:
        for key in table.keys() - allowed:
            self.error(f"{prefix}.{key}", "unknown key")

    def source(self, value: object, key: str, *, secret: bool = False, literal_allowed: bool = False) -> None:
        problem = self.sources.validate(value, key, local=self.file_for(key) == ".av/local.toml", secret=secret, literal_allowed=literal_allowed)
        if problem:
            self.error(key, problem)

    def _validate_env(self) -> None:
        if type(self.data.get("version")) is not int or self.data.get("version") != 1:
            self.error("version", "expected schema version 1")
        env = self.table(self.data.get("env", {}), "env")
        for name, value in env.items():
            prefix = f"env.{name}"
            if name not in {"targets", "services", "secrets", "values", "database"}:
                self.warnings.append({"file": self.file_for(prefix), "key": prefix, "warning": "unknown environment sub-table"})
                continue
            table = self.table(value, prefix)
            if name == "targets":
                for target, origin in table.items():
                    try:
                        if not NAME.fullmatch(target) or not isinstance(origin, str):
                            raise ConfigError("invalid target")
                        parse_origin(origin, origin_only=True)
                    except ConfigError:
                        self.error(f"{prefix}.{target}", "expected an HTTP origin without userinfo, path, query or fragment")
            elif name in {"secrets", "values"}:
                for key, source in table.items():
                    if not NAME.fullmatch(key):
                        self.error(f"{prefix}.{key}", "invalid value name")
                    self.source(source, f"{prefix}.{key}", secret=name == "secrets")
            elif name == "services":
                self.keys(table, {"health", "up", "prepare", "down"}, prefix)
                for key in ("up", "down"):
                    if key in table and (not isinstance(table[key], str) or not table[key]):
                        self.error(f"{prefix}.{key}", "expected a non-empty command")
                prepare = table.get("prepare", [])
                if not isinstance(prepare, list) or any(not isinstance(item, str) or not item for item in prepare):
                    self.error(f"{prefix}.prepare", "expected a command list")
                health = table.get("health", [])
                if not isinstance(health, list):
                    self.error(f"{prefix}.health", "expected a probe list")
                else:
                    targets = env.get("targets", {})
                    for probe in health:
                        if not isinstance(probe, str) or ":" not in probe:
                            self.error(f"{prefix}.health", "expected target:path probes")
                            continue
                        target, path = probe.split(":", 1)
                        if not isinstance(targets, dict) or target not in targets or not path.startswith("/") or path.startswith("//"):
                            self.error(f"{prefix}.health", "probe has undefined target or invalid path")
            else:
                self._validate_database(table)

    def _validate_database(self, table: Mapping[str, object]) -> None:
        prefix = "env.database"
        self.keys(table, {"kind", "host", "port", "user", "name", "password", "path"}, prefix)
        kind = table.get("kind")
        if kind not in ("postgres", "mysql", "sqlite"):
            self.error(f"{prefix}.kind", "expected postgres, mysql or sqlite")
            return
        required = ("path",) if kind == "sqlite" else ("host", "user", "name", "password")
        for key in required:
            if not isinstance(table.get(key), str) or not table.get(key):
                self.error(f"{prefix}.{key}", "expected a non-empty string")
        if "port" in table and (type(table["port"]) is not int or not 1 <= cast(int, table["port"]) <= 65535):
            self.error(f"{prefix}.port", "expected a port between 1 and 65535")
        if kind == "sqlite":
            for key in {"host", "port", "user", "name", "password"} & table.keys():
                self.error(f"{prefix}.{key}", "not supported for sqlite")
        else:
            if "path" in table:
                self.error(f"{prefix}.path", "only supported for sqlite")
            if "password" in table:
                host = table.get("host")
                self.source(table["password"], f"{prefix}.password", secret=True, literal_allowed=isinstance(host, str) and is_loopback(host))

    def state(self, plugin: str) -> str:
        if not self.exists:
            return "missing-file"
        if self.errors:
            return "invalid"
        return "ok" if plugin in self._shared_tables else "missing-table"

    def env_sensitive_subset(self) -> dict[str, object]:
        env = self.data.get("env", {})
        if not isinstance(env, dict):
            return {}
        subset = source_subset(env, "env")
        if env.get("services"):
            subset["env.services"] = env["services"]
        targets = env.get("targets", {})
        if isinstance(targets, dict):
            for name, origin in targets.items():
                if isinstance(origin, str):
                    try:
                        host = parse_origin(origin)[1]
                    except ConfigError:
                        continue
                    if not is_loopback(host):
                        subset[f"env.targets.{name}"] = origin
        database = env.get("database", {})
        if isinstance(database, dict) and isinstance(database.get("host"), str) and not is_loopback(database["host"]):
            subset["env.database.host"] = database["host"]
        return subset


class TrustStore:
    """Repository-realpath and plugin scoped pins, stored outside the checkout."""

    def __init__(self, repo: Path, plugin: str, state_home: Path | None = None) -> None:
        home = state_home or Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state"))
        self.path = home / "av-marketplace/trust.json"
        self.repo = str(repo.resolve())
        self.plugin = plugin

    def _read(self) -> dict[str, object]:
        raw = read_bytes(self.path)
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise ConfigError("invalid marketplace trust store") from error
        if not isinstance(data, dict):
            raise ConfigError("invalid marketplace trust store")
        return data

    def status(self, subset: Mapping[str, object]) -> str:
        if not subset:
            return "not-required"
        repositories = self._read()
        plugins = repositories.get(self.repo, {})
        previous = plugins.get(self.plugin) if isinstance(plugins, dict) else None
        if previous is None:
            return "new"
        return "trusted" if previous == canonical_hash(subset) else "changed"

    def accept(self, approved_hash: str, current_subset: Callable[[], Mapping[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / "trust.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if canonical_hash(current_subset()) != approved_hash:
                raise ConfigError("trust hash changed; preview the current configuration")
            data = self._read()
            plugins = data.get(self.repo)
            if not isinstance(plugins, dict):
                plugins = {}
                data[self.repo] = plugins
            plugins[self.plugin] = approved_hash
            atomic_write(self.path, json.dumps(data, sort_keys=True).encode(), 0o600)


def _fingerprint(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _key_paths(value: Mapping[str, object], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], object]:
    result: dict[tuple[str, ...], object] = {}
    for key, item in value.items():
        path = (*prefix, key)
        if isinstance(item, dict) and item:
            result.update(_key_paths(item, path))
        else:
            result[path] = item
    return result


def _permitted(key: tuple[str, ...], allowed: list[str]) -> bool:
    for prefix in allowed:
        parts = tuple(prefix.split("."))
        if key[:len(parts)] == parts:
            return True
    return False


def _protected_sections(text: str, allowed: list[str]) -> dict[tuple[str, ...], str]:
    """Keep non-whitespace TOML content and section comments byte-exact."""
    sections: dict[tuple[str, ...], list[str]] = {(): []}
    current: tuple[str, ...] = ()
    header = ""
    statement: list[str] = []
    comments: list[str] = []

    def finish_comments() -> None:
        if not _permitted(current, allowed):
            sections[current].extend(comments)
        comments.clear()

    def finish() -> None:
        if not statement:
            return
        chunk = "".join(statement)
        try:
            parsed = tomllib.loads(header + chunk)
        except tomllib.TOMLDecodeError:
            sections[current].append(chunk)
        else:
            paths = _key_paths(parsed)
            if not paths or not any(_permitted(path, allowed) for path in paths):
                sections[current].append(chunk)
        statement.clear()

    for line in text.splitlines(keepends=True):
        match = TABLE.fullmatch(line.rstrip("\r\n")) if not statement else None
        if match:
            header = match.group(1) + "\n"
            current = next(iter(_key_paths(tomllib.loads(header))))
            sections.setdefault(current, [])
            finish_comments()
            if not _permitted(current, allowed) and not any(tuple(key.split("."))[:len(current)] == current for key in allowed):
                sections[current].append(line)
            continue
        if not statement:
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                comments.append(line)
                continue
        finish_comments()
        statement.append(line)
        try:
            tomllib.loads(header + "".join(statement))
        except tomllib.TOMLDecodeError:
            continue
        finish()
    finish()
    finish_comments()
    return {key: "".join(lines) for key, lines in sections.items() if lines}


class ConfigTransaction:
    """Preview and compare-and-swap the shared config and root ignore file.

    A snapshot contains content hashes, never file contents. Originals read at
    apply time are rollback snapshots only after they match those hashes.
    """

    def __init__(self, repo: Path, validate: Callable[[str, str], dict[str, object]], accept: Callable[[str], object]) -> None:
        self.repo = repo.resolve()
        self.validate = validate
        self.accept = accept
        self.paths = (self.repo / ".av/config.toml", self.repo / ".gitignore")

    def _proposal(self, proposal: Mapping[str, object], original_ignore: bytes | None) -> tuple[str, str, list[str]]:
        if set(proposal) != {"config_text", "gitignore_add", "allowed_keys"}:
            raise InvalidConfig("proposal: expected config_text, gitignore_add and allowed_keys")
        text, additions, allowed = proposal["config_text"], proposal["gitignore_add"], proposal["allowed_keys"]
        if not isinstance(text, str) or not isinstance(additions, list) or not isinstance(allowed, list):
            raise InvalidConfig("proposal: invalid transaction types")
        if any(not isinstance(key, str) or not key or "\n" in key for key in allowed):
            raise InvalidConfig("proposal.allowed_keys: expected key paths")
        if any(not isinstance(line, str) or not line or any(char in line for char in "\r\n\x00") for line in additions):
            raise InvalidConfig("proposal.gitignore_add: expected single-line patterns")
        try:
            ignore = original_ignore.decode() if original_ignore is not None else ""
        except UnicodeError as error:
            raise ConfigError(".gitignore: invalid text encoding") from error
        for line in additions:
            if line not in ignore.splitlines():
                if ignore and not ignore.endswith("\n"):
                    ignore += "\n"
                ignore += line + "\n"
        return text, ignore, cast(list[str], allowed)

    def preview(self, proposal: Mapping[str, object]) -> dict[str, object]:
        originals = tuple(read_bytes(path) for path in self.paths)
        snapshot = json.dumps({"config": _fingerprint(originals[0]), "gitignore": _fingerprint(originals[1])}, separators=(",", ":"))
        text, ignore, allowed = self._proposal(proposal, originals[1])
        report = self.validate(text, ignore)
        errors = list(cast(list[Diagnostic], report.get("errors", [])))
        if errors:
            return {"ok": False, "errors": errors, "diff": [], "trust_subset": {}, "trust_hash": None, "snapshot": snapshot}
        try:
            original = originals[0].decode() if originals[0] is not None else ""
        except UnicodeError as error:
            raise ConfigError(".av/config.toml: invalid text encoding") from error
        try:
            old_data = tomllib.loads(original) if original else {}
            new_data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            errors.append({"file": ".av/config.toml", "key": "document", "error": "invalid original TOML"})
            return {"ok": False, "errors": errors, "diff": [], "trust_subset": {}, "trust_hash": None, "snapshot": snapshot}
        before_keys = {key: value for key, value in _key_paths(old_data).items() if not isinstance(value, dict)}
        after_keys = {key: value for key, value in _key_paths(new_data).items() if not isinstance(value, dict)}
        changed = {
            key for key in before_keys.keys() | after_keys.keys()
            if key not in before_keys or key not in after_keys
            or json.dumps(before_keys[key], sort_keys=True, default=str) != json.dumps(after_keys[key], sort_keys=True, default=str)
        }
        for key in changed:
            if not _permitted(key, allowed):
                errors.append({"file": ".av/config.toml", "key": ".".join(key), "error": "proposal changes an unapproved key"})
        protected_before = _protected_sections(original, allowed)
        protected_after = _protected_sections(text, allowed)
        if originals[0] is None:
            protected_after.pop((), None)
        if protected_before != protected_after:
            errors.append({"file": ".av/config.toml", "key": "allowed_keys", "error": "proposal changes an unapproved section, key or comment"})
        if errors:
            return {"ok": False, "errors": errors, "diff": [], "trust_subset": {}, "trust_hash": None, "snapshot": snapshot}
        # A semantic, masked diff avoids copying local literals or secret text
        # into stdout. Protected non-whitespace content was compared above.
        before = json.dumps(mask(old_data), indent=2, sort_keys=True, default=str).splitlines()
        after = json.dumps(mask(new_data), indent=2, sort_keys=True, default=str).splitlines()
        diff = "\n".join(unified_diff(before, after, fromfile=".av/config.toml", tofile="proposed .av/config.toml", lineterm=""))
        return {"ok": True, "errors": [], "diff": diff, "gitignore_add": proposal["gitignore_add"], "trust_subset": report["trust_subset"], "trust_hash": report["trust_hash"], "snapshot": snapshot}

    def apply(self, proposal: Mapping[str, object], snapshot: str, approved_hash: str) -> dict[str, object]:
        try:
            expected = json.loads(snapshot)
        except (ValueError, TypeError) as error:
            raise InvalidConfig("snapshot: invalid token") from error
        if not isinstance(expected, dict) or set(expected) != {"config", "gitignore"}:
            raise InvalidConfig("snapshot: invalid token")
        originals = tuple(read_bytes(path) for path in self.paths)
        if expected != {"config": _fingerprint(originals[0]), "gitignore": _fingerprint(originals[1])}:
            raise ConfigError("configuration conflict: files changed after preview")
        preview = self.preview(proposal)
        if not preview["ok"]:
            raise InvalidConfig("proposal: validation or section guard failed")
        if preview["snapshot"] != snapshot:
            raise ConfigError("configuration conflict: files changed after preview")
        text, ignore, _ = self._proposal(proposal, originals[1])
        written = (text.encode(), ignore.encode())
        applied: list[int] = []
        try:
            for index, path in enumerate(self.paths):
                if read_bytes(path) != originals[index]:
                    raise ConfigError("configuration conflict: files changed during apply")
                atomic_write(path, written[index])
                applied.append(index)
            actual_text = read_bytes(self.paths[0])
            actual_ignore = read_bytes(self.paths[1])
            if (actual_text, actual_ignore) != written:
                raise ConfigError("configuration conflict: files changed during apply")
            report = self.validate(text, ignore)
            if report.get("errors") or report.get("trust_hash") != approved_hash:
                raise ConfigError("post-write validation or approved hash check failed")
            self.accept(approved_hash)
        except (ConfigError, OSError) as error:
            conflicts = False
            for index in applied:
                path = self.paths[index]
                if read_bytes(path) != written[index]:
                    conflicts = True
                    continue
                if originals[index] is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, cast(bytes, originals[index]))
            if conflicts:
                raise ConfigError("configuration conflict: concurrent edits left untouched") from error
            if isinstance(error, ConfigError):
                raise
            raise ConfigError("configuration transaction failed") from error
        return {"applied": True}
