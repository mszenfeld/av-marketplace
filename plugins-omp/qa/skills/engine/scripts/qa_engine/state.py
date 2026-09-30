"""Run lifecycle, verdicts, QA IDs and loop decisions for ``/qa:run``.

``run start`` pins the plan hash and the effective config in a private run
directory and takes one lock per target origin. Loop-critical state lives in
the sidecar ``docs/testing/reports/<topic>-loop-state.json`` so it survives the
orchestrator's many tool calls; every mutation happens under the run's state
lock. Account refresh uses the private channel; records hold persona names,
scenario IDs, file fingerprints and masked config, never credential values.
"""
from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import time
from typing import Any
from typing import TypeGuard
from typing import cast
from urllib.parse import urlsplit

from av_config import ConfigError
from av_config import InvalidConfig
from av_config import atomic_write
from av_config import canonical_hash
from av_config import mask
from av_config import parse_origin
from av_config import source_subset
from qa_engine.config import Config
from qa_engine.accounts import refresh
from qa_engine.plan import FIELD
from qa_engine.plan import PERSONA_FIELD
from qa_engine.plan import TOKEN
from qa_engine.plan import Assertion
from qa_engine.plan import Plan
from qa_engine.plan import Scenario
from qa_engine.plan import check_plan
from qa_engine.plan import parse_plan

JSON = dict[str, Any]

