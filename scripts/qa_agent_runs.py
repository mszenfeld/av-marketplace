#!/usr/bin/env python3
"""Measure QA planner and reviewer runs from OMP JSONL session transcripts.

Usage:
    python3 scripts/qa_agent_runs.py --since 2026-11-03 --until 2026-11-03
    python3 scripts/qa_agent_runs.py --root ~/.hermes/kanban/omp --summary
    python3 scripts/qa_agent_runs.py --agent qa:test-planner --turns
    python3 scripts/qa_agent_runs.py --repo-suffix cd3f728=workflow-engine

Roots supplement the default OMP session directories. Costs are transcript-accounted
API prices, not billing receipts; an interrupted request may not have been persisted.

The CLI skips transcripts on I/O, Unicode, TypeError or AttributeError failures,
warning on stderr without discarding the other runs.

A recovered result dictionary is not evidence of successful completion. In particular,
error results and earlier JSON fragments are preserved, and absent JSON does not imply
a time/cost stop. Completion classification also needs the agent's output contract,
terminal execution evidence and (for a planner) the actual plan file.

Read paths under cwd become relative; classify_path still separates installed
dependencies from repository source, including dependencies inside the checkout.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import Counter
from collections import defaultdict
from collections.abc import Iterator
from datetime import date
from datetime import datetime
from datetime import timezone
from itertools import islice
from pathlib import Path

DEFAULT_ROOTS = ("~/.omp/agent/sessions", "~/.omp/profiles/*/agent/sessions")
DEFAULT_AGENTS = ("qa:test-planner", "qa:test-plan-reviewer")
PLAN_DIR_MARKER = "docs/testing/plans/"
LONG_TURN_TOKENS = 10_000
HEAD_LINES = 12
PARENT_IRC_MARKER = '<irc from="parent"'
BUDGET_MARKER = "[budget notice]"
TOKEN_RE = re.compile(r"\$\$[A-Za-z0-9_]+(?::[A-Za-z]+)?\$\$")
WROTE_RE = re.compile(r"Successfully wrote (\d+) bytes")
SELECTOR_RE = re.compile(r":(?:raw(?::.*)?|conflicts|img|-?\d[\d,+:\-]*(?::raw)?)$")


def iter_transcripts(roots: list[Path]) -> Iterator[Path]:
    """Yield non-Advisor transcripts without following directory symlinks."""
    for root in roots:
        for directory, _, files in os.walk(root, followlinks=False):
            for name in files:
                if name.endswith(".jsonl") and not name.startswith("__"):
                    yield Path(directory) / name


def read_head(path: Path, agents: set[str]) -> tuple[str, str] | None:
    """Identify the session and agent within the first HEAD_LINES lines."""
    session_id = None
    agent = None

    try:
        with path.open(encoding="utf-8") as stream:
            for line in islice(stream, HEAD_LINES):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue

                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "session":
                    session_id = entry.get("id")
                elif entry.get("type") == "session_init":
                    agent = entry.get("agent")
    except (OSError, UnicodeError) as error:
        sys.stderr.write(f"Warning: {path}: {error}\n")
        return None

    if isinstance(session_id, str) and isinstance(agent, str) and agent in agents:
        return session_id, agent
    return None


def _read_entries(path: Path) -> tuple[list[dict], int]:
    entries = []
    bad_lines = 0

    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                entry = json.loads(line)
            except ValueError:
                bad_lines += 1
                continue

            if isinstance(entry, dict):
                entries.append(entry)
            else:
                bad_lines += 1

    return entries, bad_lines


def _message(entry: dict) -> dict:
    return entry.get("message", {}) if entry.get("type") == "message" else {}


def _blocks(message: dict) -> list[dict]:
    content = message.get("content", [])
    return [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []


def _texts(message: dict) -> list[str]:
    content = message.get("content", [])
    if isinstance(content, str):
        return [content]
    return [part["text"] for part in _blocks(message)
            if part.get("type") == "text" and isinstance(part.get("text"), str)]


def _calls(message: dict) -> list[dict]:
    return [part for part in _blocks(message) if part.get("type") == "toolCall"]


def _results(entries: list[dict]) -> dict[str, dict]:
    return {message["toolCallId"]: message for entry in entries
            for message in [_message(entry)]
            if message.get("role") == "toolResult" and isinstance(message.get("toolCallId"), str)}


def _cost(usage: dict) -> float:
    return (usage.get("cost") or {}).get("total", 0) or 0


def parse_result(entries: list[dict]) -> dict | None:
    """Recover the last yield dictionary, otherwise the latest parseable text dict.

    Recovery intentionally does not classify success, failure or budget termination.
    """
    for entry in reversed(entries):
        message = _message(entry)
        if message.get("role") != "assistant":
            continue

        for call in reversed(_calls(message)):
            data = (call.get("arguments") or {}).get("data")
            if call.get("name") == "yield" and isinstance(data, dict):
                return data

    for entry in reversed(entries):
        message = _message(entry)
        if message.get("role") != "assistant":
            continue

        for text in reversed(_texts(message)):
            text = text.strip()
            if not text:
                continue

            candidates = [text]
            first, last = text.find("{"), text.rfind("}")
            if first >= 0 and last >= first:
                candidates.append(text[first:last + 1])

            for candidate in candidates:
                try:
                    value = json.loads(candidate)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    return value

    return None


def repo_label(cwd: str | None, suffix_map: dict[str, str]) -> str:
    """Label known worktree layouts before falling back to the directory name."""
    if cwd is None:
        return "unknown"
    kb = re.search(r"/\.omp/wt/kb-(.+?)-t_[0-9a-f]{8}(?:/|$)", cwd)
    if kb:
        return kb.group(1)
    if cwd.startswith(("/tmp/", "/private/tmp/")):
        return "tmp"
    suffix = re.search(r"-([0-9a-f]{7})/?$", cwd)
    if suffix:
        value = suffix.group(1)
        if value in suffix_map:
            return suffix_map[value]
        if "/wt/" in cwd:
            return f"wt-{value}"
    return Path(cwd).name


def strip_selector(raw: str) -> str:
    """Remove one trailing read selector and leading ./ components."""
    value = SELECTOR_RE.sub("", raw, count=1)
    while value.startswith("./"):
        value = value[2:]
    return value


def token_map(entries: list[dict], cwd: str | None) -> dict[str, str]:
    """Infer unambiguous masked tokens from reads with authoritative source paths."""
    results = _results(entries)
    values: dict[str, set[str]] = defaultdict(set)

    for entry in entries:
        message = _message(entry)
        if message.get("role") != "assistant":
            continue

        for call in _calls(message):
            raw = (call.get("arguments") or {}).get("path", "")
            if call.get("name") != "read" or not isinstance(raw, str) or not TOKEN_RE.search(raw):
                continue
            source = results.get(call.get("id"), {}).get("details", {}).get("meta", {}).get("source", {})
            if source.get("type") != "path" or not isinstance(source.get("value"), str):
                continue

            raw = strip_selector(raw)
            groups: dict[str, str] = {}
            pieces = [r"(?:^|/)"]
            position = 0

            for match in TOKEN_RE.finditer(raw):
                pieces.append(re.escape(raw[position:match.start()]))
                token = match.group()
                if token in groups:
                    pieces.append(f"(?P={groups[token]})")
                else:
                    group = f"t{len(groups)}"
                    groups[token] = group
                    pieces.append(f"(?P<{group}>[^/]+?)")
                position = match.end()

            pieces.extend((re.escape(raw[position:]), "$"))
            match = re.search("".join(pieces), source["value"])
            if match:
                for token, group in groups.items():
                    values[token].add(match.group(group))

    return {token: next(iter(found)) for token, found in values.items() if len(found) == 1}


def unmask(text: str, mapping: dict[str, str]) -> str:
    """Replace known masked tokens, retaining unknown tokens verbatim."""
    return TOKEN_RE.sub(lambda match: mapping.get(match.group(), match.group()), text)


def canonical_path(path: str, cwd: str | None) -> str:
    """Make absolute paths below cwd relative without resolving filesystem links."""
    value = Path(path)
    if value.is_absolute() and cwd:
        try:
            return value.relative_to(Path(cwd)).as_posix()
        except ValueError:
            pass
    return path


def classify_path(path: str, changed: set[str]) -> str:
    """Separate dependency reads even when canonicalized to checkout-relative paths."""
    if TOKEN_RE.search(path):
        return "unknown"
    value = Path(path)
    if any(part in {".venv", "venv", "node_modules", "site-packages"} for part in value.parts[:-1]):
        return "dependency"
    if path in changed:
        return "diff"
    if value.name.startswith("test_") or "tests" in value.parts[:-1] or ".test." in value.name:
        return "test"
    # An absolute Markdown file outside the checkout is external, not project docs.
    if not value.is_absolute() and (value.suffix == ".md" or value.parts[:1] == ("docs",)):
        return "docs"
    if value.is_absolute():
        return "external"
    return "other"


def advisor_stats(path: Path) -> dict:
    """Aggregate Advisor usage and actual advise calls across named Advisors."""
    stats = {
        "advisor_cost_usd": 0, "advisor_turns": 0, "advisor_notes": 0,
        "advisor_notes_by_severity": {}, "advisor_notes_delivered": 0,
        "advisor_notes_queued": 0, "advisor_text_messages": 0,
    }
    severities: Counter[str] = Counter()

    for advisor_path in sorted(path.with_suffix("").glob("__advisor*.jsonl")):
        try:
            entries, _ = _read_entries(advisor_path)
        except (OSError, UnicodeError) as error:
            sys.stderr.write(f"Warning: {advisor_path}: {error}\n")
            continue

        results = _results(entries)
        for entry in entries:
            if entry.get("type") == "model_usage":
                stats["advisor_cost_usd"] += _cost(entry.get("usage") or {})
            message = _message(entry)
            if message.get("role") != "assistant":
                continue

            stats["advisor_turns"] += 1
            stats["advisor_cost_usd"] += _cost(message.get("usage") or {})
            stats["advisor_text_messages"] += int(any(text.strip() for text in _texts(message)))

            for call in _calls(message):
                if call.get("name") != "advise":
                    continue
                stats["advisor_notes"] += 1
                severity = (call.get("arguments") or {}).get("severity")
                if isinstance(severity, str):
                    severities[severity] += 1
                text = "".join(_texts(results.get(call.get("id"), {})))
                stats["advisor_notes_delivered"] += int(text.startswith("Delivered"))
                stats["advisor_notes_queued"] += int(text.startswith("Queued"))

    stats["advisor_notes_by_severity"] = dict(severities)
    return stats


def _seconds_between(start: str | None, end: str | None) -> float:
    if start is None or end is None:
        return 0
    return (_timestamp(end) - _timestamp(start)).total_seconds()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _prompt_hashes(system_prompt: list[str]) -> tuple[str | None, str | None]:
    if not system_prompt:
        return None, None
    prompt = next((part for part in system_prompt if part.startswith("§ Role")), system_prompt[-1])
    markers = [prompt.find(marker) for marker in ("\n§ Context", "\n§ Plan", "\n§ Coop")]
    ends = [position for position in markers if position >= 0]
    instructions = prompt[:min(ends)] if ends else prompt
    agent_sha = hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:12]
    context_start = prompt.find("§ Context")
    if context_start < 0:
        context_start = prompt.find("§ Plan")
    if context_start < 0:
        return agent_sha, None

    context_end = prompt.find("\n§ Coop", context_start)
    context = prompt[context_start:context_end] if context_end >= 0 else prompt[context_start:]
    normalized = re.sub(r"\s+", " ", context)
    return agent_sha, hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _read_paths(calls: list[dict], results: dict[str, dict], cwd: str | None,
                mapping: dict[str, str]) -> tuple[list[str], int]:
    paths = []
    unresolved = 0

    for call in calls:
        if call.get("name") != "read":
            continue
        reply = results.get(call.get("id"), {})
        meta = (reply.get("details") or {}).get("meta")
        if meta is not None:
            source = meta.get("source", {})
            if source.get("type") == "path" and isinstance(source.get("value"), str):
                paths.append(canonical_path(source["value"], cwd))
            continue

        raw = (call.get("arguments") or {}).get("path")
        if not isinstance(raw, str):
            continue
        raw = strip_selector(unmask(raw, mapping))
        if TOKEN_RE.search(raw):
            unresolved += 1
        elif "://" not in raw and not raw.endswith("/"):
            paths.append(canonical_path(raw, cwd))

    return paths, unresolved


def parse_run(path: Path, long_turn_tokens: int, suffix_map: dict[str, str]) -> tuple[dict, list[dict]]:
    """Read a full transcript and return its run record and per-assistant-turn metrics."""
    entries, bad_lines = _read_entries(path)
    session = next((entry for entry in entries if entry.get("type") == "session"), {})
    init = next((entry for entry in entries if entry.get("type") == "session_init"), {})
    timestamps = [entry["timestamp"] for entry in entries if entry.get("timestamp")]
    start = session.get("timestamp") or (timestamps[0] if timestamps else None)
    session_id = session.get("id")
    cwd = session.get("cwd")
    task = init.get("task") or ""
    agent = init.get("agent")
    mode_match = re.search(r"^Mode: (draft|revise)$", task, re.M)
    round_match = re.search(r"^Round: (\d+) of 3$", task, re.M)
    plan_match = re.search(r"^Plan: (.+)$", task, re.M)
    mapping = token_map(entries, cwd)
    result = parse_result(entries)
    plan_path = plan_match.group(1) if plan_match else (result or {}).get("plan")
    plan_path = unmask(plan_path, mapping) if isinstance(plan_path, str) else None
    parent_dir = str(path.parent)
    prompts = init.get("systemPrompt") or []
    prompts = [prompts] if isinstance(prompts, str) else prompts
    agent_sha, context_sha = _prompt_hashes(prompts)
    results = _results(entries)
    users = [_message(entry) for entry in entries if _message(entry).get("role") == "user"]
    user_entries = [entry for entry in entries if _message(entry).get("role") == "user"]
    parent_ircs = [entry for entry in user_entries[1:]
                   if PARENT_IRC_MARKER in "".join(_texts(_message(entry))) ]
    irc_time = parent_ircs[0].get("timestamp") if parent_ircs else None
    advisor = advisor_stats(path)
    run = {
        "session_id": session_id, "path": str(path), "duplicates": 0, "bad_lines": bad_lines,
        "agent": agent,
        "mode": "review" if agent == "qa:test-plan-reviewer" else
                mode_match.group(1) if agent == "qa:test-planner" and mode_match else None,
        "round": int(round_match.group(1)) if round_match else None,
        "cwd": cwd, "repo": repo_label(cwd, suffix_map), "parent_dir": parent_dir,
        "plan_path": plan_path, "cycle_key": f"{parent_dir}|{plan_path}" if plan_path else None,
        "start": start, "end": start, "disposed_at": timestamps[-1] if timestamps else None,
        "wall_s": 0, "llm_s": 0,
        "omp_versions": sorted({entry["runtime"]["version"] for entry in entries
                                if (entry.get("runtime") or {}).get("version")}),
        "model": init.get("resolvedModel"), "model_role": init.get("modelRole"),
        "thinking_level": None, "effort": [], "fallback": False, "fallback_turn": None,
        "advisor": init.get("advisor"), "agent_prompt_sha": agent_sha, "context_sha": context_sha,
        "system_prompt_chars": [len(prompt) for prompt in prompts], "non_message_tokens": None,
        "turns": 0, "tool_calls": {}, "output_tokens": 0, "input_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0,
        "probe_cost_usd": sum(_cost(entry.get("usage") or {}) for entry in entries
                              if entry.get("type") == "model_usage"),
        "total_cost_usd": 0, "first_context_tokens": None, "max_context_tokens": None,
        "long_turns": 0, "long_turn_llm_s": 0,
        "compactions": sum(entry.get("type") == "compaction" for entry in entries),
        "plan_write_attempts": 0, "plan_writes_ok": 0, "first_plan_write_turn": None,
        "first_plan_write_s": None, "plan_write_bytes": None,
        "budget_notice": any(BUDGET_MARKER in "".join(_texts(message)) for message in users),
        "parent_irc_messages": len(parent_ircs), "turns_before_parent_irc": None,
        "first_parent_irc_turn": None, "masked_tokens": 0, "result": result,
        "changed_files": None, "changed_files_count": 0, "changed_files_unresolved": 0,
        "scenarios": None, "findings_by_severity": {}, **advisor,
    }
    turns = []
    tools: Counter[str] = Counter()
    efforts = set()
    masked = set()
    first_model = None
    fallback_seen = False

    for entry in entries:
        if entry.get("type") == "thinking_level_change" and not turns:
            run["thinking_level"] = entry.get("thinkingLevel")
        if entry.get("type") == "model_change" and entry.get("resolvedModelIsFallback"):
            run["fallback"] = True
            fallback_seen = True
        message = _message(entry)
        if message.get("role") == "toolResult":
            masked.update(TOKEN_RE.findall(json.dumps(message, ensure_ascii=False)))
        if message.get("role") != "assistant":
            continue

        index = len(turns) + 1
        calls = _calls(message)
        usage = message.get("usage") or {}
        timestamp = entry.get("timestamp")
        context = sum(usage.get(key, 0) or 0 for key in ("input", "cacheRead", "cacheWrite"))
        duration = (message.get("duration") or 0) / 1000
        output = usage.get("output", 0) or 0
        if not turns:
            first_model = message.get("model")
            run["first_context_tokens"] = context
            run["non_message_tokens"] = (message.get("contextSnapshot") or {}).get("nonMessageTokens")
        if fallback_seen or message.get("model") != first_model:
            run["fallback"] = True
            if run["fallback_turn"] is None:
                run["fallback_turn"] = index
        controls = message.get("requestControls") or {}
        if "effort" in controls:
            efforts.add(json.dumps(controls["effort"], sort_keys=True))
        run["end"] = timestamp
        run["llm_s"] += duration
        run["cost_usd"] += _cost(usage)
        run["max_context_tokens"] = max(run["max_context_tokens"] or 0, context)
        for field, key in (("output_tokens", "output"), ("input_tokens", "input"),
                           ("cache_read_tokens", "cacheRead"), ("cache_write_tokens", "cacheWrite")):
            run[field] += usage.get(key, 0) or 0
        if output > long_turn_tokens:
            run["long_turns"] += 1
            run["long_turn_llm_s"] += duration

        for call in calls:
            name = call.get("name", "")
            tools[name] += 1
            arguments = call.get("arguments") or {}
            masked.update(TOKEN_RE.findall(json.dumps(arguments, ensure_ascii=False)))
            is_plan_write = name == "write" and PLAN_DIR_MARKER in (arguments.get("path") or "")
            is_plan_edit = name == "edit" and any(PLAN_DIR_MARKER in (arguments.get(key) or "")
                                                 for key in ("path", "input"))
            if not (is_plan_write or is_plan_edit):
                continue
            run["plan_write_attempts"] += 1
            reply = results.get(call.get("id"))
            if reply is None or reply.get("isError") is True:
                continue

            run["plan_writes_ok"] += 1
            if run["first_plan_write_turn"] is None:
                run["first_plan_write_turn"] = index
                run["first_plan_write_s"] = _seconds_between(start, timestamp)
            if is_plan_write:
                sizes = [int(match) for text in _texts(reply) for match in WROTE_RE.findall(text)]
                size = max(sizes) if sizes else len((arguments.get("content") or "").encode("utf-8"))
                run["plan_write_bytes"] = max(run["plan_write_bytes"] or 0, size)

        read_paths, unresolved = _read_paths(calls, results, cwd, mapping)
        turns.append({
            "session_id": session_id, "index": index, "timestamp": timestamp,
            "duration_s": duration, "ttft_s": (message.get("ttft") or 0) / 1000,
            "output_tokens": output, "context_tokens": context, "cost_usd": _cost(usage),
            "tools": [call.get("name") for call in calls], "read_paths": read_paths,
            "read_unresolved": unresolved,
            "tool_result_chars": sum(len(text) for call in calls
                                     for text in _texts(results.get(call.get("id"), {}))),
            "thinking_chars": sum(len(part.get("thinking") or "") for part in _blocks(message)
                                  if part.get("type") == "thinking"),
            "before_first_plan_write": run["first_plan_write_turn"] is None,
            "before_parent_irc": irc_time is None or
                                 (timestamp is not None and _timestamp(timestamp) < _timestamp(irc_time)),
        })

    run["turns"] = len(turns)
    run["wall_s"] = _seconds_between(start, run["end"])
    run["tool_calls"] = dict(tools)
    run["effort"] = sorted(efforts)
    run["masked_tokens"] = len(masked)
    run["total_cost_usd"] = run["cost_usd"] + run["probe_cost_usd"] + advisor["advisor_cost_usd"]
    if parent_ircs:
        run["turns_before_parent_irc"] = sum(turn["before_parent_irc"] for turn in turns)
        run["first_parent_irc_turn"] = run["turns_before_parent_irc"] + 1
    if result:
        changed = result.get("changed_files")
        if isinstance(changed, list):
            run["changed_files"] = [unmask(value, mapping) for value in changed]
            run["changed_files_count"] = len(changed)
            run["changed_files_unresolved"] = sum(bool(TOKEN_RE.search(value)) for value in run["changed_files"])
        scenario_keys = ("fe_scenarios", "be_scenarios", "desktop_scenarios")
        if any(key in result for key in scenario_keys):
            run["scenarios"] = sum(result.get(key, 0) or 0 for key in scenario_keys)
        run["findings_by_severity"] = dict(Counter(finding["severity"] for finding in result.get("findings", [])
                                                 if isinstance(finding, dict) and "severity" in finding))

    return run, turns


def _stat(values: list[float], percentile: int = 50) -> str:
    if not values:
        return "-"
    value = statistics.median(values) if percentile == 50 else sorted(values)[math.ceil(0.9 * len(values)) - 1]
    return f"{value:.2f}"


def summarize(runs: list[dict]) -> str:
    """Build grouped Markdown medians and nearest-rank p90s; absent samples are '-'."""
    columns = ("agent", "mode", "runs", "wall_min p50", "wall_min p90", "llm_min p50", "llm_min p90",
               "turns p50", "output_k p50", "total_usd p50", "total_usd sum", "advisor_usd sum",
               "advisor_notes sum", "long_turns p50", "long_share p50", "first_write_pct p50")
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    groups: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for run in runs:
        groups[(run["agent"], run["mode"])].append(run)

    for (agent, mode), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        wall = [run["wall_s"] / 60 for run in group]
        llm = [run["llm_s"] / 60 for run in group]
        cells = [
            agent, mode or "-", str(len(group)), _stat(wall), _stat(wall, 90), _stat(llm), _stat(llm, 90),
            _stat([run["turns"] for run in group]), _stat([run["output_tokens"] / 1000 for run in group]),
            _stat([run["total_cost_usd"] for run in group]), f'{sum(run["total_cost_usd"] for run in group):.2f}',
            f'{sum(run["advisor_cost_usd"] for run in group):.2f}', str(sum(run["advisor_notes"] for run in group)),
            _stat([run["long_turns"] for run in group]),
            _stat([run["long_turn_llm_s"] / run["llm_s"] for run in group if run["llm_s"]]),
            _stat([100 * run["first_plan_write_turn"] / run["turns"] for run in group
                   if run["first_plan_write_turn"] is not None and run["turns"]]),
        ]
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def _date_argument(value: str) -> str:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("expected YYYY-MM-DD")
        date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a valid YYYY-MM-DD date") from error
    return value


def _suffix_argument(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"([0-9a-f]{7})=(.+)", value)
    if match is None:
        raise argparse.ArgumentTypeError("expected HEX=NAME with seven lowercase hexadecimal digits")
    return match.group(1), match.group(2)


def _threshold_argument(value: str) -> int:
    try:
        threshold = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a nonnegative integer") from error
    if threshold < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return threshold


def main(argv: list[str] | None = None) -> int:
    """Print deduplicated, filtered run records, turn records or a Markdown summary."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", action="append", default=[], metavar="DIR")
    parser.add_argument("--agent", action="append", metavar="NAME")
    parser.add_argument("--since", type=_date_argument, metavar="YYYY-MM-DD")
    parser.add_argument("--until", type=_date_argument, metavar="YYYY-MM-DD")
    parser.add_argument("--cwd-contains", metavar="TEXT")
    parser.add_argument("--repo-suffix", action="append", type=_suffix_argument, default=[], metavar="HEX=NAME")
    parser.add_argument("--long-turn-tokens", type=_threshold_argument, default=LONG_TURN_TOKENS, metavar="N")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--turns", action="store_true")
    output.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    roots = [Path(expanded) for raw in (*DEFAULT_ROOTS, *args.root)
             for expanded in glob.glob(os.path.expanduser(raw)) if Path(expanded).is_dir()]
    agents = set(args.agent or DEFAULT_AGENTS)
    sessions: dict[str, list[Path]] = defaultdict(list)

    for path in sorted(set(iter_transcripts(roots)), key=str):
        head = read_head(path, agents)
        if head:
            sessions[head[0]].append(path)

    parsed = []
    for paths in sessions.values():
        try:
            run, turns = parse_run(paths[0], args.long_turn_tokens, dict(args.repo_suffix))
        except (OSError, UnicodeError, TypeError, AttributeError) as error:
            sys.stderr.write(f"Warning: {paths[0]}: {error}\n")
            continue

        run["duplicates"] = len(paths) - 1
        day = (run["start"] or "")[:10]
        if args.since and day < args.since:
            continue
        if args.until and day > args.until:
            continue
        if args.cwd_contains and args.cwd_contains not in (run["cwd"] or ""):
            continue
        parsed.append((run, turns))

    parsed.sort(key=lambda pair: (pair[0]["start"] or "", pair[0]["path"]))
    if args.summary:
        sys.stdout.write(summarize([run for run, _ in parsed]) + "\n")
    else:
        for run, turns in parsed:
            for record in turns if args.turns else [run]:
                sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
