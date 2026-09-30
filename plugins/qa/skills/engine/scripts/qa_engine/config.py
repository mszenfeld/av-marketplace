"""QA's schema, capabilities, trust subset, and safe CLI display."""
from __future__ import annotations

from collections.abc import Mapping
import math
from pathlib import Path
import re
from typing import cast

from av_config import InvalidConfig
from av_config import ConfigTransaction
from av_config import Configuration
from av_config import TrustStore
from av_config import canonical_hash
from av_config import flatten
from av_config import mask
from av_config import source_subset

PERSONA = re.compile(r"[a-z][a-z0-9_]*\Z")
PLACEHOLDER = re.compile(r"(?<!\$)\{([^{}]+)\}")
EXTRACTOR = re.compile(r"\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?)*\Z")
POLICY = {"fix": "approve", "mutations": "rejections-only", "disposable_data": False, "min_severity": "LOW", "dirty_tree": "ask"}
BUDGET = {"iterations": 3, "dispatches": 50, "minutes": 30}
DATABASE_NAMES = {
    "postgres": ["PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD"],
    "mysql": ["MYSQL_HOST", "MYSQL_TCP_PORT", "MYSQL_USER", "MYSQL_DATABASE", "MYSQL_PWD"],
    "sqlite": ["SQLITE_DB"],
}