RUN_ID = re.compile(r"[0-9a-f]{8}\Z")
RUN_DIRECTORY = re.compile(r"qa-run-[0-9a-f]{8}\Z")
ISSUE_ID = re.compile(r"QA-\d{3,}\Z")
ISSUE_NUMBER = re.compile(r"\bQA-(\d{3,})\b")
ASSERTION_KEY = re.compile(r"((?:FE|BE)-\d{2,})(?: \(edge ([1-9]\d*)\))?\Z")
SCENARIO_REFERENCE = re.compile(r"\b(?:FE|BE)-\d{2,}\b")
ISSUE_HEADING = re.compile(r"^###[ \t]+\[([A-Za-z]+)\][ \t]+(QA-\d{3,})\b.*$", re.MULTILINE)
HEADING_ISSUE = re.compile(r"^###[^\n]*?\b(QA-\d{3,})\b", re.MULTILINE)
BLOCK_END = re.compile(r"^(?:#{1,3}[ \t]|---[ \t]*$)", re.MULTILINE)
LOOP_HISTORY = re.compile(r"^##[ \t]+Loop History[ \t]*$(.*?)(?=^##[ \t]|\Z)", re.MULTILINE | re.DOTALL)
EXPECTED_LINE = re.compile(r"^[ \t]*[-*][ \t]*(?:\*\*Expected:\*\*|Expected:)[ \t]*(.+)$", re.MULTILINE)
RESULT_FENCE = re.compile(r"^(`{3,}|~{3,})[ \t]*json[ \t]+qa-results[ \t]*$", re.MULTILINE)
CODE_FENCE = re.compile(r"^[ \t]*(?:`{3,}|~{3,}).*$", re.MULTILINE)
LOCATION = re.compile(r"\S+:\d+(?:-\d+)?\Z")
LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`([^`\n]*)`')
TOOL_PROSE = re.compile(r"no .*client|unavailable|not supported", re.IGNORECASE)
TRANSPORT_PROSE = re.compile(r"connection refused|could not connect|timeout", re.IGNORECASE)
STATUSES = frozenset({"PASS", "FAIL", "SKIP", "NEED_INFO"})
KINDS = frozenset({"credentials", "service", "fixture", "tool"})
# A reclassified auth-unverified main flow still mints an issue (loop.md Step 2.2).
FAILING = frozenset({"FAIL", "AUTH"})
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
SANITY_PATHS = frozenset({"/health", "/healthz", "/openapi.json", "/version", "/", "/docs", "/api/docs"})
STOP_REASONS = frozenset({"user-abort", "config-drift", "login-failure", "plan-changed", "cleanup-error", "other"})
PAYLOAD_FIELDS = frozenset({"payload", "request payload", "body", "request body"})
STALE_SECONDS = 24 * 3600
LOCK_GRACE_MINUTES = 15
# Kept across runs of one plan (idempotency Case 1 and Case 2); everything
# else in the sidecar describes a single run and is reset by ``run start``.
PERSISTENT: dict[str, type] = {
    "created": str, "scenario_issues": dict, "issue_assertion": dict, "scenario_kind": dict,
    "scenario_reason": dict, "unverified_issues": list, "auth_gated_issues": list, "need_info": dict,
    "auto_generated": bool,
}
RUN_SCOPED: dict[str, type | tuple[type, ...]] = {
    "plan_sha256": str, "plan_path": str, "report_file": str, "topic": str, "run_id": str,
    "baseline": dict, "current": dict, "assertions": dict, "pre_loop_dirty": list, "fix_touched_files": list,
    "dispatch_count": int, "dispatches": dict, "iteration": int, "open_iteration": (dict, type(None)),
    "loop_end": (dict, type(None)), "iterations": list,
}


class StateStop(ConfigError):
    """A domain stop (exit 1); ``details`` carries only run IDs, times and origins."""

    def __init__(self, message: str, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = dict(details or {})


@dataclass
class IssueBlock:
    """The fields of one ``### [SEVERITY] QA-NNN`` report block the loop reads."""

    severity: str
    status: str | None
    location: str | None
    problem: bool
    remediation: bool
    scenario: str | None
    expected: str | None

    @property
    def rejected(self) -> bool:
        # Prefix match: a rejected Status carries a reason tail we do not control.
        return self.status is not None and self.status.startswith("🚫 Rejected")

    @property
    def located(self) -> bool:
        return self.location is not None and self.location != "unknown:0" and bool(LOCATION.fullmatch(self.location))


@dataclass
class Evaluation:
    """One scenario's ingested result: verdict, reason, gaps and assertion records."""

    verdict: str
    reason: str | None
    kind: str
    complete: bool
    assertions: dict[str, JSON] = field(default_factory=dict)
    gaps: dict[str, JSON] = field(default_factory=dict)


@dataclass
class Run:
    """A started run: its private record and the sidecar state it mutates."""

    repo: Path
    run_id: str
    directory: Path
    record: JSON
    state: JSON

    @property
    def policy(self) -> JSON:
        return cast(JSON, self.record["config"]["qa"]["policy"])

    @property
    def budget(self) -> JSON:
        return cast(JSON, self.record["config"]["qa"]["budget"])

    @property
    def plan_path(self) -> Path:
        return Path(self.record["plan"])

    def plan_unchanged(self) -> bool:
        return _plan_hash(self.plan_path) == str(self.record["plan_sha256"])

    def plan(self, *, strict: bool = True) -> Plan:
        """Parse the run's plan; ``strict`` refuses a plan edited since ``run start``."""
        if strict and not self.plan_unchanged():
            raise StateStop("plan changed mid-run (hash mismatch)")
        return parse_plan(self.plan_path)

    def report_text(self) -> str:
        try:
            return Path(self.record["report"]).read_text()
        except FileNotFoundError:
            return ""

    def artifacts(self) -> set[str]:
        """Repository-relative paths the loop itself writes, never counted as fix edits."""
        paths = {Path(self.record["sidecar"]), Path(self.record["report"])}
        paths |= {path.with_suffix(".bak") for path in paths}
        paths |= {self.repo / path for path in self.record.get("bootstrap_dirty", [])}
        return {_relative(self.repo, path) for path in paths}


class OriginLocks:
    """One lock file per target origin, shared by every checkout on this machine."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.directory / ".mutex").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _path(self, origin: str) -> Path:
        return self.directory / f"{hashlib.sha256(origin.encode()).hexdigest()[:32]}.json"

    def _holders(self) -> list[tuple[Path, JSON]]:
        return [(path, _lock_data(path) or {}) for path in sorted(self.directory.glob("*.json"))]

    def acquire(self, origins: Sequence[str], holder: JSON, now: float, takeover: str | None) -> list[Path]:
        """Take every origin in sorted order, or none; return taken-over run directories.

        The check runs before any write, under the lock directory's mutex, so a
        conflict leaves nothing behind. A lock is live until released or older
        than its holder's limit; ``takeover`` names the one holder that may be
        displaced, and every other lock of that holder is released as well.
        """
        with self._exclusive():
            displaced: set[str] = set()
            for origin in sorted(origins):
                current = _lock_data(self._path(origin))
                if current is None or not _live(current, now):
                    continue
                if takeover is not None and current.get("run_id") == takeover:
                    displaced.add(str(current.get("dir", "")))
                    continue
                raise StateStop("a live run holds this stack", {"holder": {
                    "run": current.get("run_id"), "started": _iso(float(current["started"])), "origin": origin,
                }})
            acquired: list[Path] = []
            try:
                for origin in sorted(origins):
                    path = self._path(origin)
                    atomic_write(path, json.dumps({**holder, "origin": origin}).encode(), 0o600)
                    acquired.append(path)
            except OSError:
                for path in acquired:
                    path.unlink(missing_ok=True)
                raise
            if displaced:
                for path, data in self._holders():
                    if data.get("run_id") == takeover:
                        path.unlink(missing_ok=True)

        return [Path(directory) for directory in sorted(displaced) if directory]

    def release(self, run_id: str) -> list[str]:
        if not self.directory.is_dir():
            return []
        released: list[str] = []
        with self._exclusive():
            for path, data in self._holders():
                if data.get("run_id") == run_id:
                    path.unlink(missing_ok=True)
                    released.append(str(data.get("origin")))

        return sorted(released)


def effective_config(config: Config) -> JSON:
    """Pin only QA's environment tables and QA config, with defaults applied."""
    env = {name: config.env[name] for name in ("targets", "services", "secrets", "values", "database") if name in config.env}
    return {"env": env, "qa": {**config.qa, "policy": config.policy, "budget": config.budget}}


def check_drift(run: Run, config: Config) -> None:
    """Refuse to act on a config other than the one ``run start`` recorded."""
    if config.errors or canonical_hash(effective_config(config)) != run.record["config_hash"]:
        raise StateStop("config changed during run")


def run_directory(run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise InvalidConfig("invalid run id")
    return _tmp_root() / f"qa-run-{run_id}"


def default_lock_directory() -> Path:
    home = Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state"))
    return home / "av-marketplace/qa-locks"


def start_run(
    config: Config, plan_path: Path, takeover: str | None = None, *,
    generated: bool = False, baseline_run: str | None = None, baseline_file: Path | None = None,
) -> JSON:
    """Create a run and its sidecar, rolling back locks and directory on failure."""
    if config.errors:
        raise InvalidConfig("invalid configuration; run the config subcommand for its errors")
    if config.state != "ok":
        raise StateStop("no QA configuration in .av/config.toml")
    if config.trust not in {"trusted", "not-required"}:
        raise StateStop("trust required")
    if takeover is not None and not RUN_ID.fullmatch(takeover):
        raise InvalidConfig("invalid run id")

    repo = config.repo
    plan_path = plan_path.resolve()
    plan_hash = _plan_hash(plan_path)
    if plan_hash is None:
        raise ConfigError("plan is not readable")
    plan = parse_plan(plan_path)
    effective = effective_config(config)
    pre_loop = _restart_baseline(repo, plan, baseline_run) if baseline_run is not None else None
    if baseline_file is not None:
        names = _load_json(baseline_file)
        if not isinstance(names, list) or any(
            not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts
            for name in names
        ):
            raise InvalidConfig("baseline file must contain a JSON array of repository-relative paths")
        pre_loop = {name: _fingerprint(repo / name) for name in names}
    origins = sorted({_origin_text(url) for url in config.targets.values()})
    now = time.time()
    tmp = _tmp_root()
    _remove_stale(tmp, repo, now)
    locks = OriginLocks(config.store.path.parent / "qa-locks")
    run_id, directory = _create_run_directory(tmp)
    started = False

    try:
        holder = {
            "run_id": run_id, "started": now, "repo": str(repo), "dir": str(directory),
            "limit_minutes": float(cast(float, config.budget["minutes"])) + LOCK_GRACE_MINUTES,
        }
        for displaced in locks.acquire(origins, holder, now, takeover):
            _remove_run_directory(displaced)
        start_dirty = _dirty(repo)
        if pre_loop is None:
            pre_loop = start_dirty
        idempotency, sidecar, report, state = _prepare_sidecar(repo, plan, plan_hash, run_id, pre_loop, generated)
        record = {
            "run_id": run_id, "repo": str(repo), "plan": str(plan_path), "plan_sha256": plan_hash,
            "started": now, "sidecar": str(sidecar), "report": str(report), "config": mask(effective),
            "config_hash": canonical_hash(effective), "trust_hash": config.trust_hash, "origins": origins,
            "locks": str(locks.directory), "pre_loop": pre_loop,
            "bootstrap_dirty": sorted((set(start_dirty) - set(pre_loop)) & {".av/config.toml", ".gitignore"}),
        }
        _write_json(directory / "run.json", record, 0o600)
        (directory / "results").mkdir(mode=0o700)
        _write_json(sidecar, state)
        started = True
    finally:
        if not started:
            try:
                locks.release(run_id)
            finally:
                shutil.rmtree(directory, ignore_errors=True)

    return {"run": run_id, "dir": str(directory), "sidecar": str(sidecar), "report": str(report), "idempotency": idempotency}


def end_run(repo: Path, run_id: str) -> JSON:
    """Release this run's locks and delete its directory; never compares the config."""
    directory = run_directory(run_id)
    record = _load_json(directory / "run.json") if _owned_directory(directory) else None
    if isinstance(record, dict) and record.get("repo") != str(repo):
        raise StateStop("run belongs to another repository")
    recorded = record.get("locks") if isinstance(record, dict) else None
    locks = OriginLocks(Path(recorded) if isinstance(recorded, str) else default_lock_directory())
    released = locks.release(run_id)
    _remove_run_directory(directory)

    return {"released": released}


def _restart_baseline(repo: Path, plan: Plan, run_id: str) -> dict[str, str]:
    """Read the ended pass's fingerprints from its durable sidecar, not its deleted directory."""
    if not RUN_ID.fullmatch(run_id):
        raise InvalidConfig("invalid baseline run id")
    path = repo / "docs/testing/reports" / f"{_topic(plan.path)}-loop-state.json"
    stored = _load_json(path)
    if not isinstance(stored, dict) or stored.get("run_id") != run_id:
        raise StateStop("baseline run is unavailable for this plan")
    baseline = stored.get("pre_loop")
    if not isinstance(baseline, dict) or any(not isinstance(name, str) or not isinstance(value, str) for name, value in baseline.items()):
        raise StateStop("baseline run has no recorded dirty fingerprints")

    return baseline


def _stop_hidden(run: Run) -> set[str]:
    """Collect private values and configured sources without resolving or executing them."""
    hidden: set[str] = set()

    def remember(value: object) -> None:
        if isinstance(value, str) and value:
            hidden.add(value)
            hidden.add(json.dumps(value, ensure_ascii=False)[1:-1])
        elif isinstance(value, Mapping):
            for item in value.values():
                remember(item)
        elif isinstance(value, list):
            for item in value:
                remember(item)

    for config in (run.record["config"], Config(run.repo).data):
        for source in source_subset(config).values():
            if isinstance(source, str):
                if source.startswith("literal:") and source != "literal:***":
                    remember(source[8:])
                elif source.startswith("env:"):
                    remember(os.environ.get(source[4:]))
    for name in ("accounts.private.json", "secrets.json"):
        remember(_load_json(run.directory / name))

    return hidden


def stop_run(run: Run, reason: str, detail: str | None = None) -> JSON:
    """Close pending fix work and record a sanitized explicit stop, without config drift checks."""
    if reason not in STOP_REASONS:
        raise InvalidConfig("invalid stop reason")
    if run.state["open_iteration"] is not None:
        iteration_close(run, decide=False)
    end = run.state["loop_end"] or {}
    if end.get("decision") == "stop":
        return end
    sanitized = detail or ""
    if sanitized:
        for value in sorted(_stop_hidden(run), key=len, reverse=True):
            sanitized = sanitized.replace(value, "***")
        sanitized = " ".join(sanitized.split())
    end = {"decision": "stop", "reason": reason, "detail": sanitized}
    run.state["loop_end"] = end

    return end


@contextmanager
def open_run(repo: Path, run_id: str, *, close_iteration: bool = False) -> Iterator[Run]:
    """Load locked state; exit closure persists even if the subsequent operation fails."""
    directory = run_directory(run_id)
    if not _owned_directory(directory):
        raise StateStop("unknown run; start one with run start")
    with (directory / "state.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        record = _load_json(directory / "run.json")
        if not isinstance(record, dict) or record.get("repo") != str(repo) or record.get("run_id") != run_id:
            raise StateStop("run belongs to another repository")
        sidecar = Path(record["sidecar"])
        state = _load_json(sidecar)
        if not _valid_state(state):
            raise StateStop("the sidecar is missing or invalid")
        if state["run_id"] != run_id:
            raise StateStop("the sidecar belongs to another run")
        run = Run(repo, run_id, directory, record, state)
        if close_iteration and state["open_iteration"] is not None:
            iteration_close(run, decide=False)
            _write_json(sidecar, state)
        before = json.dumps(state, sort_keys=True)
        yield run
        if json.dumps(state, sort_keys=True) != before:
            _write_json(sidecar, state)


def dispatch_tester(run: Run, config: Config, section: str, phase: str) -> JSON:
    """Log in section personas before recording a tester assignment."""
    check_drift(run, config)
    state = run.state
    plan = run.plan()
    ids = plan.sections.get(section, [])
    if not ids:
        raise StateStop(f"the plan has no {section} scenarios")
    opened = state["open_iteration"]
    if phase in {"baseline", "retry"} and (state["iteration"] or opened is not None):
        raise StateStop("baseline and retry dispatches precede the first iteration")
    if phase == "iteration" and opened is None:
        raise StateStop("no iteration is open; run iteration open first")

    scenarios = {scenario.id: scenario for scenario in plan.scenarios}
    guarded = [sid for sid in cast(list[str], check_plan(plan, config)["guarded"]) if sid in ids]
    edges = {sid: len(scenarios[sid].edges) for sid in ids}
    authenticated = refresh(run, config, section)
    dispatch = _record_dispatch(state, {
        "kind": "tester", "section": section, "phase": phase, "scenarios": ids, "edges": edges,
        "guarded": guarded, "authenticated": sorted(set(authenticated)),
        "iteration": opened["iteration"] if opened else None, "ingested": False,
    })

    return {
        "dispatch": dispatch, "scenarios": ids, "edges": edges, "guarded": guarded, "refreshed": authenticated,
        "dispatch_count": state["dispatch_count"], "budget_left": _budget_left(run),
    }


def dispatch_fix(run: Run, config: Config, qa: str) -> JSON:
    """Record a fix-auto dispatch for a current candidate of the open iteration."""
    check_drift(run, config)
    if not ISSUE_ID.fullmatch(qa):
        raise InvalidConfig("invalid QA ID")
    state = run.state
    opened = state["open_iteration"]
    if opened is None:
        raise StateStop("no iteration is open; run iteration open first")
    entry = next((item for item in candidates(run)["fix"] if item["qa"] == qa), None)
    if entry is None:
        raise StateStop(f"{qa} is not a fix candidate")

    dispatch = _record_dispatch(state, {
        "kind": "fix", "qa": qa, "key": entry["key"], "iteration": opened["iteration"],
        "dirty_before": _dirty(run.repo), "result": None, "warnings": [],
    })

    return {
        "dispatch": dispatch, "qa": qa, "scenarios": [_scenario_of(entry["key"])], "edges": {}, "refreshed": [],
        "dispatch_count": state["dispatch_count"], "budget_left": _budget_left(run),
    }


def fix_done(run: Run, dispatch_id: str, result: str) -> JSON:
    """Record a fix attempt and its anti-hardcoding warnings; verdicts stay with the re-run."""
    state = run.state
    record = state["dispatches"].get(dispatch_id)
    if not isinstance(record, dict) or record.get("kind") != "fix":
        raise StateStop("unknown fix dispatch")
    if record["result"] is not None:
        if record["result"] == result:
            return {"warnings": record["warnings"]}
        raise StateStop("a different result is already recorded for this dispatch")
    opened = state["open_iteration"]
    if opened is None or opened["iteration"] != record["iteration"]:
        raise StateStop("the iteration of this fix is closed")

    warnings = _hardcoding_warnings(run, record)
    record.update(result=result, warnings=warnings)

    return {"warnings": warnings}


def ingest(run: Run, dispatch_id: str, text: str) -> JSON:
    """Apply a tester's C8 block to its dispatch's assignment (loop.md Steps 2.1.5-2.1.7)."""
    state = run.state
    record = state["dispatches"].get(dispatch_id)
    if not isinstance(record, dict) or record.get("kind") != "tester":
        raise StateStop("unknown tester dispatch")
    if record["ingested"]:
        raise StateStop("dispatch already ingested")
    if any(
        other.get("kind") == "tester" and other.get("section") == record["section"] and other.get("ingested")
        and _dispatch_number(key) > _dispatch_number(dispatch_id)
        for key, other in state["dispatches"].items()
    ):
        raise StateStop("a later dispatch of this section was already ingested")

    plan = run.plan()
    scenarios = {scenario.id: scenario for scenario in plan.scenarios}
    planned: dict[str, int] = record["edges"]
    rows, error = _parse_results(text, record["section"], planned)
    accounts = run.record["config"]["qa"].get("accounts", {})
    personas = [name for name in accounts.get("personas", []) if isinstance(name, str)] + list(accounts.get("static", {}))
    authenticated = set(record["authenticated"])
    previous = dict(state["current"])
    verdicts: dict[str, str] = {}
    gaps: dict[str, JSON] = {}
    incomplete: list[str] = []

    for sid in record["scenarios"]:
        scenario = scenarios[sid]
        credentialed = _sends_credential(scenario, authenticated, personas, "login" in accounts)
        outcome = _evaluate(scenario, rows.get(sid) if rows is not None else None, planned[sid], credentialed, sid in record["guarded"])
        _forget(state["need_info"], sid)
        _forget(state["assertions"], sid)
        state["need_info"].update(outcome.gaps)
        state["assertions"].update(outcome.assertions)
        state["scenario_kind"][sid] = outcome.kind
        if outcome.reason is None:
            state["scenario_reason"].pop(sid, None)
        else:
            state["scenario_reason"][sid] = outcome.reason
        state["current"][sid] = outcome.verdict
        if record["phase"] in {"baseline", "retry"}:
            state["baseline"][sid] = outcome.verdict
        verdicts[sid] = outcome.verdict
        gaps.update(outcome.gaps)
        if not outcome.complete:
            incomplete.append(sid)

    record["ingested"] = True
    _refresh_guards(state, plan)
    claims = _claims(state)
    comparing = record["phase"] in {"iteration", "final"}
    result: JSON = {
        "verdicts": verdicts, "need_info": gaps,
        "new_failures": [
            key for sid in record["scenarios"] for key in _keys(scenarios[sid])
            if state["assertions"].get(key, {}).get("result") in FAILING and key not in claims
        ],
        "regressions": [sid for sid in verdicts if comparing and state["baseline"].get(sid) == "pass" and verdicts[sid] == "fail"],
        "now_passing": [sid for sid in verdicts if previous.get(sid) == "fail" and verdicts[sid] == "pass"],
        "incomplete": incomplete,
    }
    if error is not None:
        result["error"] = error

    return result


def assign_issues(run: Run) -> JSON:
    """Key-based QA-ID assignment for failing assertions, in plan order (loop.md Step 2.2)."""
    state = run.state
    plan = run.plan()
    claims = _claims(state)
    number = _highest_issue(run) + 1
    assigned: list[JSON] = []
    open_ids: list[str] = []

    for scenario in plan.scenarios:
        for key in _keys(scenario):
            record = state["assertions"].get(key)
            if record is None or record["result"] not in FAILING:
                continue
            if key in claims:
                open_ids.extend(claims[key])
                continue
            qa = f"QA-{number:03d}"
            number += 1
            claims[key] = [qa]
            state["issue_assertion"][qa] = key
            issues = state["scenario_issues"].setdefault(scenario.id, [])
            if qa not in issues:
                issues.append(qa)
            unverified = _assertion(scenario, key).unverified
            observed = record["observed_status"]
            if (observed is not None and observed >= 500) or record["crash"]:
                floor: str | None = "CRITICAL"
            elif unverified:
                floor = "LOW"
            else:
                floor = None
            assigned.append({
                "qa": qa, "key": key, "scenario": scenario.id, "observed_status": observed,
                "unverified": unverified, "severity_floor": floor,
            })
            open_ids.append(qa)

    _refresh_guards(state, plan)

    return {"assign": assigned, "open": open_ids}


def candidates(run: Run) -> JSON:
    """Step 3a: failing issues that may go to fix-auto, and why the rest may not."""
    state = run.state
    plan = run.plan()
    blocks = _issue_blocks(run.report_text())
    claims = _claims(state)
    auto = run.policy["fix"] == "auto"
    floor = SEVERITIES.index(run.policy["min_severity"])
    fix: list[JSON] = []
    dropped: list[JSON] = []

    for scenario in plan.scenarios:
        if state["current"].get(scenario.id) != "fail":
            continue
        for qa in _sorted_ids(state["scenario_issues"].get(scenario.id, [])):
            key = state["issue_assertion"].get(qa)
            record = state["assertions"].get(key) if isinstance(key, str) else None
            reason = _drop_reason(state, scenario, qa, key, blocks.get(qa), claims, record, floor)
            flags: list[str] = []
            if reason is None and qa in state["unverified_issues"]:
                if auto:
                    reason = "unverified assertion"
                flags.append("unverified")
            if reason is None and record is not None and record["auth"]:
                if auto:
                    reason = "auth"
                flags.append("auth")
            if reason is None:
                fix.append({"qa": qa, "key": key, "flags": flags})
            else:
                dropped.append({"qa": qa, "reason": reason})

    return {"fix": fix, "dropped": dropped}


def iteration_open(run: Run) -> JSON:
    """Step 3.0: decide whether another fix iteration starts; increment only on iterate."""
    state = run.state
    iteration = state["iteration"]
    if not run.plan_unchanged():
        return {"decision": "stop", "iteration": iteration, "reason": "plan changed mid-run (hash mismatch)"}
    if state["open_iteration"] is not None:
        return {"decision": "iterate", "iteration": iteration, "reason": "iteration already open"}
    if state["loop_end"] is not None:
        return {"decision": state["loop_end"]["decision"], "iteration": iteration, "reason": state["loop_end"]["reason"]}
    if not failures_at_floor(run):
        return {"decision": "final", "iteration": iteration, "reason": "no failures at or above min_severity"}
    exhausted = _budget_exhausted(run)
    if exhausted is not None:
        return {"decision": "final", "iteration": iteration, "reason": exhausted}

    state["iteration"] = iteration + 1
    state["open_iteration"] = {
        "iteration": iteration + 1, "snapshot": dict(state["current"]),
        "dispatch_count": state["dispatch_count"], "opened": time.time(),
    }

    return {"decision": "iterate", "iteration": iteration + 1, "reason": "failures remain at or above min_severity"}


def iteration_close(run: Run, *, decide: bool = True) -> JSON:
    """Commit one history row; exit closures collect recovery without deciding the loop end."""
    state = run.state
    opened = state["open_iteration"]
    if opened is None:
        if state["iterations"]:
            return cast(JSON, state["iterations"][-1]["result"])
        raise StateStop("no iteration is open")
    fixes = [
        record for record in state["dispatches"].values()
        if record.get("kind") == "fix" and record.get("iteration") == opened["iteration"]
    ]
    for record in fixes:
        if record["result"] is None:
            warnings = _hardcoding_warnings(run, record) if run.plan_unchanged() else [
                f"{record['qa']}: Anti-hardcoding check unavailable: plan changed mid-run"
            ]
            record.update(result="failed", warnings=warnings)

    snapshot: dict[str, str] = opened["snapshot"]
    current: dict[str, str] = state["current"]
    now_passing = _ordered(sid for sid, verdict in snapshot.items() if verdict == "fail" and current.get(sid) == "pass")
    regressions = _ordered(sid for sid, verdict in current.items() if verdict == "fail" and state["baseline"].get(sid) == "pass")
    dirty = _dirty(run.repo)
    pre_loop: dict[str, str] = run.record["pre_loop"]
    artifacts = run.artifacts()
    touched = sorted(set(state["fix_touched_files"]) | {path for path in dirty if path not in pre_loop and path not in artifacts})
    overlap = sorted(path for path, fingerprint in pre_loop.items() if dirty.get(path) != fingerprint and path not in artifacts)
    state["fix_touched_files"] = touched

    if not decide:
        decision, reason = "closed", "iteration closed at exit"
    elif not run.plan_unchanged():
        decision, reason = "continue", "plan changed mid-run; the next iteration open stops"
    elif regressions:
        decision, reason = "final", f"scenario regression detected: {', '.join(regressions)}"
    elif not now_passing:
        decision, reason = "final", "no progress this iteration (no newly passing scenarios)"
    else:
        exhausted = _budget_exhausted(run)
        decision, reason = ("final", exhausted) if exhausted is not None else ("continue", "progress this iteration")

    result = {
        "decision": decision, "reason": reason, "now_passing": now_passing, "regressions": regressions,
        "fix_touched_files": touched, "overlap": overlap,
    }
    state["iterations"].append({
        "iteration": opened["iteration"],
        "failing_in": _ordered(sid for sid, verdict in snapshot.items() if verdict == "fail"),
        "attempted_fixes": [record["qa"] for record in fixes],
        "fix_results": {record["qa"]: record["result"] for record in fixes},
        "now_passing": now_passing,
        "still_failing": _ordered(sid for sid, verdict in current.items() if verdict == "fail"),
        "regressions": regressions,
        "warnings": [warning for record in fixes for warning in record["warnings"]],
        "dispatch_count": state["dispatch_count"] - opened["dispatch_count"],
        "elapsed_s": int(time.time() - float(run.record["started"])),
        "result": result,
    })
    state["open_iteration"] = None
    if decision == "final" and (state["loop_end"] or {}).get("decision") != "stop":
        state["loop_end"] = {"decision": decision, "reason": reason}

    return result


def scenario_kind(scenario: Scenario) -> str:
    """Step 2.1.6: what a PASS of this scenario means for coverage."""
    if scenario.section == "FE":
        return "feature"
    statuses = scenario.expected.statuses
    if statuses and statuses[0] >= 400:
        return "negative"
    if scenario.path is not None and (urlsplit(scenario.path).path or "/") in SANITY_PATHS:
        return "sanity"
    return "feature"


def _evaluate(scenario: Scenario, row: JSON | None, planned: int, credentialed: bool, guarded: bool) -> Evaluation:
    skipped: JSON = {"status": "SKIP", "observed_status": None, "crash": False, "kind": None, "missing": [], "skip_reason": None, "refutation": None}
    given: dict[int, JSON] = row["edges"] if row is not None else {}
    complete = row is not None and len(given) == planned
    main = row if row is not None else skipped
    edges = [given.get(number, skipped) for number in range(1, planned + 1)]
    if guarded:
        # The guard excluded this scenario; a reported result is neither credited nor minted.
        main, edges = skipped, [skipped] * planned
    kind = scenario_kind(scenario)
    main_result = main["status"]
    auth = False
    statuses = scenario.expected.statuses
    if (
        scenario.section == "BE" and kind == "feature" and main_result in {"PASS", "FAIL"}
        and main["observed_status"] in {401, 403} and statuses and 200 <= statuses[0] < 300
    ):
        if credentialed:
            main_result, auth = "FAIL", True
        else:
            main_result = "AUTH"

    results = [edge["status"] for edge in edges]
    if main_result == "FAIL" or "FAIL" in results:
        verdict = "fail"
    elif main_result == "NEED_INFO" or "NEED_INFO" in results:
        verdict = "need-info"
    elif main_result == "AUTH":
        verdict = "auth-unverified"
    elif main_result == "SKIP" or "SKIP" in results:
        verdict = "skip"
    else:
        verdict = "pass"

    skip = main if main_result == "SKIP" else next((edge for edge in edges if edge["status"] == "SKIP"), None)
    evaluation = Evaluation(verdict, _reason(verdict, guarded, skip), kind, complete)
    evaluation.assertions[scenario.id] = _assertion_record(main, main_result, auth)
    if main["status"] == "NEED_INFO":
        evaluation.gaps[scenario.id] = {"kind": main["kind"], "missing": main["missing"]}
    for number, edge in enumerate(edges, 1):
        key = f"{scenario.id} (edge {number})"
        evaluation.assertions[key] = _assertion_record(edge, edge["status"], False)
        if edge["status"] == "NEED_INFO":
            evaluation.gaps[key] = {"kind": edge["kind"], "missing": edge["missing"]}

    return evaluation


def _assertion_record(outcome: JSON, result: str, auth: bool) -> JSON:
    return {
        "result": result, "observed_status": outcome["observed_status"], "crash": outcome["crash"],
        "auth": auth, "refutation": outcome["refutation"],
    }


def _reason(verdict: str, guarded: bool, skip: JSON | None) -> str | None:
    """Normalize the reason of a non-pass, non-fail verdict (loop.md Step 2.1.7)."""
    if verdict in {"pass", "fail"}:
        return None
    if verdict == "need-info":
        return "need-info"
    if guarded:
        return "mutation-guard"
    if verdict == "auth-unverified":
        return "auth-unverified"
    text = skip["skip_reason"] if skip is not None else None
    if not text or text.strip().lower().startswith(("harness error:", "out of harness scope:")):
        return "cannot-confirm"
    if TOOL_PROSE.search(text):
        return "tool-unavailable"
    if TRANSPORT_PROSE.search(text):
        return "transport"
    return "cannot-confirm"


def _sends_credential(scenario: Scenario, authenticated: set[str], personas: Sequence[str], login: bool) -> bool:
    """Whether the main flow uses a credential of a persona this dispatch logged in."""
    if not authenticated:
        return False
    for first, second in TOKEN.findall(scenario.main_flow):
        name = (first or second).removeprefix("QA_")
        for persona in sorted(personas, key=len, reverse=True):
            prefix = f"{persona.upper()}_"
            if not name.startswith(prefix):
                continue
            capability = name.removeprefix(prefix)
            sends = capability == "TOKEN" or capability.startswith("COOKIE") or (login and capability in {"EMAIL", "PASSWORD"})
            if persona in authenticated and sends and PERSONA_FIELD.match(capability):
                return True
            break
    return False


def _parse_results(text: str, section: str, planned: Mapping[str, int]) -> tuple[dict[str, JSON] | None, str | None]:
    """Read the single C8 block; any structural problem rejects the whole block."""
    openings = list(RESULT_FENCE.finditer(text))
    if not openings:
        return None, "missing qa-results block"
    if len(openings) > 1:
        return None, "more than one qa-results block"
    fence = openings[0].group(1)
    closing = re.compile(rf"^{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", re.MULTILINE).search(text, openings[0].end())
    if closing is None:
        return None, "unterminated qa-results block"
    try:
        data = json.loads(text[openings[0].end():closing.start()])
    except ValueError:
        return None, "qa-results block is not valid JSON"
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        return None, "qa-results block lacks its scenarios"
    if data.get("section") != section:
        return None, "qa-results block is for another section"

    rows: dict[str, JSON] = {}
    for item in data["scenarios"]:
        row = _outcome(item, section)
        sid = item.get("id") if isinstance(item, dict) else None
        edges = item.get("edges", []) if isinstance(item, dict) else None
        if row is None or not isinstance(sid, str) or not isinstance(edges, list):
            return None, "qa-results block has an invalid scenario entry"
        if sid not in planned:
            return None, "qa-results block reports an unassigned scenario"
        if sid in rows:
            return None, "qa-results block repeats a scenario"
        parsed: dict[int, JSON] = {}
        for edge in edges:
            outcome = _outcome(edge, section)
            number = edge.get("n") if isinstance(edge, dict) else None
            if outcome is None or isinstance(number, bool) or not isinstance(number, int):
                return None, "qa-results block has an invalid edge entry"
            if not 1 <= number <= planned[sid]:
                return None, "qa-results block reports an unplanned edge"
            if number in parsed:
                return None, "qa-results block repeats an edge"
            parsed[number] = outcome
        rows[sid] = {**row, "edges": parsed}

    return rows, None


def _outcome(item: object, section: str) -> JSON | None:
    """Validate one C8 result; ``None`` when any field has the wrong shape."""
    if not isinstance(item, dict) or item.get("status") not in STATUSES:
        return None
    observed = item.get("observed_status")
    crash = item.get("crash", False)
    kind = item.get("kind")
    missing = item.get("missing", [])
    skip_reason = item.get("skip_reason")
    refutation = item.get("refutation")
    valid = (
        (observed is None or (isinstance(observed, int) and not isinstance(observed, bool) and 100 <= observed <= 599))
        and isinstance(crash, bool) and (kind is None or kind in KINDS)
        and isinstance(missing, list) and all(isinstance(name, str) for name in missing)
        and (skip_reason is None or isinstance(skip_reason, str)) and (refutation is None or isinstance(refutation, str))
    )
    if not valid:
        return None

    return {
        "status": item["status"], "observed_status": None if section == "FE" else observed, "crash": crash,
        "kind": kind, "missing": missing, "skip_reason": skip_reason, "refutation": refutation,
    }


def _drop_reason(
    state: JSON, scenario: Scenario, qa: str, key: object, block: IssueBlock | None,
    claims: Mapping[str, list[str]], record: JSON | None, floor: int,
) -> str | None:
    if block is not None and block.rejected:
        return "rejected by user"
    if block is None or block.severity not in SEVERITIES:
        return "incomplete fields"
    if not block.located:
        return "needs manual location"
    if not (block.problem and block.remediation):
        return "incomplete fields"
    if not isinstance(key, str) or key not in _keys(scenario) or claims.get(key) != [qa]:
        return "needs manual assertion mapping"
    if record is None or record["result"] not in FAILING:
        return "assertion not failing"
    if SEVERITIES.index(block.severity) < floor:
        return "below min_severity"
    if key == scenario.id and qa in state["auth_gated_issues"]:
        return "auth-gated main flow"
    return None


def failures_at_floor(run: Run) -> bool:
    """Whether a failing scenario has an issue at or above ``min_severity``.

    An issue whose severity is not rendered yet counts, so a missing report
    never ends the loop as green.
    """
    state = run.state
    blocks = _issue_blocks(run.report_text())
    claims = _claims(state)
    floor = SEVERITIES.index(run.policy["min_severity"])
    for sid, verdict in state["current"].items():
        if verdict != "fail":
            continue
        owners = [
            qa for key, record in state["assertions"].items()
            if _scenario_of(key) == sid and record["result"] in FAILING for qa in claims.get(key, [])
        ]
        if not owners:
            return True
        for qa in owners:
            block = blocks.get(qa)
            if block is None or block.severity not in SEVERITIES or SEVERITIES.index(block.severity) >= floor:
                return True
    return False


def _budget_exhausted(run: Run) -> str | None:
    state = run.state
    if state["iteration"] >= run.budget["iterations"]:
        return "max iterations reached"
    if state["dispatch_count"] >= run.budget["dispatches"]:
        return "dispatch budget exhausted"
    if time.time() - float(run.record["started"]) >= float(run.budget["minutes"]) * 60:
        return "time budget exhausted"
    return None


def _budget_left(run: Run) -> int:
    return max(0, int(run.budget["dispatches"]) - int(run.state["dispatch_count"]))


def _record_dispatch(state: JSON, record: JSON) -> str:
    state["dispatch_count"] += 1
    dispatch = f"D{len(state['dispatches']) + 1:03d}"
    state["dispatches"][dispatch] = {**record, "time": time.time()}
    return dispatch


def _dispatch_number(dispatch: str) -> int:
    match = re.fullmatch(r"D(\d+)", dispatch)
    return int(match.group(1)) if match else 0


def _hardcoding_warnings(run: Run, record: JSON) -> list[str]:
    """Step 3d: added string literals equal to a request-payload value of the issue's BE scenario."""
    scenarios = {scenario.id: scenario for scenario in run.plan().scenarios}
    scenario = scenarios.get(_scenario_of(record["key"]))
    if scenario is None or scenario.section != "BE":
        return []
    values = _payload_values(scenario)
    if not values:
        return []
    before: dict[str, str] = record["dirty_before"]
    now = _dirty(run.repo)
    touched = ({path for path in now if before.get(path) != now[path]} | {path for path in before if path not in now}) - run.artifacts()
    if not touched:
        return []
    literals: set[str] = set()
    for line in _git(run.repo, "diff", "--unified=0", "HEAD", "--", *sorted(touched)).splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            literals.update(first or second or third for first, second, third in LITERAL.findall(line[1:]))

    return [
        f"{record['qa']}: Possible hardcoding: added literal matches scenario request-payload value {json.dumps(value)}"
        for value in sorted(literals & values)
    ]


def _payload_values(scenario: Scenario) -> set[str]:
    """String values of the main flow's payload field; placeholders never count."""
    text = scenario.main_flow
    matches = list(FIELD.finditer(text))
    values: set[str] = set()
    for index, match in enumerate(matches):
        if match.group(1).strip().lower() not in PAYLOAD_FIELDS:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = CODE_FENCE.sub("", text[match.start(2):end]).strip().strip("`").strip()
        try:
            values.update(_json_strings(json.loads(content)))
        except ValueError:
            values.update(first or second or third for first, second, third in LITERAL.findall(content))
    return {value for value in values if value and not TOKEN.search(value)}


