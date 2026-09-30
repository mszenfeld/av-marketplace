"""Parse QA scenarios, resolve branch plans, and check their config requirements.

Parsing never resolves a value source or runs a scenario. CLI reports contain
identifiers and sanitized origins, not scenario payloads or credential values.
Sections and DB checks are scenario IDs in plan order. Persona gaps carry
``name``, ``token``, and ``reason``; value and target gaps carry config names.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from av_config import ConfigError
from av_config import Origin
from av_config import parse_origin
from qa_engine.config import Config

SCENARIO = re.compile(r"^###[ \t]+((FE|BE)-\d{2,})(?=[ \t:]|$)[ \t]*:?[ \t]*(.*)$", re.MULTILINE)
BLOCK_BOUNDARY = re.compile(r"^#{1,3}[ \t]+.*$|^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>.*)$", re.MULTILINE)
FIELD = re.compile(r"^(?:-[ \t]*)?\*\*([^*\n]+?):\*\*[ \t]*(.*)$", re.MULTILINE)
TOKEN = re.compile(r"\$\{(QA_[A-Z0-9_]+)\}|\$(QA_[A-Z0-9_]+)(?![A-Za-z0-9_])")
URL = re.compile(r"https?://[^\s`\"'<>]+", re.IGNORECASE)
WRITE_ACTION = re.compile(r"\b(POST|PUT|PATCH|DELETE)\b", re.IGNORECASE)
SQL_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|CREATE|UPSERT)\b", re.IGNORECASE)
WRITE_STEP = re.compile(r"\b(create|delete|update|insert|seed)\b", re.IGNORECASE)
CITATION = re.compile(r"\((`?[^()\s]+:\d+(?:-\d+)?`?)\)")
STATUS = re.compile(r"(?<![A-Za-z0-9_])([1-5]\d{2})(?![A-Za-z0-9_])")
PERSONA_FIELD = re.compile(r"(?:EMAIL|PASSWORD|ID|TOKEN|COOKIE|COOKIE_[A-Z0-9_]+)\Z")
UNKNOWN_PERSONA = re.compile(r"(.+?)_(EMAIL|PASSWORD|ID|TOKEN|COOKIE|COOKIE_[A-Z0-9_]+)\Z")
RELATIVE_PATH = re.compile(r"(?<![A-Za-z0-9_:/])/(?!/)[A-Za-z0-9_{?]")


@dataclass
class Assertion:
    """An Expected or edge assertion, retaining its grounding and status tokens."""

    text: str
    statuses: list[int] = field(init=False)
    grounding: list[str] = field(init=False)
    unverified: bool = field(init=False)

    def __post_init__(self) -> None:
        self.grounding = [match.strip("`") for match in CITATION.findall(self.text)]
        self.statuses = [int(status) for status in STATUS.findall(CITATION.sub("", self.text))]
        self.unverified = bool(re.search(r"\(unverified\b", self.text, re.IGNORECASE))


@dataclass
class Scenario:
    """A scenario's original block and mechanically parsed execution requirements."""

    id: str
    section: str
    title: str
    text: str
    method: str | None
    path: str | None
    target: str | None
    preconditions: str
    expected: Assertion
    edges: list[Assertion]
    db_check: str | None
    tokens: list[str]
    urls: list[str]
    actions: str
    other_steps: str

    @property
    def main_flow(self) -> str:
        """Return the original main flow without the edge-case field."""
        match = re.search(r"^(?:-\s*)?\*\*Edge cases:\*\*", self.text, re.MULTILINE | re.IGNORECASE)
        return self.text[:match.start()] if match else self.text


@dataclass
class Plan:
    """Source metadata and scenarios in the order the plan dispatches them."""

    path: Path
    branch: str | None
    head: str | None
    scenarios: list[Scenario]

    @property
    def sections(self) -> dict[str, list[str]]:
        return {section: [scenario.id for scenario in self.scenarios if scenario.section == section] for section in ("FE", "BE")}


@dataclass
class _Field:
    name: str
    text: str


