"""Render QA findings and coverage from engine state, never tester narration.

Issue prose is supplied by the orchestrator; assertion grounding, refutation,
verdicts, account cleanup and authoritative Fixed statuses belong to the engine.
Prose cannot inject report fields; tester text is flattened to one line.
Validation recognizes every Python line separator; final Status writes leave
issue bodies unchanged.
Carried metadata is read only before Category, and only a code-review rewritten
Location overrides fresh orchestrator evidence.
Missing gap kinds display as unspecified; auth-unverified counts as Skip.
Only remaining failures inherit the loop's budget or no-progress stop reason.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import time
from typing import Any

from av_config.errors import InvalidConfig
from av_config.files import atomic_write
from qa_engine.accounts import ledger
from qa_engine.common import BLOCK_END
from qa_engine.common import LOOP_HISTORY
from qa_engine.common import METADATA_FIELDS
from qa_engine.common import REPORT_FIELD
from qa_engine.common import SEVERITIES
from qa_engine.common import assertion_keys
from qa_engine.common import plan_assertion
from qa_engine.common import read_object
from qa_engine.common import report_blocks
from qa_engine.common import report_field
from qa_engine.common import report_fields
from qa_engine.common import report_header
from qa_engine.config import Config
from qa_engine.config import DATABASE_NAMES
from qa_engine.plan import Plan
from qa_engine.plan import persona_token
from qa_engine.plan import run_plan
from qa_engine.models import Run
from qa_engine.models import StateStop
from qa_engine.schema import TesterDispatch
from qa_engine.candidates import failures_at_floor
from qa_engine.iterations import iteration_close
from qa_engine.iterations import record_final
from qa_engine.verdicts import scenario_kind

JSON = dict[str, Any]
VERDICTS = ("pass", "fail", "skip", "need-info")
LABELS = {"pass": "Pass", "fail": "Fail", "skip": "Skip", "need-info": "Need info", "auth-unverified": "Auth-unverified"}
DECISION_FIELDS = ("Decision", "Decision-retired", "Verification-plan", "Decision-pin", "Dispatch", "Verification")
ACCOUNTS = re.compile(r"^- Accounts:[^\n]*$", re.MULTILINE)
SUMMARY = re.compile(r"^## Summary[ \t]*\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
REQUIRED_ISSUE_FIELDS = frozenset({"qa", "title", "severity", "location", "actual", "impact", "remediation"})
OPTIONAL_ISSUE_FIELDS = frozenset({"severity_reason", "response", "screenshot"})
HEADING_FIELDS = ("qa", "title", "severity", "location")
PROSE_FIELDS = ("actual", "impact", "remediation", "response", "screenshot")


def _multiline(entry: JSON, fields: tuple[str, ...]) -> bool:
    return any("\n" in entry[name] or "\r" in entry[name] or len(entry[name].splitlines()) > 1 for name in fields if name in entry)


def _prose_lines(entry: JSON) -> Iterator[str]:
    for name in PROSE_FIELDS:
        if name in entry:
            yield from entry[name].splitlines()


ENTRY_RULES: tuple[tuple[Callable[[JSON], bool], str], ...] = (
    (lambda entry: any(not entry[name].strip() for name in REQUIRED_ISSUE_FIELDS - {"impact"}), "required issue text is empty"),
    (lambda entry: _multiline(entry, HEADING_FIELDS), "heading and location must occupy one line"),
    (lambda entry: "`" in entry["location"] or any(BLOCK_END.search(line) for line in _prose_lines(entry)), "prose cannot contain report block boundaries"),
    (lambda entry: any(REPORT_FIELD.search(line.lstrip(" \t")) for line in _prose_lines(entry)), "prose cannot contain report field lines"),
    (lambda entry: _multiline(entry, ("actual", "response", "screenshot")), "actual, response and screenshot must occupy one line"),
    (lambda entry: entry["severity"] not in SEVERITIES, "unknown severity"),
)


def _validate_issue(entry: object, run: Run, entries: Mapping[str, JSON]) -> JSON:
    if not isinstance(entry, dict) or not REQUIRED_ISSUE_FIELDS <= entry.keys() or entry.keys() - REQUIRED_ISSUE_FIELDS - OPTIONAL_ISSUE_FIELDS:
        raise InvalidConfig("issues: invalid issue fields")
    if any(not isinstance(item, str) for item in entry.values()):
        raise InvalidConfig("issues: every issue field must be text")
    if entry["qa"] not in run.state["issue_assertion"]:
        raise InvalidConfig("issues: QA id was not assigned")
    if entry["qa"] in entries:
        raise InvalidConfig("issues: duplicate QA id")
    for broken, message in ENTRY_RULES:
        if broken(entry):
            raise InvalidConfig(f"issues: {message}")
    return entry


def _validate_issue_severity(entry: JSON, run: Run, plan: Plan) -> None:
    key = run.state["issue_assertion"][entry["qa"]]
    _, assertion = plan_assertion(plan, key)
    record = run.state["assertions"].get(key)
    observed = record["observed_status"] if record is not None else None
    critical = (record is not None and record["crash"]) or (observed is not None and observed >= 500)
    severity = entry["severity"]
    if critical:
        if severity != "CRITICAL":
            raise InvalidConfig("issues: observed server error or crash requires CRITICAL")
        return
    if assertion.unverified and severity != "LOW":
        raise InvalidConfig("issues: an unverified assertion cannot exceed LOW")
    if severity == "CRITICAL" and entry.get("severity_reason") not in {"security-bypass", "data-loss"}:
        raise InvalidConfig("issues: CRITICAL requires security-bypass or data-loss evidence in actual")


def _read_issues(path: Path, run: Run, plan: Plan) -> dict[str, JSON]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as error:
        raise InvalidConfig("issues: unreadable or invalid JSON list") from error
    if not isinstance(value, list):
        raise InvalidConfig("issues: expected a JSON list")
    entries: dict[str, JSON] = {}
    for item in value:
        entry = _validate_issue(item, run, entries)
        _validate_issue_severity(entry, run, plan)
        entries[entry["qa"]] = entry
    return entries


def _issue(entry: JSON, key: str, plan: Plan, run: Run, previous: str) -> str:
    scenario, assertion = plan_assertion(plan, key)
    carried: dict[str, str] = {}
    decisions: list[str] = []
    for name, _, line in report_fields(report_header(previous)):
        if name not in METADATA_FIELDS:
            continue
        if name in carried:
            continue
        carried[name] = line
        if name in DECISION_FIELDS:
            decisions.append(line)
    lines = [f"### [{entry['severity']}] {entry['qa']}: {entry['title']}"]
    if "Status" in carried:
        lines.append(carried["Status"])
    lines.extend(decisions)
    location = carried.get("Location", "")
    if " (was: " not in location:
        location = f"**Location:** `{entry['location']}`"
    record = run.state["assertions"].get(key)
    refutation = record["refutation"] if record is not None else None
    lines.extend(["", f"**ID:** {entry['qa']}", location, "**Category:** Testing", "", "**Problem:**",
                  f"- Expected: {assertion.text}", f"- Actual: {entry['actual']}"])
    if refutation is not None:
        lines.append(f"- Refutation: {' '.join(refutation.split())}")
    if entry["impact"]:
        lines.extend(["", "**Impact:**", entry["impact"]])
    lines.extend(["", "**Remediation:**", entry["remediation"], "", f"**Scenario:** {key}"])
    extra = "response" if scenario.section == "BE" else "screenshot"
    if extra in entry:
        lines.append(f"**{extra.title()}:** {entry[extra]}")
    return "\n".join(lines)


def _verdicts(run: Run, plan: Plan) -> dict[str, str]:
    return {scenario.id: run.state["current"].get(scenario.id, "skip") for scenario in plan.scenarios}


def _counts(verdicts: Mapping[str, str]) -> str:
    counts = Counter("skip" if verdict == "auth-unverified" else verdict for verdict in verdicts.values())
    return f"- Total: {len(verdicts)} | " + " | ".join(f"{LABELS[name]}: {counts[name]}" for name in VERDICTS)


def accounts_line(run: Run) -> str:
    """List persona names only; a failed/pending cleanup is always left."""
    config = Config(run.repo)
    with ledger(run, config) as accounts:
        records = [row for row in accounts.records if row.get("run_id") == run.run_id and row.get("status") != "conflict"]
    names: dict[str, list[bool]] = {}
    for row in records:
        names.setdefault(str(row["persona"]), []).append(bool(row.get("deleted")))
    personas = [f"{name} (provisioned; {'deleted' if all(names[name]) else 'left'})" for name in sorted(names)]
    private = read_object(run.directory / "accounts.private.json")
    personas.extend(f"{name} (static; left)" for name in sorted(private) if private[name].get("static"))
    return "- Accounts: " + (", ".join(personas) or "none")


def refresh_accounts(run: Run) -> JSON:
    """Rewrite only the Summary's Accounts line, even after config drift."""
    text = run.report_text()
    section = SUMMARY.search(text)
    if section is None:
        raise StateStop("report unavailable: render a report before updating accounts")
    line = accounts_line(run)
    summary = section[0]
    if ACCOUNTS.search(summary):
        summary = ACCOUNTS.sub(lambda _: line, summary, count=1)
    else:
        heading, separator, body = summary.partition("\n")
        summary = heading + separator + line + "\n" + body
    updated = text[:section.start()] + summary + text[section.end():]
    atomic_write(Path(run.record["report"]), updated.encode())
    return {"report": run.state["report_file"]}