def _json_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def _refresh_guards(state: JSON, plan: Plan) -> None:
    """Recompute unverified and auth-gated membership for every mapped issue with data."""
    scenarios = {scenario.id: scenario for scenario in plan.scenarios}
    unverified = set(state["unverified_issues"])
    gated = set(state["auth_gated_issues"])
    for qa, key in state["issue_assertion"].items():
        scenario = scenarios.get(_scenario_of(key))
        if scenario is None or key not in _keys(scenario):
            continue
        if _assertion(scenario, key).unverified:
            unverified.add(qa)
        else:
            unverified.discard(qa)
        record = state["assertions"].get(key)
        if record is None:
            continue
        # Only a bare-ID (main-flow) issue can be auth-gated; edge issues stay eligible.
        if key == scenario.id and record["result"] == "AUTH":
            gated.add(qa)
        else:
            gated.discard(qa)
    state["unverified_issues"] = _sorted_ids(unverified)
    state["auth_gated_issues"] = _sorted_ids(gated)


def _claims(state: JSON) -> dict[str, list[str]]:
    claims: dict[str, list[str]] = {}
    for qa, key in state["issue_assertion"].items():
        claims.setdefault(key, []).append(qa)
    return {key: _sorted_ids(owners) for key, owners in claims.items()}