def _fields(text: str) -> list[_Field]:
    matches = list(FIELD.finditer(text))
    fields: list[_Field] = []
    preamble = text[:matches[0].start()] if matches else text
    preamble = preamble.partition("\n")[2].strip()
    if preamble:
        fields.append(_Field("steps", preamble))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        continuation = text[match.end():end]
        content = (match.group(2) + continuation).strip("\r\n").rstrip()
        name = match.group(1).lower()
        # A sibling action bullet is not a continuation of an assertion or
        # request declaration. Keep it in the actions checked by both guards.
        sibling = re.search(r"^(?:[-*]|\d+[.)])[ \t]+(?!\*\*)", continuation, re.MULTILINE)
        if sibling and name not in {"steps", "preconditions", "edge cases"}:
            fields.append(_Field(name, (match.group(2) + continuation[:sibling.start()]).strip("\r\n").rstrip()))
            fields.append(_Field("steps", continuation[sibling.start():]))
        else:
            fields.append(_Field(name, content))
    return fields


def _edges(text: str) -> list[Assertion]:
    """Keep one assertion per edge bullet, including its continuation lines."""
    bullets = list(re.finditer(r"^([ \t]*)(?:[-*]|\d+[.)])[ \t]+(\S.*)$", text, re.MULTILINE))
    if not bullets:
        return [Assertion(text)] if text.strip() else []
    indentation = min(len(match.group(1)) for match in bullets)
    bullets = [match for match in bullets if len(match.group(1)) == indentation]
    return [
        Assertion((match.group(2) + text[match.end():bullets[index + 1].start() if index + 1 < len(bullets) else len(text)]).strip())
        for index, match in enumerate(bullets)
    ]


def _source(text: str) -> tuple[str | None, str | None]:
    section = re.search(r"^##[ \t]+Source[ \t]*$", text, re.MULTILINE | re.IGNORECASE)
    if section is None:
        return None, None
    following = re.search(r"^#{1,2}\s+", text[section.end():], re.MULTILINE)
    source = text[section.end():section.end() + following.start() if following else len(text)]
    metadata: dict[str, str] = {}
    for match in re.finditer(r"^[ \t]*(?:-[ \t]*)?(?:\*\*)?(Branch|Head):(?:\*\*)?[ \t]*(.*)$", source, re.MULTILINE | re.IGNORECASE):
        metadata[match.group(1).lower()] = match.group(2).strip().strip("`")
    return metadata.get("branch"), metadata.get("head")


def _scenario(match: re.Match[str], text: str) -> Scenario:
    fields = _fields(text)
    values = {item.name: item.text for item in fields}
    method: str | None = None
    path = values.get("path") or values.get("url")
    method_text = values.get("method", "").strip("`")
    request = re.match(r"(GET|HEAD|OPTIONS|POST|PUT|PATCH|DELETE)\b(?:[ \t]+([^\s`]+))?", method_text, re.IGNORECASE)
    if request:
        method = request.group(1).upper()
        path = request.group(2) or path
    if path:
        path = path.split()[0].strip("`")
    tokens = list(dict.fromkeys(first or second for first, second in TOKEN.findall(text)))
    urls = list(dict.fromkeys(url.rstrip(".,;:)") for url in URL.findall(text)))
    # Expected and DB fields describe assertions, not HTTP/UI actions. DB writes
    # are classified separately, while edges can contain actions of their own.
    actions = "\n".join(item.text for item in fields if item.name not in {"expected", "db check", "target", "area"})
    other_steps = "\n".join(item.text for item in fields if item.name not in {"method", "expected", "db check", "target", "area", "path", "url"})
    return Scenario(
        id=match.group(1), section=match.group(2), title=match.group(3), text=text,
        method=method, path=path, target=values.get("target", "").strip().strip("`") or None,
        preconditions="\n".join(item.text for item in fields if item.name == "preconditions"),
        expected=Assertion(values.get("expected", "")),
        edges=[edge for item in fields if item.name == "edge cases" for edge in _edges(item.text)],
        db_check=values.get("db check"), tokens=tokens, urls=urls,
        actions=actions, other_steps=other_steps,
    )