def cookie_name(name: str) -> str:
    """Normalize a cookie name for its tester environment variable."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


class Config:
    """Effective QA configuration. Construction validates when the shared file exists.

    ``data`` retains the effective unmasked config for engine consumers;
    ``report()`` is the only tester/orchestrator display surface. ``accept``
    always reloads from disk so a stale object cannot approve a changed file.
    """

    def __init__(self, repo: Path, *, state_home: Path | None = None, config_text: str | None = None, gitignore_text: str | None = None) -> None:
        self.repo = repo.resolve()
        self.state_home = state_home
        self.shared = Configuration(self.repo, plugin_prefix="QA_", config_text=config_text, gitignore_text=gitignore_text)
        self.data = self.shared.data
        self.errors = self.shared.errors
        self.warnings = self.shared.warnings
        self.provenance = self.shared.provenance
        self.store = TrustStore(self.repo, "qa", state_home)
        self.qa = mapping(self.data.get("qa"))
        self.env = mapping(self.data.get("env"))
        self.targets = cast(dict[str, str], mapping(self.env.get("targets")))
        self.accounts = mapping(self.qa.get("accounts"))
        self.defaults = mapping(self.qa.get("defaults"))
        self.policy = {**POLICY, **mapping(self.qa.get("policy"))}
        self.budget = {**BUDGET, **mapping(self.qa.get("budget"))}
        self.services = mapping(self.env.get("services"))
        self.values = mapping(self.env.get("values"))
        self.database = mapping(self.env.get("database"))
        configured = self.accounts.get("personas", [])
        self.personas = [name for name in configured if isinstance(name, str)] if isinstance(configured, list) else []
        self.static = mapping(self.accounts.get("static"))
        self.static_personas = sorted(self.static)
        if self.shared.exists and "qa" in self.data:
            self._validate()
        self.trust_subset = self._sensitive_subset() if not self.errors else {}
        self.trust_hash = canonical_hash(self.trust_subset)
        self.trust = self.store.status(self.trust_subset)

    @property
    def state(self) -> str:
        return self.shared.state("qa")

    def _validate(self) -> None:
        validator = self.shared
        qa = validator.table(self.data["qa"], "qa")
        validator.keys(qa, {"defaults", "policy", "budget", "accounts"}, "qa")
        for name in ("defaults", "policy", "budget", "accounts"):
            if name in qa:
                validator.table(qa[name], f"qa.{name}")
        validator.keys(self.defaults, {"be_target", "fe_target"}, "qa.defaults")
        for key, target in self.defaults.items():
            if not isinstance(target, str) or target not in self.targets:
                validator.error(f"qa.defaults.{key}", "undefined target")
        policy = mapping(qa.get("policy"))
        validator.keys(policy, set(POLICY), "qa.policy")
        choices = {"fix": ("approve", "auto", "off"), "mutations": ("allow", "rejections-only", "deny"), "dirty_tree": ("ask", "allow", "abort"), "min_severity": ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
        for key, allowed in choices.items():
            if key in policy and policy[key] not in allowed:
                validator.error(f"qa.policy.{key}", "unsupported policy value")
        if "disposable_data" in policy and type(policy["disposable_data"]) is not bool:
            validator.error("qa.policy.disposable_data", "expected a boolean")
        if self.policy["mutations"] == "allow" and self.policy["disposable_data"] is not True:
            validator.error("qa.policy.disposable_data", "must be true when mutations are allowed")
        budget = mapping(qa.get("budget"))
        validator.keys(budget, set(BUDGET), "qa.budget")
        for key, value in budget.items():
            if type(value) is not int or cast(int, value) <= 0:
                validator.error(f"qa.budget.{key}", "expected a positive integer")
        self._validate_accounts()
        self._validate_names()

    def _validate_accounts(self) -> None:
        validator = self.shared
        validator.keys(self.accounts, {"personas", "email", "password", "create", "confirm", "login", "delete", "static"}, "qa.accounts")
        if "personas" in self.accounts:
            raw = self.accounts["personas"]
            if not isinstance(raw, list) or any(not isinstance(name, str) or not PERSONA.fullmatch(name) for name in raw) or len(set(self.personas)) != len(self.personas):
                validator.error("qa.accounts.personas", "expected distinct lower-case persona names")
        if "email" in self.accounts:
            template = self.accounts["email"]
            if not isinstance(template, str) or not template:
                validator.error("qa.accounts.email", "expected an email template")
            elif any(name not in {"run", "persona"} for name in PLACEHOLDER.findall(template)):
                validator.error("qa.accounts.email", "unsupported email placeholder")
        if "password" in self.accounts and self.accounts["password"] != "generate":
            validator.source(self.accounts["password"], "qa.accounts.password")
        if "static" in self.accounts:
            validator.table(self.accounts["static"], "qa.accounts.static")
        for persona, value in self.static.items():
            prefix = f"qa.accounts.static.{persona}"
            if not PERSONA.fullmatch(persona):
                validator.error(prefix, "invalid persona name")
            account = validator.table(value, prefix)
            validator.keys(account, {"email", "password", "id"}, prefix)
            for key in ("email", "password"):
                if key not in account:
                    validator.error(f"{prefix}.{key}", "required account source")
                else:
                    validator.source(account[key], f"{prefix}.{key}", secret=key == "password")
            if "id" in account:
                validator.source(account["id"], f"{prefix}.id")
        for name in ("create", "confirm", "login", "delete"):
            if name in self.accounts:
                recipe = validator.table(self.accounts[name], f"qa.accounts.{name}")
                self._validate_recipe(recipe, name)

    def _validate_recipe(self, recipe: Mapping[str, object], name: str) -> None:
        validator = self.shared
        prefix = f"qa.accounts.{name}"
        kind = recipe.get("kind")
        if kind == "http":
            validator.keys(recipe, {"kind", "target", "method", "path", "headers", "json", "form", "expect", "conflict", "id", "token", "cookies"}, prefix)
            target = recipe.get("target")
            if not isinstance(target, str) or target not in self.targets:
                validator.error(f"{prefix}.target", "undefined target")
            if recipe.get("method") not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                validator.error(f"{prefix}.method", "expected an HTTP method")
            path = recipe.get("path")
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path or "#" in path:
                validator.error(f"{prefix}.path", "expected a target-relative request path")
            for key in ("expect", "conflict"):
                if key not in recipe and key == "conflict":
                    continue
                codes = recipe.get(key)
                if not isinstance(codes, list) or not codes or any(type(code) is not int or not 100 <= cast(int, code) <= 599 for code in codes):
                    validator.error(f"{prefix}.{key}", "expected HTTP status codes")
            for key in ("headers", "form", "json"):
                if key in recipe:
                    body = validator.table(recipe[key], f"{prefix}.{key}")
                    if key in {"headers", "form"}:
                        for field, value in body.items():
                            if not isinstance(value, str):
                                validator.error(f"{prefix}.{key}.{field}", "expected a string")
                    else:
                        self._validate_json(body, f"{prefix}.{key}")
            if "json" in recipe and "form" in recipe:
                validator.error(prefix, "json and form are mutually exclusive")
            for key in ("id", "token"):
                extractor = recipe.get(key)
                if key in recipe and (not isinstance(extractor, str) or not EXTRACTOR.fullmatch(extractor)):
                    validator.error(f"{prefix}.{key}", "expected a dotted JSON extractor")
            if "cookies" in recipe:
                self._validate_cookies(recipe["cookies"], f"{prefix}.cookies")
            for key, item in flatten(recipe, prefix).items():
                self._validate_placeholders(item, key, name)
        elif kind == "command":
            validator.keys(recipe, {"kind", "run", "outputs"}, prefix)
            if not isinstance(recipe.get("run"), str) or not recipe.get("run"):
                validator.error(f"{prefix}.run", "expected a non-empty command")
            outputs = recipe.get("outputs", {})
            if isinstance(outputs, list):
                if any(item not in ("id", "token") for item in outputs):
                    validator.error(f"{prefix}.outputs", "unsupported command output")
            elif isinstance(outputs, dict):
                validator.keys(outputs, {"id", "token", "cookies"}, f"{prefix}.outputs")
                for key in ("id", "token"):
                    if key in outputs and outputs[key] is not True:
                        validator.error(f"{prefix}.outputs.{key}", "expected true")
                if "cookies" in outputs:
                    self._validate_cookies(outputs["cookies"], f"{prefix}.outputs.cookies")
            else:
                validator.error(f"{prefix}.outputs", "expected an output declaration")
        else:
            validator.error(f"{prefix}.kind", "expected http or command")

    def _validate_json(self, value: object, key: str) -> None:
        if isinstance(value, dict):
            for field, item in value.items():
                self._validate_json(item, f"{key}.{field}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._validate_json(item, f"{key}.{index}")
        elif not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value):
            self.shared.error(key, "expected a JSON-compatible value")

    def _validate_placeholders(self, value: object, key: str, recipe: str) -> None:
        if isinstance(value, list):
            for item in value:
                self._validate_placeholders(item, key, recipe)
        elif isinstance(value, dict):
            for field, item in value.items():
                self._validate_placeholders(item, f"{key}.{field}", recipe)
        if not isinstance(value, str):
            return
        for placeholder in PLACEHOLDER.findall(value):
            if placeholder == "password" and recipe == "delete":
                self.shared.error(key, "delete cannot use a password placeholder")
            elif placeholder.startswith("secret."):
                if placeholder[7:] not in mapping(self.env.get("secrets")):
                    self.shared.error(key, "placeholder names an undefined secret")
            elif placeholder.startswith("value."):
                if placeholder[6:] not in self.values:
                    self.shared.error(key, "placeholder names an undefined value")
            elif placeholder not in {"email", "password", "persona", "run", "id"}:
                self.shared.error(key, "unsupported recipe placeholder")

    def _validate_cookies(self, cookies: object, key: str) -> None:
        if not isinstance(cookies, list) or not cookies or any(not isinstance(cookie, str) or not cookie or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", cookie) or not cookie_name(cookie) for cookie in cookies):
            self.shared.error(key, "expected cookie names")
            return
        normalized = [cookie_name(cookie) for cookie in cookies]
        if len(normalized) != len(set(normalized)):
            self.shared.error(key, "cookie names collide after normalization")

    def persona_fields(self, persona: str) -> set[str]:
        """Return the fields this persona can actually supply to a tester."""
        if persona not in self.personas and persona not in self.static:
            return set()
        fields = {"EMAIL", "PASSWORD"}
        create = mapping(self.accounts.get("create"))
        static = mapping(self.static.get(persona))
        outputs = create.get("outputs", {})
        if (persona in self.static and "id" in static) or (persona not in self.static and ("id" in create or isinstance(outputs, (dict, list)) and "id" in outputs)):
            fields.add("ID")
        login = mapping(self.accounts.get("login"))
        outputs = login.get("outputs", {})
        if "token" in login or isinstance(outputs, (dict, list)) and "token" in outputs:
            fields.add("TOKEN")
        cookies = login.get("cookies", mapping(outputs).get("cookies", []))
        if isinstance(cookies, list) and cookies:
            fields.add("COOKIE")
            fields.update(f"COOKIE_{cookie_name(cookie)}" for cookie in cookies if isinstance(cookie, str))
        return fields

    def _validate_names(self) -> None:
        seen: set[str] = set()
        for persona in sorted(set(self.personas) | self.static.keys()):
            upper = persona.upper()
            if upper in seen:
                self.shared.error("qa.accounts.personas", "persona names collide after upper-casing")
            seen.add(upper)
        exposed: set[str] = set()
        for persona in sorted(set(self.personas) | self.static.keys()):
            for field in {"EMAIL", "PASSWORD", "ID", "TOKEN", "COOKIE"} | self.persona_fields(persona):
                name = f"{persona.upper()}_{field}"
                if name in exposed:
                    self.shared.error("qa.accounts.personas", "persona field names collide")
                exposed.add(name)
        for name in self.values:
            cookie_field = any(name.upper().startswith(f"{persona.upper()}_COOKIE_") for persona in set(self.personas) | self.static.keys())
            if name.upper() in exposed or cookie_field:
                self.shared.error(f"env.values.{name}", "value name collides with an exposed name")
            exposed.add(name.upper())

    def _sensitive_subset(self) -> dict[str, object]:
        subset = self.shared.env_sensitive_subset()
        subset.update(source_subset(self.qa, "qa"))
        for recipe in ("create", "confirm", "login", "delete"):
            if recipe in self.accounts:
                subset[f"qa.accounts.{recipe}"] = self.accounts[recipe]
        policy = mapping(self.qa.get("policy"))
        for key in ("fix", "dirty_tree", "mutations", "disposable_data"):
            if key in policy:
                subset[f"qa.policy.{key}"] = policy[key]
        return subset

    def report(self) -> dict[str, object]:
        """Return JSON-safe metadata, never source results or secret literals."""
        report: dict[str, object] = {"state": self.state, "errors": self.errors, "warnings": self.warnings, "provenance": self.provenance, "trust": self.trust, "trust_hash": self.trust_hash, "trust_subset": mask(self.trust_subset)}
        if self.errors:
            return report
        exposed = [f"QA_{persona.upper()}_{field}" for persona in sorted(set(self.personas) | self.static.keys()) for field in sorted(self.persona_fields(persona))]
        exposed.extend(f"QA_{name.upper()}" for name in self.values)
        exposed.extend(DATABASE_NAMES.get(cast(str, self.database.get("kind")), []))
        report.update({"targets": self.targets, "defaults": self.defaults, "policy": self.policy, "budget": self.budget, "services": mask({"health": [], "up": None, "prepare": [], "down": None, **self.services}), "personas": self.personas, "static_personas": self.static_personas, "values": sorted(self.values), "exposed": sorted(exposed), "database": mask(self.database) if self.database else None})
        return report

    def accept(self, approved_hash: str) -> dict[str, object]:
        def current() -> Mapping[str, object]:
            config = Config(self.repo, state_home=self.state_home)
            if config.errors:
                raise InvalidConfig("invalid config cannot be trusted")
            return config.trust_subset
        self.store.accept(approved_hash, current)
        return {"trusted": approved_hash}

    def _transaction(self) -> ConfigTransaction:
        def validate(text: str, ignore: str) -> dict[str, object]:
            return Config(self.repo, state_home=self.state_home, config_text=text, gitignore_text=ignore).report()
        return ConfigTransaction(self.repo, validate, self.accept)

    def preview(self, proposal: Mapping[str, object]) -> dict[str, object]:
        return self._transaction().preview(proposal)

    def apply(self, proposal: Mapping[str, object], snapshot: str, approved_hash: str) -> dict[str, object]:
        return self._transaction().apply(proposal, snapshot, approved_hash)