def _highest_issue(run: Run) -> int:
    """The largest QA number in the report headings, Loop History and sidecar."""
    state = run.state
    text = run.report_text()
    identifiers = set(state["issue_assertion"]) | {qa for owners in state["scenario_issues"].values() for qa in owners}
    identifiers |= set(HEADING_ISSUE.findall(text))
    history = LOOP_HISTORY.search(text)
    numbers = [_issue_number(qa) for qa in identifiers]
    if history is not None:
        numbers.extend(int(number) for number in ISSUE_NUMBER.findall(history.group(1)))
    return max(numbers, default=0)


def _issue_number(qa: str) -> int:
    match = ISSUE_NUMBER.search(qa)
    return int(match.group(1)) if match else 0


def _sorted_ids(ids: Iterable[object]) -> list[str]:
    return sorted({qa for qa in ids if isinstance(qa, str)}, key=lambda qa: (_issue_number(qa), qa))


def _keys(scenario: Scenario) -> list[str]:
    return [scenario.id, *(f"{scenario.id} (edge {number})" for number in range(1, len(scenario.edges) + 1))]


def _scenario_of(key: str) -> str:
    match = ASSERTION_KEY.fullmatch(key)
    return match.group(1) if match else key


def _assertion(scenario: Scenario, key: str) -> Assertion:
    match = ASSERTION_KEY.fullmatch(key)
    edge = match.group(2) if match else None
    return scenario.edges[int(edge) - 1] if edge else scenario.expected