def _gaps(run: Run) -> list[str]:
    groups: dict[str, tuple[set[str], list[str]]] = {}
    for key, gap in run.state["need_info"].items():
        names, keys = groups.setdefault(gap.get("kind") or "unspecified", (set(), []))
        names.update(" ".join(name.split()) for name in gap["missing"])
        keys.append(key)
    return [f"- {kind}: {', '.join(f'`{name}`' for name in sorted(names)) or 'no identifier supplied'} — {', '.join(keys)}"
            for kind, (names, keys) in sorted(groups.items())]


def _details(run: Run, plan: Plan, verdicts: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    for scenario in plan.scenarios:
        verdict = verdicts[scenario.id]
        label = "Skip" if verdict == "auth-unverified" else LABELS[verdict]
        line = f"### {label}: {scenario.id}: {scenario.title}"
        if verdict == "fail":
            issues = run.state["scenario_issues"].get(scenario.id, [])
            if issues:
                line += " — see " + ", ".join(issues)
        elif verdict != "pass":
            gaps = [f"{key}: {gap.get('kind') or 'unspecified'}: {', '.join(' '.join(name.split()) for name in gap['missing'])}" for key, gap in run.state["need_info"].items()
                    if key == scenario.id or key.startswith(scenario.id + " (edge ")]
            reason = run.state["scenario_reason"].get(scenario.id, "cannot-confirm")
            line += f" ({'; '.join(gaps) if gaps else reason})"
        lines.append(line)
    return lines


def _require_final_dispatches(run: Run, verdicts: Mapping[str, str]) -> None:
    latest: dict[str, TesterDispatch] = {}
    for dispatch in run.state["dispatches"].values():
        if dispatch["kind"] == "tester":
            for sid in dispatch["scenarios"]:
                latest[sid] = dispatch
    if any(sid not in latest or latest[sid]["phase"] != "final" or not latest[sid]["ingested"] for sid in verdicts):
        raise StateStop("final report requires an ingested final dispatch for every scenario")


def _mark_fixed(blocks: dict[str, str], qa: str, today: str) -> bool:
    if qa not in blocks:
        return False
    status = report_field(blocks[qa], "Status")
    if status is not None and status.startswith("🚫 Rejected"):
        return False
    header = report_header(blocks[qa])
    lines = [line for line in header.split("\n") if report_field(line, "Status") is None]
    lines.insert(1, f"**Status:** ✅ Fixed ({today})")
    blocks[qa] = "\n".join(lines) + blocks[qa][len(header):]
    return True


def _final_fixed(run: Run, plan: Plan, blocks: dict[str, str], verdicts: Mapping[str, str]) -> list[str]:
    fixed: list[str] = []
    today = datetime.now().astimezone().date().isoformat()
    for scenario in plan.scenarios:
        if verdicts[scenario.id] == "pass":
            for qa in run.state["scenario_issues"].get(scenario.id, []):
                if _mark_fixed(blocks, qa, today):
                    fixed.append(qa)
    return fixed


def _final_remaining(run: Run, plan: Plan, blocks: Mapping[str, str], verdicts: Mapping[str, str]) -> list[str]:
    still: list[str] = []
    for scenario in plan.scenarios:
        if verdicts[scenario.id] == "pass":
            continue
        if not any(qa in blocks and report_field(blocks[qa], "Status") is None for qa in run.state["scenario_issues"].get(scenario.id, [])):
            continue
        note = ""
        record = run.state["assertions"].get(scenario.id)
        if record is not None and record["result"] == "PASS":
            note = {"need-info": " (edge need info)", "skip": " (edge skipped)"}.get(verdicts[scenario.id], "")
        still.append(scenario.id + note)
    return still


def _final(run: Run, plan: Plan, blocks: dict[str, str], verdicts: Mapping[str, str]) -> list[str]:
    _require_final_dispatches(run, verdicts)
    fixed = _final_fixed(run, plan, blocks, verdicts)
    record_final(run, verdicts, fixed, _final_remaining(run, plan, blocks, verdicts))
    return fixed


def _history(run: Run, previous: str, final: bool) -> list[str]:
    rows: list[str] = []
    prior = LOOP_HISTORY.search(previous)
    if prior:
        for line in prior[1].splitlines():
            if re.match(r"\|\s*(?:\d+|Final)\s*\|", line) and not (final and re.match(r"\|\s*Final\s*\|", line)):
                rows.append(line)
    for row in run.state["iterations"]:
        cells = [str(row["iteration"])]
        for column in (row["failing_in"], row["now_passing"], row["still_failing"], row["warnings"], row["regressions"]):
            cells.append(", ".join(value.replace("|", "\\|").replace("\n", " ") for value in column) or "—")
        cells.append(str(row["dispatch_count"]))
        line = "| " + " | ".join(cells) + " |"
        if line not in rows:
            rows.append(line)
    return ["| Iteration | Failing in | Now passing | Still failing | Warnings | Regressions | Dispatches |",
            "| :-- | :-- | :-- | :-- | :-- | :-- | :-- |", *rows]


def render_report(run: Run, issues: Path, *, final: bool = False) -> JSON:
    """Merge issue prose in plan order; only an authoritative final PASS closes issues."""
    if final and run.state["open_iteration"] is not None:
        iteration_close(run, decide=False)
    # An explicit abort cannot become verification through a later --final call.
    final = final and not run.stopped
    plan = run_plan(run, strict=final)
    previous = run.report_text()
    blocks = report_blocks(previous)
    entries = _read_issues(issues, run, plan)
    for qa, entry in entries.items():
        blocks[qa] = _issue(entry, run.state["issue_assertion"][qa], plan, run, blocks.get(qa, ""))
    missing = [qa for qa in run.state["issue_assertion"] if qa not in blocks]
    if missing:
        raise InvalidConfig("issues: prose required for assigned QA ids: " + ", ".join(missing))
    order: dict[str, int] = {}
    for scenario in plan.scenarios:
        for key in assertion_keys(scenario):
            order[key] = len(order)
    blocks = dict(sorted(blocks.items(), key=lambda item: (order.get(run.state["issue_assertion"].get(item[0], ""), len(order)), int(item[0][3:]))))
    verdicts = _verdicts(run, plan)
    fixed = _final(run, plan, blocks, verdicts) if final else []
    lines = [f"# Test Report: {run.state['topic']}", "", "## Summary", _counts(verdicts),
             f"- Plan: {run.state['plan_path']}",
             "- Plan provenance: " + ("auto-generated" if run.state["auto_generated"] else "existing"),
             f"- Date: {run.state['created']}", f"- Duration: {run.elapsed()}s", accounts_line(run)]
    gaps = _gaps(run)
    if gaps:
        lines.extend(["", "## Setup gaps", *gaps])
    lines.extend(["", "## Issues Found", ""])
    for block in blocks.values():
        lines.extend([block, ""])
    lines.extend(["## Detailed Results", "", *_details(run, plan, verdicts), "", "## Loop History", "", *_history(run, previous, final), ""])
    atomic_write(Path(run.record["report"]), "\n".join(lines).encode())
    return {"report": run.state["report_file"], "fixed": fixed, "written": True}


def _result(run: Run, verdicts: Mapping[str, str], elapsed: int) -> str:
    end = run.state["loop_end"]
    reason = end["reason"] if end is not None else ""
    if not run.plan_unchanged() or run.stopped:
        return "Stopped"
    if failures_at_floor(run):
        if "no progress" in reason or "regression" in reason:
            return "Stopped"
        if ("budget" in reason or "max iterations" in reason
                or run.state["dispatch_count"] >= run.budget["dispatches"] or run.state["iteration"] >= run.budget["iterations"]
                or elapsed >= run.budget["minutes"] * 60):
            return "Budget Exhausted"
        return "Fail"
    if verdicts and all(verdict in {"skip", "need-info"} for verdict in verdicts.values()) and not run.state["auto_generated"]:
        return "Stopped"
    return "Pass"


def _coverage(run: Run, plan: Plan, verdicts: Mapping[str, str]) -> tuple[list[str], bool]:
    kinds = {scenario.id: run.state["scenario_kind"].get(scenario.id, scenario_kind(scenario)) for scenario in plan.scenarios}
    passed = Counter(kinds[sid] for sid, verdict in verdicts.items() if verdict == "pass")
    reasons = Counter(run.state["scenario_reason"].get(sid, "cannot-confirm") for sid, verdict in verdicts.items() if verdict in {"skip", "need-info", "auth-unverified"})
    shallow = passed["feature"] == 0 and any(kind == "feature" and verdicts[sid] != "pass" for sid, kind in kinds.items())
    not_verified = Counter(verdicts.values())
    coverage = ["## Coverage", f"- Exercised: {passed['feature']} feature · {passed['sanity']} sanity · {passed['negative']} enforcement",
                f"- Not verified: auth-unverified {not_verified['auth-unverified']} · need-info {not_verified['need-info']} · "
                + " · ".join(f"{reason}{' SKIP' if reason == 'mutation-guard' else ''} {reasons[reason]}" for reason in ("mutation-guard", "tool-unavailable", "cannot-confirm", "transport")),
                "- Confidence: " + ("low — no feature behavior exercised" if shallow else "low — some assertions were not verified" if run.state["need_info"] or sum(not_verified[name] for name in ("skip", "need-info", "auth-unverified")) else "high")]
    return coverage, shallow


def _credential_key(run: Run, name: str) -> str:
    for names in DATABASE_NAMES.values():
        if name in names:
            return "env.database." + names[name]
    token = name.removeprefix("QA_")
    accounts = run.record["config"]["qa"].get("accounts", {})
    personas = set(accounts.get("personas", [])) | set(accounts.get("static", {}))
    recognized = persona_token(name, personas)
    if recognized:
        persona, field = recognized
        if field == "TOKEN" or field.startswith("COOKIE"):
            return "qa.accounts.login"
        if persona in accounts.get("static", {}):
            return f"qa.accounts.static.{persona}.{field.lower()}"
        return "qa.accounts.create" if field == "ID" else f"qa.accounts.{field.lower()}"
    if re.fullmatch(r".+_(TOKEN|COOKIE(?:_.+)?)", token):
        return "qa.accounts.login"
    return "env.values." + token


def _auth_unlock(run: Run, count: int) -> str:
    accounts = run.record["config"]["qa"].get("accounts", {})
    personas = sorted(set(accounts.get("personas", [])) | set(accounts.get("static", {})))
    credentials = ", ".join(f"`$QA_{name.upper()}_TOKEN` or `$QA_{name.upper()}_COOKIE`" for name in personas) or "`$QA_<P>_TOKEN` or `$QA_<P>_COOKIE`"
    return f"- auth-unverified ({count}): use the persona's {credentials} in the scenario, or add a `qa.accounts.login` recipe; re-run `/qa:run`."


def _gap_action(run: Run, kind: str) -> str:
    if kind == "credentials":
        keys = sorted({_credential_key(run, " ".join(name.split())) for item in run.state["need_info"].values()
                       if item["kind"] == "credentials" for name in item["missing"]})
        return "fix " + ", ".join(f"`{key}`" for key in keys) + " in `.av/config.toml` or `.av/local.toml`"
    return {"service": "fix/start the named service using `env.targets` and `env.services`",
            "fixture": "provide the named fixture or create it in the scenario's data preconditions",
            "tool": "install/enable the named tool"}.get(kind, "supply the named items listed under Setup gaps")


def _gap_unlocks(run: Run, verdicts: Mapping[str, str]) -> list[str]:
    if not run.state["need_info"]:
        return []
    hints = [f"- need-info ({sum(verdict == 'need-info' for verdict in verdicts.values())}):"]
    for gap in _gaps(run):
        kind = gap.split(":", 1)[0].removeprefix("- ")
        hints.append(f"  {gap} — {_gap_action(run, kind)}; re-run `/qa:run`.")
    return hints


def _backend_unavailable(run: Run, plan: Plan) -> bool:
    be = [scenario.id for scenario in plan.scenarios if scenario.section == "BE"]
    if not be:
        return False
    gaps = run.state["need_info"]
    assertions = run.state["assertions"]
    return (all(sid in gaps and gaps[sid]["kind"] == "service" for sid in be)
            or all(run.state["scenario_reason"].get(sid) == "transport" and (sid not in assertions or assertions[sid]["observed_status"] is None)
                   for sid in be))


def _unlock(run: Run, plan: Plan, verdicts: Mapping[str, str]) -> list[str]:
    reasons = Counter(run.state["scenario_reason"].values())
    hints: list[str] = []
    if reasons["mutation-guard"]:
        hints.append(f"- mutation-guard ({reasons['mutation-guard']}): set `qa.policy.mutations` in `.av/config.toml`; `allow` requires `qa.policy.disposable_data = true` and disposable test data.")
    if reasons["auth-unverified"] or run.state["auth_gated_issues"]:
        hints.append(_auth_unlock(run, reasons["auth-unverified"]))
    hints.extend(_gap_unlocks(run, verdicts))
    if reasons["tool-unavailable"]:
        hints.append(f"- tool-unavailable ({reasons['tool-unavailable']}): install/enable the missing browser, HTTP or database client.")
    if run.state["dispatch_count"] >= run.budget["dispatches"]:
        hints.append("- dispatch-exhausted: raise `qa.budget.dispatches` in `.av/config.toml`.")
    if run.state["iteration"] >= run.budget["iterations"]:
        hints.append("- iterations exhausted: raise `qa.budget.iterations` in `.av/config.toml`.")
    if _backend_unavailable(run, plan):
        hints.append("- No BE scenario returned an HTTP status at the configured `env.targets` origins — the dev stack may be down; check `env.services.health`/`up`.")
    return hints


def _stop_summary(run: Run, result: str) -> list[str]:
    if result != "Stopped":
        return []
    end = run.state["loop_end"]
    reason = (end["reason"] if end is not None else "") or ("plan changed mid-run (hash mismatch)" if not run.plan_unchanged() else "no executable verifier")
    lines = [f"- Stop reason: {reason}"]
    detail = end.get("detail") if end is not None else None
    if detail:
        lines.append(f"- Stop detail: {detail}")
    return lines


def _all_unverified(verdicts: Mapping[str, str]) -> bool:
    return bool(verdicts) and all(verdict in {"skip", "need-info"} for verdict in verdicts.values())


def _coverage_notice(run: Run, verdicts: Mapping[str, str], result: str, shallow: bool) -> list[str]:
    if _all_unverified(verdicts) and not run.stopped:
        return [_unverified_notice(run, verdicts)]
    if result == "Stopped" or failures_at_floor(run):
        return []
    if shallow and run.state["auto_generated"]:
        return ["All assertions passed, but coverage is shallow — no feature behavior was exercised (see Coverage). Low-confidence green: the plan was auto-generated and may not reflect runtime auth/setup."]
    lines = ["No failing assertions to fix. Check Coverage and Setup gaps for unverified scenarios."]
    if shallow:
        lines.append("Warning: shallow coverage — no feature behavior was exercised. This green reflects infrastructure and enforcement checks only.")
    return lines


def _unverified_notice(run: Run, verdicts: Mapping[str, str]) -> str:
    if not run.state["auto_generated"]:
        return "Error: No executable verifier — cannot gate (all scenarios marked SKIP or NEED_INFO). Check your test plan, Setup gaps and tool availability."
    if all(run.state["scenario_reason"].get(sid) == "mutation-guard" for sid in verdicts):
        return "Auto-generated plan is backend-write-only under the mutation guard — nothing executable here; rely on the unit/integration suite."
    return "Warning: All scenarios skipped or need setup for tooling/parse/prerequisite reasons, not mutation-guard — coverage is zero; verify the generated plan, Setup gaps and tool availability."


def _recovery_summary(run: Run, remaining: int) -> list[str]:
    lines: list[str] = []
    if remaining:
        lines.append("Use `/fix QA-NNN` to fix remaining issues by ID, or re-run `/qa:run` after adjusting `.av/config.toml` policy or budgets.")
    touched = sorted(set(run.state["fix_touched_files"]) - set(run.state["pre_loop_dirty"]))
    if touched:
        lines.append("To recover the loop's own edits: `git restore -- " + " ".join(shlex.quote(path) for path in touched)
                     + "` (scoped — restores only what the loop's fixes touched, never your pre-existing changes).")
    else:
        lines.append("The loop touched nothing eligible for scoped recovery.")
    overlap = sorted({path for row in run.state["iterations"] if "overlap" in row["result"] for path in row["result"]["overlap"]})
    if overlap:
        lines.append("Pre-existing-dirty files also edited by fixes are excluded from scoped recovery; reconcile manually: " + ", ".join(f"`{path}`" for path in overlap) + ".")
    return lines


def _issue_counts(blocks: Mapping[str, str]) -> tuple[int, int]:
    fixed = sum((report_field(block, "Status") or "").startswith("✅ Fixed") for block in blocks.values())
    remaining = sum(report_field(block, "Status") is None for block in blocks.values())
    return fixed, remaining


def render_summary(run: Run) -> str:
    """Render the severity-floor result, advisory coverage, unlocks and scoped recovery."""
    if run.state["open_iteration"] is not None:
        iteration_close(run, decide=False)
    plan = run_plan(run, strict=False)
    verdicts = _verdicts(run, plan)
    elapsed = max(0, int(time.time() - run.record["started"]))
    result = _result(run, verdicts, elapsed)
    blocks = report_blocks(run.report_text())
    fixed, remaining = _issue_counts(blocks)
    warnings = [warning for row in run.state["iterations"] for warning in row["warnings"]]
    regressions = {sid for row in run.state["iterations"] for sid in row["regressions"]}
    coverage, shallow = _coverage(run, plan, verdicts)
    lines = ["## Loop Summary", "", f"**Result:** {result}", "", "**Final Status:**", _counts(verdicts),
             f"- Fixed (Status written): {fixed}", f"- Remaining unfixed: {remaining}", f"- Warnings: {len(warnings)} (anti-hardcoding)",
             f"- Regressions: {len(regressions)}", "", *coverage, ""]
    lines.extend(_stop_summary(run, result))
    lines.extend(_coverage_notice(run, verdicts, result, shallow))
    lines.extend(["", "**Next steps to widen coverage:**", *_unlock(run, plan, verdicts), "", "**Budget Used:**",
                  f"- Dispatches: {run.state['dispatch_count']} / {run.budget['dispatches']}",
                  f"- Iterations: {run.state['iteration']} / {run.budget['iterations']}",
                  f"- Time: {elapsed // 60}m {elapsed % 60}s / {run.budget['minutes']}m", "", "**Next Steps:**"])
    lines.extend(_recovery_summary(run, remaining))
    if warnings:
        lines.extend(["", "**Warnings (manual review recommended):**", *(f"- {warning}" for warning in warnings)])
    lines.extend(["", "**Changes remain uncommitted for your control.**", ""])
    return "\n".join(lines)
