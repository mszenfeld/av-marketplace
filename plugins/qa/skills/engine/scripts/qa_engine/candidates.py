"""Fix eligibility and remaining failures at the configured severity floor."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from qa_engine.assertions import FAILING
from qa_engine.common import assertion_keys
from qa_engine.assertions import _scenario_of
from qa_engine.common import SEVERITIES
from qa_engine.issue_blocks import IssueBlock
from qa_engine.issue_blocks import _issue_blocks
from qa_engine.issues import _claims
from qa_engine.issues import _sorted_ids
from qa_engine.models import Run
from qa_engine.plan import Scenario
from qa_engine.plan import run_plan
from qa_engine.schema import AssertionRecord
from qa_engine.schema import JSON
from qa_engine.schema import SidecarState


@dataclass(frozen=True)
class CandidateContext:
    """Shared eligibility inputs for one candidate-selection pass."""

    state: SidecarState
    claims: Mapping[str, list[str]]
    blocks: Mapping[str, IssueBlock]
    floor: int


def candidates(run: Run) -> JSON:
    """Failing issues that may go to fix-auto, and why the rest may not."""
    state = run.state
    plan = run_plan(run)
    context = CandidateContext(
        state=state, claims=_claims(state), blocks=_issue_blocks(run.report_text()),
        floor=SEVERITIES.index(run.policy["min_severity"]),
    )
    auto = run.policy["fix"] == "auto"
    fix: list[JSON] = []
    dropped: list[JSON] = []

    for scenario in plan.scenarios:
        if state["current"].get(scenario.id) != "fail":
            continue
        for qa in _sorted_ids(state["scenario_issues"].get(scenario.id, [])):
            key = state["issue_assertion"].get(qa)
            record = state["assertions"].get(key) if isinstance(key, str) else None
            reason = _drop_reason(context, scenario, qa, key, record)
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


def _drop_reason(
    context: CandidateContext, scenario: Scenario, qa: str, key: object, record: AssertionRecord | None,
) -> str | None:
    block = context.blocks.get(qa)
    if block is not None and block.rejected:
        return "rejected by user"
    if block is None or block.severity not in SEVERITIES:
        return "incomplete fields"
    if not block.located:
        return "needs manual location"
    if not (block.problem and block.remediation):
        return "incomplete fields"
    if not isinstance(key, str) or key not in assertion_keys(scenario) or context.claims.get(key) != [qa]:
        return "needs manual assertion mapping"
    if record is None or record["result"] not in FAILING:
        return "assertion not failing"
    if SEVERITIES.index(block.severity) < context.floor:
        return "below min_severity"
    if key == scenario.id and qa in context.state["auth_gated_issues"]:
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