def _forget(entries: dict[str, Any], sid: str) -> None:
    for key in [key for key in entries if key == sid or key.startswith(f"{sid} (edge ")]:
        del entries[key]


def _ordered(ids: Iterable[str]) -> list[str]:
    def order(sid: str) -> tuple[int, int, str]:
        match = re.fullmatch(r"(FE|BE)-(\d+)", sid)
        return (0 if sid.startswith("FE") else 1, int(match.group(2)) if match else 0, sid)
    return sorted(set(ids), key=order)


def _issue_blocks(text: str) -> dict[str, IssueBlock]:
    """Parse issue blocks the way fix and fix-report bound them; the first block of an ID wins."""
    blocks: dict[str, IssueBlock] = {}
    for match in ISSUE_HEADING.finditer(text):
        end = BLOCK_END.search(text, match.end())
        body = text[match.end():end.start() if end else len(text)]
        scenario = _field(body, "Scenario")
        reference = SCENARIO_REFERENCE.search(scenario) if scenario else None
        expected = EXPECTED_LINE.search(body)
        blocks.setdefault(match.group(2), IssueBlock(
            severity=match.group(1).upper(), status=_field(body, "Status"), location=_location(_field(body, "Location")),
            problem=_field(body, "Problem") is not None, remediation=_field(body, "Remediation") is not None,
            scenario=reference.group(0) if reference else None, expected=expected.group(1).strip() if expected else None,
        ))
    return blocks


