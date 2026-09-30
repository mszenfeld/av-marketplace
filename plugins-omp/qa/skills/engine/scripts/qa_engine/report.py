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
from collections.abc import Mapping
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import time
from typing import Any

from av_config import InvalidConfig
from av_config import atomic_write
from qa_engine.accounts import ledger
from qa_engine.accounts import read_object
from qa_engine.config import Config
from qa_engine.plan import Assertion
from qa_engine.plan import Plan
from qa_engine.plan import Scenario
from qa_engine.state import ASSERTION_KEY
from qa_engine.state import BLOCK_END
from qa_engine.state import ISSUE_HEADING
from qa_engine.state import LOOP_HISTORY
from qa_engine.state import SEVERITIES
from qa_engine.state import Run
from qa_engine.state import StateStop
from qa_engine.state import failures_at_floor
from qa_engine.state import iteration_close
from qa_engine.state import scenario_kind

JSON = dict[str, Any]
VERDICTS = ("pass", "fail", "skip", "need-info")
LABELS = {"pass": "Pass", "fail": "Fail", "skip": "Skip", "need-info": "Need info", "auth-unverified": "Auth-unverified"}
DECISION_FIELDS = ("Decision", "Decision-retired", "Verification-plan", "Decision-pin", "Dispatch", "Verification")
PROTECTED = re.compile(r"^\*\*(Status|Location|Decision|Decision-retired|Verification-plan|Decision-pin|Dispatch|Verification):\*\*[^\n]*$", re.MULTILINE)
PROSE_FIELD = re.compile(r"^\*\*[\w-]+:\*\*")
ACCOUNTS = re.compile(r"^- Accounts:[^\n]*$", re.MULTILINE)
SUMMARY = re.compile(r"^## Summary[ \t]*\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def _blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in ISSUE_HEADING.finditer(text):
        end = BLOCK_END.search(text, match.end())
        blocks.setdefault(match[2], text[match.start():end.start() if end else len(text)].rstrip())
    return blocks


def _header(block: str) -> str:
    return re.split(r"^\*\*Category:\*\*", block, maxsplit=1, flags=re.MULTILINE)[0]


def _field(block: str, field: str) -> str | None:
    match = re.search(rf"^\*\*{re.escape(field)}:\*\*[^\n]*$", _header(block), re.MULTILINE)
    return match[0] if match else None


def _assertion(plan: Plan, key: str) -> tuple[Scenario, Assertion]:
    match = ASSERTION_KEY.fullmatch(key)
    if match is not None:
        for scenario in plan.scenarios:
            if scenario.id == match[1]:
                if match[2] is None:
                    return scenario, scenario.expected
                number = int(match[2])
                if number <= len(scenario.edges):
                    return scenario, scenario.edges[number - 1]
    raise InvalidConfig("issues: assertion is not in the run's plan")


def _read_issues(path: Path, run: Run, plan: Plan) -> dict[str, JSON]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as error:
        raise InvalidConfig("issues: unreadable or invalid JSON list") from error
    if not isinstance(value, list):
        raise InvalidConfig("issues: expected a JSON list")
    entries: dict[str, JSON] = {}
    required = {"qa", "title", "severity", "location", "actual", "impact", "remediation"}
    optional = {"severity_reason", "response", "screenshot"}
    for entry in value:
        if not isinstance(entry, dict) or not required <= entry.keys() or entry.keys() - required - optional:
            raise InvalidConfig("issues: invalid issue fields")
        if any(not isinstance(item, str) for item in entry.values()):
            raise InvalidConfig("issues: every issue field must be text")
        qa = entry["qa"]
        if qa not in run.state["issue_assertion"]:
            raise InvalidConfig("issues: QA id was not assigned")
        if qa in entries:
            raise InvalidConfig("issues: duplicate QA id")
        if any(not entry[name].strip() for name in required - {"impact"}):
            raise InvalidConfig("issues: required issue text is empty")
        if any("\n" in entry[name] or "\r" in entry[name] or len(entry[name].splitlines()) > 1 for name in ("qa", "title", "severity", "location")):
            raise InvalidConfig("issues: heading and location must occupy one line")
        if "`" in entry["location"] or any(BLOCK_END.search(line) for name in ("actual", "impact", "remediation", "response", "screenshot") if name in entry for line in entry[name].splitlines()):
            raise InvalidConfig("issues: prose cannot contain report block boundaries")
        if any(PROSE_FIELD.search(line) for name in ("actual", "impact", "remediation", "response", "screenshot") if name in entry for line in entry[name].splitlines()):
            raise InvalidConfig("issues: prose cannot contain report field lines")
        if any("\n" in entry[name] or "\r" in entry[name] or len(entry[name].splitlines()) > 1 for name in ("actual", "response", "screenshot") if name in entry):
            raise InvalidConfig("issues: actual, response and screenshot must occupy one line")
        severity = entry["severity"]
        if severity not in SEVERITIES:
            raise InvalidConfig("issues: unknown severity")
        key = run.state["issue_assertion"][qa]
        _, assertion = _assertion(plan, key)
        result = run.state["assertions"].get(key, {})
        observed = result.get("observed_status")
        critical = result.get("crash", False) or (isinstance(observed, int) and observed >= 500)
        if critical and severity != "CRITICAL":
            raise InvalidConfig("issues: observed server error or crash requires CRITICAL")
        if not critical and assertion.unverified and severity != "LOW":
            raise InvalidConfig("issues: an unverified assertion cannot exceed LOW")
        if severity == "CRITICAL" and not critical and entry.get("severity_reason") not in {"security-bypass", "data-loss"}:
            raise InvalidConfig("issues: CRITICAL requires security-bypass or data-loss evidence in actual")
        entries[qa] = entry
    return entries


def _issue(entry: JSON, key: str, plan: Plan, run: Run, previous: str) -> str:
    scenario, assertion = _assertion(plan, key)
    carried: dict[str, str] = {}
    decisions: list[str] = []
    for match in PROTECTED.finditer(_header(previous)):
        name = match[1]
        if name in carried:
            continue
        carried[name] = match[0]
        if name in DECISION_FIELDS:
            decisions.append(match[0])
    lines = [f"### [{entry['severity']}] {entry['qa']}: {entry['title']}"]
    if "Status" in carried:
        lines.append(carried["Status"])
    lines.extend(decisions)
    location = carried.get("Location", "")
    if " (was: " not in location:
        location = f"**Location:** `{entry['location']}`"
    refutation = run.state["assertions"].get(key, {}).get("refutation")
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


def _elapsed(run: Run) -> int:
    # Report re-renders use recorded activity, not the rendering process's clock.
    latest = max((row.get("time", run.record["started"]) for row in run.state["dispatches"].values()), default=run.record["started"])
    return max(int(latest - run.record["started"]), max((row.get("elapsed_s", 0) for row in run.state["iterations"]), default=0))


def _final(run: Run, plan: Plan, blocks: dict[str, str], verdicts: Mapping[str, str]) -> list[str]:
    latest: dict[str, JSON] = {}
    for dispatch in run.state["dispatches"].values():
        if dispatch.get("kind") == "tester":
            for sid in dispatch["scenarios"]:
                latest[sid] = dispatch
    if any(sid not in latest or latest[sid]["phase"] != "final" or not latest[sid]["ingested"] for sid in verdicts):
        raise StateStop("final report requires an ingested final dispatch for every scenario")
    fixed: list[str] = []
    today = datetime.now().astimezone().date().isoformat()
    for scenario in plan.scenarios:
        if verdicts[scenario.id] != "pass":
            continue
        for qa in run.state["scenario_issues"].get(scenario.id, []):
            if qa not in blocks:
                continue
            status = _field(blocks[qa], "Status")
            if status is not None and status.removeprefix("**Status:**").strip().startswith("🚫 Rejected"):
                continue
            header = _header(blocks[qa])
            lines = [line for line in header.split("\n") if not line.startswith("**Status:**")]
            lines.insert(1, f"**Status:** ✅ Fixed ({today})")
            blocks[qa] = "\n".join(lines) + blocks[qa][len(header):]
            fixed.append(qa)
    still = []
    for scenario in plan.scenarios:
        open_issues = [qa for qa in run.state["scenario_issues"].get(scenario.id, []) if qa in blocks and _field(blocks[qa], "Status") is None]
        if verdicts[scenario.id] != "pass" and open_issues:
            note = ""
            if run.state["assertions"].get(scenario.id, {}).get("result") == "PASS":
                if verdicts[scenario.id] == "need-info":
                    note = " (edge need info)"
                elif verdicts[scenario.id] == "skip":
                    note = " (edge skipped)"
            still.append(scenario.id + note)
    regressions = [sid for sid, verdict in verdicts.items() if verdict == "fail" and run.state["baseline"].get(sid) == "pass"]
    row = {"iteration": "Final", "failing_in": [sid for sid, verdict in run.state["baseline"].items() if verdict == "fail"],
           "now_passing": [sid for sid, verdict in verdicts.items() if verdict == "pass" and run.state["baseline"].get(sid) != "pass"],
           "still_failing": still, "warnings": [], "regressions": regressions,
           "dispatch_count": sum(dispatch.get("kind") == "tester" and dispatch.get("phase") == "final" for dispatch in run.state["dispatches"].values()),
           "elapsed_s": _elapsed(run), "result": {"fixed": fixed, "regressions": regressions}}
    run.state["iterations"] = [old for old in run.state["iterations"] if old.get("iteration") != "Final"] + [row]
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
        for name in ("failing_in", "now_passing", "still_failing", "warnings", "regressions"):
            cells.append(", ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in row.get(name, [])) or "—")
        cells.append(str(row.get("dispatch_count", 0)))
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
    final = final and (run.state["loop_end"] or {}).get("decision") != "stop"
    plan = run.plan(strict=final)
    previous = run.report_text()
    blocks = _blocks(previous)
    entries = _read_issues(issues, run, plan)
    for qa, entry in entries.items():
        blocks[qa] = _issue(entry, run.state["issue_assertion"][qa], plan, run, blocks.get(qa, ""))
    missing = [qa for qa in run.state["issue_assertion"] if qa not in blocks]
    if missing:
        raise InvalidConfig("issues: prose required for assigned QA ids: " + ", ".join(missing))
    order: dict[str, int] = {}
    for scenario in plan.scenarios:
        for key in (scenario.id, *(f"{scenario.id} (edge {number})" for number in range(1, len(scenario.edges) + 1))):
            order[key] = len(order)
    blocks = dict(sorted(blocks.items(), key=lambda item: (order.get(run.state["issue_assertion"].get(item[0], ""), len(order)), int(item[0][3:]))))
    verdicts = _verdicts(run, plan)
    fixed = _final(run, plan, blocks, verdicts) if final else []
    lines = [f"# Test Report: {run.state['topic']}", "", "## Summary", _counts(verdicts),
             f"- Plan: {run.state['plan_path']}",
             "- Plan provenance: " + ("auto-generated" if run.state["auto_generated"] else "existing"),
             f"- Date: {run.state['created']}", f"- Duration: {_elapsed(run)}s", accounts_line(run)]
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
    end = run.state["loop_end"] or {}
    reason = str(end.get("reason", ""))
    if not run.plan_unchanged() or end.get("decision") == "stop":
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
    database = {"PGHOST": "host", "PGPORT": "port", "PGUSER": "user", "PGDATABASE": "name", "PGPASSWORD": "password",
                "MYSQL_HOST": "host", "MYSQL_TCP_PORT": "port", "MYSQL_USER": "user", "MYSQL_DATABASE": "name", "MYSQL_PWD": "password", "SQLITE_DB": "path"}
    if name in database:
        return "env.database." + database[name]
    token = name.removeprefix("QA_")
    accounts = run.record["config"]["qa"].get("accounts", {})
    personas = sorted(set(accounts.get("personas", [])) | set(accounts.get("static", {})), key=lambda persona: (-len(persona), persona))
    for persona in personas:
        prefix = persona.upper() + "_"
        if token.startswith(prefix):
            field = token.removeprefix(prefix)
            if field.startswith(("TOKEN", "COOKIE")):
                return "qa.accounts.login"
            if persona in accounts.get("static", {}):
                return f"qa.accounts.static.{persona}.{field.lower()}"
            return "qa.accounts.create" if field == "ID" else f"qa.accounts.{field.lower()}"
    if re.fullmatch(r".+_(TOKEN|COOKIE(?:_.+)?)", token):
        return "qa.accounts.login"
    return "env.values." + token


def _unlock(run: Run, plan: Plan, verdicts: Mapping[str, str]) -> list[str]:
    reasons = Counter(run.state["scenario_reason"].values())
    hints: list[str] = []
    if reasons["mutation-guard"]:
        hints.append(f"- mutation-guard ({reasons['mutation-guard']}): set `qa.policy.mutations` in `.av/config.toml`; `allow` requires `qa.policy.disposable_data = true` and disposable test data.")
    if reasons["auth-unverified"] or run.state["auth_gated_issues"]:
        accounts = run.record["config"]["qa"].get("accounts", {})
        personas = sorted(set(accounts.get("personas", [])) | set(accounts.get("static", {})))
        credentials = ", ".join(f"`$QA_{name.upper()}_TOKEN` or `$QA_{name.upper()}_COOKIE`" for name in personas) or "`$QA_<P>_TOKEN` or `$QA_<P>_COOKIE`"
        hints.append(f"- auth-unverified ({reasons['auth-unverified']}): use the persona's {credentials} in the scenario, or add a `qa.accounts.login` recipe; re-run `/qa:run`.")
    if run.state["need_info"]:
        hints.append(f"- need-info ({sum(verdict == 'need-info' for verdict in verdicts.values())}):")
        for gap in _gaps(run):
            kind = gap.split(":", 1)[0].removeprefix("- ")
            action = {"service": "fix/start the named service using `env.targets` and `env.services`",
                      "fixture": "provide the named fixture or create it in the scenario's data preconditions",
                      "tool": "install/enable the named tool",
                      "credentials": "fix the named `env.values` or `qa.accounts` source in `.av/config.toml` or `.av/local.toml`"}.get(
                          kind, "supply the named items listed under Setup gaps")
            if kind == "credentials":
                keys = sorted({_credential_key(run, " ".join(name.split())) for item in run.state["need_info"].values()
                               if item["kind"] == "credentials" for name in item["missing"]})
                action = "fix " + ", ".join(f"`{key}`" for key in keys) + " in `.av/config.toml` or `.av/local.toml`"
            hints.append(f"  {gap} — {action}; re-run `/qa:run`.")
    if reasons["tool-unavailable"]:
        hints.append(f"- tool-unavailable ({reasons['tool-unavailable']}): install/enable the missing browser, HTTP or database client.")
    if run.state["dispatch_count"] >= run.budget["dispatches"]:
        hints.append("- dispatch-exhausted: raise `qa.budget.dispatches` in `.av/config.toml`.")
    if run.state["iteration"] >= run.budget["iterations"]:
        hints.append("- iterations exhausted: raise `qa.budget.iterations` in `.av/config.toml`.")
    be = [scenario.id for scenario in plan.scenarios if scenario.section == "BE"]
    gaps = run.state["need_info"]
    if be and (all(gaps.get(sid, {}).get("kind") == "service" for sid in be)
               or all(run.state["scenario_reason"].get(sid) == "transport" and run.state["assertions"].get(sid, {}).get("observed_status") is None for sid in be)):
        hints.append("- No BE scenario returned an HTTP status at the configured `env.targets` origins — the dev stack may be down; check `env.services.health`/`up`.")
    return hints


def render_summary(run: Run) -> str:
    """Render the severity-floor result, advisory coverage, unlocks and scoped recovery."""
    if run.state["open_iteration"] is not None:
        iteration_close(run, decide=False)
    plan = run.plan(strict=False)
    verdicts = _verdicts(run, plan)
    elapsed = max(0, int(time.time() - run.record["started"]))
    result = _result(run, verdicts, elapsed)
    blocks = _blocks(run.report_text())
    fixed = sum((_field(block, "Status") or "").startswith("**Status:** ✅ Fixed") for block in blocks.values())
    remaining = sum(_field(block, "Status") is None for block in blocks.values())
    warnings = [warning for row in run.state["iterations"] for warning in row.get("warnings", [])]
    regressions = {sid for row in run.state["iterations"] for sid in row.get("regressions", [])}
    coverage, shallow = _coverage(run, plan, verdicts)
    lines = ["## Loop Summary", "", f"**Result:** {result}", "", "**Final Status:**", _counts(verdicts),
             f"- Fixed (Status written): {fixed}", f"- Remaining unfixed: {remaining}", f"- Warnings: {len(warnings)} (anti-hardcoding)",
             f"- Regressions: {len(regressions)}", "", *coverage, ""]
    if result == "Stopped":
        end = run.state["loop_end"] or {}
        reason = end.get("reason") or ("plan changed mid-run (hash mismatch)" if not run.plan_unchanged() else "no executable verifier")
        lines.append(f"- Stop reason: {reason}")
        if end.get("detail"):
            lines.append(f"- Stop detail: {end['detail']}")
    all_unverified = bool(verdicts) and all(verdict in {"skip", "need-info"} for verdict in verdicts.values())
    if all_unverified and (run.state["loop_end"] or {}).get("decision") != "stop":
        if run.state["auto_generated"]:
            if all(run.state["scenario_reason"].get(sid) == "mutation-guard" for sid in verdicts):
                lines.append("Auto-generated plan is backend-write-only under the mutation guard — nothing executable here; rely on the unit/integration suite.")
            else:
                lines.append("Warning: All scenarios skipped or need setup for tooling/parse/prerequisite reasons, not mutation-guard — coverage is zero; verify the generated plan, Setup gaps and tool availability.")
        else:
            lines.append("Error: No executable verifier — cannot gate (all scenarios marked SKIP or NEED_INFO). Check your test plan, Setup gaps and tool availability.")
    elif result != "Stopped" and not failures_at_floor(run):
        if shallow and run.state["auto_generated"]:
            lines.append("All assertions passed, but coverage is shallow — no feature behavior was exercised (see Coverage). Low-confidence green: the plan was auto-generated and may not reflect runtime auth/setup.")
        else:
            lines.append("No failing assertions to fix. Check Coverage and Setup gaps for unverified scenarios.")
            if shallow:
                lines.append("Warning: shallow coverage — no feature behavior was exercised. This green reflects infrastructure and enforcement checks only.")
    lines.extend(["", "**Next steps to widen coverage:**", *_unlock(run, plan, verdicts), "", "**Budget Used:**",
                  f"- Dispatches: {run.state['dispatch_count']} / {run.budget['dispatches']}",
                  f"- Iterations: {run.state['iteration']} / {run.budget['iterations']}",
                  f"- Time: {elapsed // 60}m {elapsed % 60}s / {run.budget['minutes']}m", "", "**Next Steps:**"])
    if remaining:
        lines.append("Use `/fix QA-NNN` to fix remaining issues by ID, or re-run `/qa:run` after adjusting `.av/config.toml` policy or budgets.")
    touched = sorted(set(run.state["fix_touched_files"]) - set(run.state["pre_loop_dirty"]))
    if touched:
        lines.append("To recover the loop's own edits: `git restore -- " + " ".join(shlex.quote(path) for path in touched)
                     + "` (scoped — restores only what the loop's fixes touched, never your pre-existing changes).")
    else:
        lines.append("The loop touched nothing eligible for scoped recovery.")
    overlap = sorted({path for row in run.state["iterations"] for path in row.get("result", {}).get("overlap", [])})
    if overlap:
        lines.append("Pre-existing-dirty files also edited by fixes are excluded from scoped recovery; reconcile manually: " + ", ".join(f"`{path}`" for path in overlap) + ".")
    if warnings:
        lines.extend(["", "**Warnings (manual review recommended):**", *(f"- {warning}" for warning in warnings)])
    lines.extend(["", "**Changes remain uncommitted for your control.**", ""])
    return "\n".join(lines)
