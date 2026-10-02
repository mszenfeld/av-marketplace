"""QA's schema, capabilities, trust subset, and safe CLI display."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from typing import cast

from av_config.errors import InvalidConfig
from av_config.transaction import ConfigTransaction
from av_config.files import Configuration
from av_config.trust import TrustStore
from av_config.trust import canonical_hash
from av_config.sources import mask
from av_config.sources import source_subset
from qa_engine.recipes import PLACEHOLDER
from qa_engine.recipes import Recipe
from qa_engine.recipes import build_recipe
from qa_engine.recipes import cookie_name

PERSONA = re.compile(r"[a-z][a-z0-9_]*\Z")
VALUE_SOURCE_KEYS = re.compile(r"(?:env\.(?:secrets|values)\.[A-Za-z_][A-Za-z0-9_]*|env\.database\.password|qa\.accounts\.password|qa\.accounts\.static\.[a-z][a-z0-9_]*\.(?:email|password|id))")
POLICY = {"fix": "approve", "mutations": "rejections-only"}
FIX = ("approve", "auto", "off")
MUTATIONS = ("allow", "rejections-only", "deny")
SECTION_TARGETS = {"FE": ("ui", "backend"), "BE": ("backend", "ui")}
DATABASE_NAMES = {
    "postgres": {"PGHOST": "host", "PGPORT": "port", "PGUSER": "user", "PGDATABASE": "name", "PGPASSWORD": "password"},
    "mysql": {"MYSQL_HOST": "host", "MYSQL_TCP_PORT": "port", "MYSQL_USER": "user", "MYSQL_DATABASE": "name", "MYSQL_PWD": "password"},
    "sqlite": {"SQLITE_DB": "path"},
}


def mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _persona_names(value: object) -> bool:
    if not isinstance(value, list):
        return False
    if not all(isinstance(name, str) and PERSONA.fullmatch(name) for name in value):
        return False
    return len(set(value)) == len(value)


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
        self.recipes: dict[str, Recipe] = {}
        self.policy = {**POLICY, **{key: self.qa[key] for key in POLICY if key in self.qa}}
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

    def section_target(self, section: str) -> str | None:
        """The origin name a section's relative paths use: its reserved name, the other one, or the only target."""
        for name in SECTION_TARGETS[section]:
            if name in self.targets:
                return name
        if len(self.targets) == 1:
            return next(iter(self.targets))
        return None

    def _validate(self) -> None:
        validator = self.shared
        qa = validator.table(self.data["qa"], "qa")
        validator.keys(qa, {"fix", "mutations", "accounts"}, "qa")
        if "accounts" in qa:
            validator.table(qa["accounts"], "qa.accounts")
        for key, allowed in (("fix", FIX), ("mutations", MUTATIONS)):
            if key in qa and qa[key] not in allowed:
                validator.error(f"qa.{key}", "unsupported policy value")
        self._validate_accounts()
        self._validate_names()

    def _validate_accounts(self) -> None:
        validator = self.shared
        validator.keys(self.accounts, {"personas", "email", "password", "create", "confirm", "login", "delete", "static"}, "qa.accounts")
        self._validate_account_templates()
        if "static" in self.accounts:
            validator.table(self.accounts["static"], "qa.accounts.static")
        for persona, value in self.static.items():
            self._validate_static_account(persona, value)
        for name in ("create", "confirm", "login", "delete"):
            if name in self.accounts:
                data = validator.table(self.accounts[name], f"qa.accounts.{name}")
                recipe = build_recipe(data, name, self)
                if recipe is not None:
                    self.recipes[name] = recipe

    def _validate_account_templates(self) -> None:
        validator = self.shared
        if "personas" in self.accounts and not _persona_names(self.accounts["personas"]):
            validator.error("qa.accounts.personas", "expected distinct lower-case persona names")
        if "email" in self.accounts:
            template = self.accounts["email"]
            if not isinstance(template, str) or not template:
                validator.error("qa.accounts.email", "expected an email template")
            elif any(name not in {"run", "persona"} for name in PLACEHOLDER.findall(template)):
                validator.error("qa.accounts.email", "unsupported email placeholder")
        if "password" in self.accounts and self.accounts["password"] != "generate":
            validator.source(self.accounts["password"], "qa.accounts.password")

    def _validate_static_account(self, persona: str, value: object) -> None:
        validator = self.shared
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

    def persona_fields(self, persona: str) -> set[str]:
        """Return the fields this persona can actually supply to a tester."""
        if persona not in self.personas and persona not in self.static:
            return set()
        fields = {"EMAIL", "PASSWORD"}
        create = self.recipes.get("create")
        static = mapping(self.static.get(persona))
        if (persona in self.static and "id" in static) or (persona not in self.static and create is not None and "id" in create.outputs):
            fields.add("ID")
        login = self.recipes.get("login")
        outputs = login.outputs if login is not None else {}
        if "token" in outputs:
            fields.add("TOKEN")
        cookies = outputs.get("cookies", [])
        if cookies:
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
        for key in POLICY:
            if key in self.qa:
                subset[f"qa.{key}"] = self.qa[key]
        return subset

    def report(self) -> dict[str, object]:
        """Return JSON-safe metadata, never source results or secret literals."""
        report: dict[str, object] = {"state": self.state, "errors": self.errors, "warnings": self.warnings, "provenance": self.provenance, "trust": self.trust, "trust_hash": self.trust_hash, "trust_subset": mask(self.trust_subset, VALUE_SOURCE_KEYS)}
        if self.errors:
            return report
        exposed = [f"QA_{persona.upper()}_{field}" for persona in sorted(set(self.personas) | self.static.keys()) for field in sorted(self.persona_fields(persona))]
        exposed.extend(f"QA_{name.upper()}" for name in self.values)
        exposed.extend(DATABASE_NAMES.get(cast(str, self.database.get("kind")), []))
        report.update({"targets": self.targets, "defaults": {"FE": self.section_target("FE"), "BE": self.section_target("BE")}, "policy": self.policy, "services": {"health": [], "up": None, "prepare": [], "down": None, **self.services}, "personas": self.personas, "static_personas": self.static_personas, "values": sorted(self.values), "exposed": sorted(exposed), "database": mask(self.database, VALUE_SOURCE_KEYS, "env.database") if self.database else None})
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
        return ConfigTransaction(self.repo, validate, self.accept, value_source_keys=VALUE_SOURCE_KEYS)

    def preview(self, proposal: Mapping[str, object]) -> dict[str, object]:
        return self._transaction().preview(proposal)

    def apply(self, proposal: Mapping[str, object], snapshot: str, approved_hash: str) -> dict[str, object]:
        return self._transaction().apply(proposal, snapshot, approved_hash)