def _field(body: str, name: str) -> str | None:
    match = re.search(rf"^(?:[-*][ \t]*)?\*\*{name}:\*\*[ \t]*(.*)$", body, re.MULTILINE)
    return match.group(1).strip() if match else None


def _location(value: str | None) -> str | None:
    """Two-clause read rule: the first backticked token, else the first bare token."""
    if value is None:
        return None
    backticked = re.search(r"`([^`]*)`", value)
    if backticked:
        return backticked.group(1).strip()
    tokens = value.split()
    return tokens[0] if tokens else None


def _prepare_sidecar(
    repo: Path, plan: Plan, plan_hash: str, run_id: str, pre_loop: dict[str, str], generated: bool,
) -> tuple[str, Path, Path, JSON]:
    """Resolve the four idempotency cases (loop.md Step 1.2) and build the run's sidecar."""
    reports = repo / "docs/testing/reports"
    topic = _topic(plan.path)
    sidecar = reports / f"{topic}-loop-state.json"
    newest = _newest_report(reports, topic)
    fresh = reports / f"{datetime.now().astimezone().date().isoformat()}-{topic}-report.md"
    carried: JSON = {}

    if sidecar.exists():
        stored = _stored_sidecar(sidecar)
        previous = _stored_report(reports, stored) or newest
        if stored is not None and stored.get("plan_sha256") == plan_hash:
            idempotency = "reuse"
            report = previous or fresh
            carried = {key: stored[key] for key in PERSISTENT if key in stored}
            _reconstruct(carried, report, plan)
        else:
            idempotency = "rebaseline"
            os.replace(sidecar, sidecar.with_suffix(".bak"))
            if previous is not None:
                # Fresh IDs start at QA-001, so the old report must leave the ID union.
                os.replace(previous, previous.with_suffix(".bak"))
            report = fresh
    elif newest is not None:
        idempotency = "adopt"
        report = newest
        carried = {"scenario_issues": {}, "issue_assertion": {}}
        prior_text = newest.read_text()
        carried["auto_generated"] = bool(re.search(r"^- Plan provenance: auto-generated$", prior_text, re.MULTILINE))
        for qa, block in _issue_blocks(prior_text).items():
            if block.scenario is not None:
                carried["scenario_issues"].setdefault(block.scenario, []).append(qa)
        _reconstruct(carried, report, plan)
    else:
        idempotency = "fresh"
        report = fresh

    state: JSON = {
        "plan_sha256": plan_hash, "plan_path": _relative(repo, plan.path), "report_file": _relative(repo, report),
        "topic": topic, "created": datetime.now().astimezone().date().isoformat(), "run_id": run_id,
        "scenario_issues": {}, "issue_assertion": {}, "scenario_kind": {}, "scenario_reason": {},
        "unverified_issues": [], "auth_gated_issues": [], "need_info": {}, "baseline": {}, "current": {},
        "assertions": {}, "auto_generated": generated, "pre_loop_dirty": sorted(pre_loop), "pre_loop": pre_loop,
        "fix_touched_files": [],
        "dispatch_count": 0, "dispatches": {}, "iteration": 0, "open_iteration": None, "loop_end": None,
        "iterations": [],
    }
    state.update(carried)
    if generated:
        state["auto_generated"] = True

    return idempotency, sidecar, report, state