def parse_plan(path: Path) -> Plan:
    """Read scenario blocks and Source metadata, ignoring headings inside fences.

    Raises ConfigError for an unreadable plan. Payloads remain in memory for
    engine consumers; check_plan does not expose them in its JSON report.
    """
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as error:
        raise ConfigError("plan is not readable") from error
    branch, head = _source(text)
    scenarios: list[Scenario] = []
    start: re.Match[str] | None = None
    fence: str | None = None
    for match in BLOCK_BOUNDARY.finditer(text):
        marker = match.group("fence")
        if marker:
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not match.group("info").strip():
                fence = None
            continue
        if fence is not None:
            continue
        if start is not None:
            scenarios.append(_scenario(start, text[start.start():match.start()].rstrip()))
        start = SCENARIO.match(text, match.start())
    if start is not None:
        scenarios.append(_scenario(start, text[start.start():].rstrip()))
    return Plan(path, branch, head, scenarios)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConfigError("plan resolution git operation failed") from error


def _display_path(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def resolve_plan(repo: Path, argument: str = "") -> dict[str, object]:
    """Reuse explicit paths or the newest current-branch plan, checking its Head.

    An explicit change source, no branch match, or detached HEAD generates a
    plan. Only automatically selected plans are checked for source staleness.
    """
    repo = repo.resolve()
    branch_result = _git(repo, "branch", "--show-current")
    head_result = _git(repo, "rev-parse", "HEAD")
    if branch_result.returncode or head_result.returncode:
        raise ConfigError("plan resolution needs a git repository with a HEAD")
    branch, head = branch_result.stdout.strip(), head_result.stdout.strip()
    result: dict[str, object] = {"action": "generate", "plan": None, "branch": branch, "head": head, "source": argument or branch, "changed_files": []}
    if argument:
        candidate = Path(argument)
        if not candidate.is_absolute():
            candidate = repo / candidate
        if candidate.is_file():
            result.update(action="reuse", plan=_display_path(repo, candidate.resolve()))
        return result
    if not branch:
        return result
    candidates = sorted((repo / "docs/testing/plans").glob("*.md"), key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for path in candidates:
        plan = parse_plan(path)
        if plan.branch != branch:
            continue
        result.update(action="reuse", plan=_display_path(repo, path))
        # Only literal commit IDs are accepted; plan metadata cannot introduce
        # git options or revision expressions that silently refer to another tip.
        if not plan.head or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", plan.head) or _git(repo, "merge-base", "--is-ancestor", plan.head, "HEAD").returncode:
            result["action"] = "stale"
            return result
        changed = _git(repo, "diff", "--name-only", "-z", f"{plan.head}..HEAD")
        if changed.returncode:
            raise ConfigError("plan resolution cannot compare the recorded Head")
        files = [name for name in changed.stdout.split("\0") if name]
        result["changed_files"] = files
        if any(not name.startswith("docs/") for name in files):
            result["action"] = "stale"
        return result
    return result


def _persona_token(token: str, configured: set[str]) -> tuple[str, str] | None:
    name = token.removeprefix("QA_")
    for persona in sorted(configured, key=lambda item: (-len(item), item)):
        prefix = persona.upper() + "_"
        if name.startswith(prefix) and PERSONA_FIELD.fullmatch(name[len(prefix):]):
            return persona, name[len(prefix):]
    return None


def _persona_reason(config: Config, persona: str, capability: str) -> str | None:
    if persona not in config.static:
        if config.policy["mutations"] == "deny":
            return "provisioning is forbidden under mutations=deny"
        if not config.accounts.get("create"):
            return "qa.accounts.create is required to provision this persona"
    if capability not in config.persona_fields(persona):
        recipe = "create or a static id source" if capability == "ID" else "login"
        return f"{recipe} cannot produce {capability}"
    return None


def _origin_display(url: str) -> str | None:
    """Describe a refused origin without reflecting URL userinfo or payloads."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return None
        port = parts.port if parts.port is not None else (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme.lower()}://{host.lower()}:{port}"


def _mutates(scenario: Scenario) -> bool:
    if scenario.section == "FE":
        return bool(WRITE_ACTION.search(scenario.actions))
    return bool(WRITE_ACTION.search(scenario.actions) or WRITE_STEP.search(scenario.other_steps) or SQL_WRITE.search(scenario.db_check or ""))


def _rejection_exempt(scenario: Scenario) -> bool:
    if scenario.section != "BE":
        return False
    assertions = [scenario.expected, *scenario.edges]
    # Assertions, DB checks, request declarations and metadata are checked
    # separately or contain no action; scan every other label to fail closed.
    steps = "\n".join(
        item.text for item in _fields(scenario.text)
        if item.name not in {
            "method", "path", "url", "target", "area", "blocked-by",
            "expected", "edge cases", "headers", "payload", "db check",
        }
    )
    return (
        all(not assertion.unverified and len(assertion.statuses) == 1 and assertion.statuses[0] >= 400 for assertion in assertions)
        and not SQL_WRITE.search(scenario.db_check or "")
        and not WRITE_STEP.search(steps)
        and not WRITE_ACTION.search(steps)
    )


def check_plan(plan: Plan, config: Config) -> dict[str, object]:
    """Return C7 requirements, config gaps, refused origins, and mutation guards.

    Persona recognition precedes value lookup; otherwise configured values win
    before the unknown-token field grammar determines the gap category.
    """
    configured = set(config.personas) | config.static.keys()
    value_names = {name.upper(): name for name in config.values}
    personas: set[str] = set()
    values: set[str] = set()
    persona_gaps: dict[str, dict[str, str]] = {}
    missing_values: set[str] = set()
    missing_targets: set[str] = set()
    db_checks: list[str] = []
    off_target: list[dict[str, object]] = []
    guarded: list[str] = []
    exempt: list[str] = []
    origins: set[Origin] = {parse_origin(origin, origin_only=True) for origin in config.targets.values()}
    for scenario in plan.scenarios:
        for token in scenario.tokens:
            recognized = _persona_token(token, configured)
            if recognized:
                persona, capability = recognized
                personas.add(persona)
                reason = _persona_reason(config, persona, capability)
                if reason:
                    persona_gaps[token] = {"name": persona, "token": token, "reason": reason}
            elif token[3:] in value_names:
                values.add(value_names[token[3:]])
            else:
                unknown = UNKNOWN_PERSONA.fullmatch(token[3:])
                if unknown:
                    persona_gaps[token] = {"name": unknown.group(1).lower(), "token": token, "reason": "persona is not configured"}
                else:
                    missing_values.add(token[3:])
        if scenario.target:
            if scenario.target not in config.targets:
                missing_targets.add(scenario.target)
        else:
            # An absolute-only scenario already names its origin; relative
            # actions (including setup requests) still need the section default.
            without_urls = URL.sub("", scenario.actions)
            needs_default = not scenario.urls or bool(RELATIVE_PATH.search(without_urls))
            if needs_default:
                key = "fe_target" if scenario.section == "FE" else "be_target"
                target = config.defaults.get(key)
                if not isinstance(target, str) or not target:
                    missing_targets.add(f"qa.defaults.{key}")
                elif target not in config.targets:
                    missing_targets.add(target)
        for url in scenario.urls:
            try:
                allowed = parse_origin(url) in origins
                reason = "origin is not a configured target"
            except ConfigError:
                allowed = False
                reason = "invalid URL or userinfo is refused"
            if not allowed:
                off_target.append({"scenario": scenario.id, "origin": _origin_display(url), "reason": reason})
        if scenario.db_check is not None:
            db_checks.append(scenario.id)
        if config.policy["mutations"] != "allow" and _mutates(scenario):
            if config.policy["mutations"] == "rejections-only" and _rejection_exempt(scenario):
                exempt.append(scenario.id)
            else:
                guarded.append(scenario.id)
    missing = {
        "personas": [persona_gaps[token] for token in sorted(persona_gaps)],
        "values": sorted(missing_values), "targets": sorted(missing_targets),
        "database": bool(db_checks and not config.database),
    }
    return {
        "ok": not any(missing.values()) and not off_target,
        "sections": plan.sections, "personas": sorted(personas), "values": sorted(values),
        "missing": missing, "off_target": off_target, "guarded": guarded,
        "exempt": exempt, "db_checks": db_checks,
    }