def _reconstruct(carried: JSON, report: Path, plan: Plan) -> None:
    """Map unmapped issue IDs to assertion keys from Scenario and Expected, only when unique."""
    issue_assertion: dict[str, str] = carried.setdefault("issue_assertion", {})
    scenario_issues: dict[str, list[str]] = carried.setdefault("scenario_issues", {})
    unmapped = [qa for owners in scenario_issues.values() for qa in owners if qa not in issue_assertion]
    if not unmapped:
        return
    try:
        blocks = _issue_blocks(report.read_text())
    except FileNotFoundError:
        return
    scenarios = {scenario.id: scenario for scenario in plan.scenarios}
    claimed = set(issue_assertion.values())
    found: dict[str, list[str]] = {}
    for qa in unmapped:
        block = blocks.get(qa)
        scenario = scenarios.get(block.scenario) if block is not None and block.scenario else None
        if block is None or scenario is None or not block.expected or qa not in scenario_issues.get(scenario.id, []):
            continue
        matches = _matching(scenario, block.expected)
        if len(matches) == 1 and matches[0] not in claimed:
            found.setdefault(matches[0], []).append(qa)
    for key, owners in found.items():
        if len(owners) == 1:
            issue_assertion[owners[0]] = key


def _matching(scenario: Scenario, expected: str) -> list[str]:
    def normalize(text: str) -> str:
        return " ".join(text.replace("`", "").split())

    wanted = normalize(expected)
    options = [(key, normalize(_assertion(scenario, key).text)) for key in _keys(scenario)]
    exact = [key for key, text in options if text and text == wanted]
    if exact:
        return exact
    return [key for key, text in options if text and (text in wanted or wanted in text)]


def _stored_sidecar(path: Path) -> JSON | None:
    """A prior sidecar whose kept fields have the right types; anything else rebaselines."""
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("plan_sha256"), str):
        return None
    if any(key in value and not isinstance(value[key], kind) for key, kind in PERSISTENT.items()):
        return None
    if not _valid_issue_maps(value):
        return None
    return value


def _valid_state(value: object) -> TypeGuard[JSON]:
    if not isinstance(value, dict):
        return False
    kinds: dict[str, type | tuple[type, ...]] = {**PERSISTENT, **RUN_SCOPED}
    return all(isinstance(value.get(key), kind) for key, kind in kinds.items()) and _valid_issue_maps(value)


def _valid_issue_maps(value: JSON) -> bool:
    scenario_issues = value.get("scenario_issues", {})
    issue_assertion = value.get("issue_assertion", {})
    return (
        all(isinstance(owners, list) and all(isinstance(qa, str) for qa in owners) for owners in scenario_issues.values())
        and all(isinstance(key, str) for key in issue_assertion.values())
    )


def _stored_report(reports: Path, stored: JSON | None) -> Path | None:
    """The sidecar's report, only when it is an existing report of this directory."""
    name = stored.get("report_file") if stored is not None else None
    if not isinstance(name, str) or not name.endswith("-report.md"):
        return None
    # Only the file name is taken from the sidecar, so it cannot point outside the reports directory.
    candidate = reports / Path(name).name
    return candidate if candidate.is_file() else None


def _newest_report(reports: Path, topic: str) -> Path | None:
    pattern = re.compile(rf"\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(topic)}-report\.md\Z")
    found = [path for path in reports.glob("*-report.md") if pattern.fullmatch(path.name) and path.is_file()]
    return max(found, key=lambda path: (path.stat().st_mtime_ns, path.name), default=None)


def _topic(plan: Path) -> str:
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", plan.stem)
    stem = re.sub(r"-(?:test-)?plan$", "", stem)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "qa"


def _relative(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def _plan_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args], cwd=repo, capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConfigError("git operation failed") from error
    if result.returncode:
        raise ConfigError("git operation failed")
    return result.stdout


def _dirty(repo: Path) -> dict[str, str]:
    """Tracked paths modified against HEAD, each with a content fingerprint."""
    names = [name for name in _git(repo, "diff", "--name-only", "-z", "HEAD").split("\0") if name]
    return {name: _fingerprint(repo / name) for name in names}


def _fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"


def _origin_text(url: str) -> str:
    scheme, host, port = parse_origin(url, origin_only=True)
    return f"{scheme}://{host}:{port}"


def _tmp_root() -> Path:
    return Path(os.environ.get("TMPDIR") or "/tmp")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")


def _lock_data(path: Path) -> JSON | None:
    """A lock's holder; an unreadable lock is stale (``{}``), a missing one ``None``."""
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _live(data: JSON, now: float) -> bool:
    started = data.get("started")
    limit = data.get("limit_minutes")
    if isinstance(started, bool) or isinstance(limit, bool):
        return False
    if not isinstance(started, (int, float)) or not isinstance(limit, (int, float)):
        return False
    return now < started + limit * 60


def _create_run_directory(tmp: Path) -> tuple[str, Path]:
    for _ in range(8):
        run_id = secrets.token_hex(4)
        directory = tmp / f"qa-run-{run_id}"
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        directory.chmod(0o700)
        return run_id, directory
    raise ConfigError("cannot create a run directory")


def _owned_directory(directory: Path) -> bool:
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


def _remove_run_directory(directory: Path) -> None:
    if RUN_DIRECTORY.fullmatch(directory.name) and _owned_directory(directory):
        shutil.rmtree(directory)


def _remove_stale(tmp: Path, repo: Path, now: float) -> None:
    """Best-effort removal of this repository's run directories older than 24 h."""
    for directory in tmp.glob("qa-run-*"):
        if not RUN_DIRECTORY.fullmatch(directory.name) or not _owned_directory(directory):
            continue
        try:
            record = json.loads((directory / "run.json").read_text())
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("repo") != str(repo):
            continue
        started = record.get("started")
        if isinstance(started, (int, float)) and now - started > STALE_SECONDS:
            shutil.rmtree(directory, ignore_errors=True)


def _load_json(path: Path) -> object:
    """The parsed file, ``None`` when absent; invalid JSON is a stop."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw)
    except ValueError as error:
        raise ConfigError(f"{path.name} is not valid JSON") from error


def _write_json(path: Path, value: object, mode: int | None = None) -> None:
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode(), mode)
