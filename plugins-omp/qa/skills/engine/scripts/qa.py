#!/usr/bin/env python3
"""Stdlib-only JSON command interface for the QA engine."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import NoReturn

# Check before importing tomllib or any engine module, so older interpreters
# receive the actionable version error rather than an import traceback.
if sys.version_info < (3, 11):
    print(json.dumps({"error": f"Error: the qa engine needs Python 3.11 or newer (found {sys.version_info.major}.{sys.version_info.minor})"}))
    raise SystemExit(2)

from av_config import ConfigError
from av_config import InvalidConfig
from qa_engine.config import Config
from qa_engine.accounts import provision
from qa_engine.accounts import refresh
from qa_engine.accounts import teardown
from qa_engine.services import services
from qa_engine.plan import check_plan
from qa_engine.plan import parse_plan
from qa_engine.plan import resolve_plan
from qa_engine.state import StateStop
from qa_engine.state import assign_issues
from qa_engine.state import candidates
from qa_engine.state import check_drift
from qa_engine.state import dispatch_fix
from qa_engine.state import dispatch_tester
from qa_engine.state import end_run
from qa_engine.state import fix_done
from qa_engine.state import ingest
from qa_engine.state import iteration_close
from qa_engine.state import iteration_open
from qa_engine.state import open_run
from qa_engine.state import start_run

STATE_COMMANDS = frozenset({"dispatch", "fix", "ingest", "issues", "candidates", "iteration", "accounts", "services"})


class UsageError(ConfigError):
    """Invalid command syntax, with no user-provided value echoed."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError("invalid command arguments; use --help")

    def format_help(self) -> str:
        return json.dumps({"help": super().format_help()}) + "\n"


def repo_option(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    return parser


def run_option(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--run", required=True)
    return repo_option(parser)


def parser() -> argparse.ArgumentParser:
    root = Parser(description="QA engine (JSON output)")
    root.add_argument("--repo", type=Path)
    commands = root.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config")
    config.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    operations = config.add_subparsers(dest="operation")
    preview = operations.add_parser("preview")
    preview.add_argument("proposal", type=Path)
    preview.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    apply = operations.add_parser("apply")
    apply.add_argument("proposal", type=Path)
    apply.add_argument("--snapshot", required=True)
    apply.add_argument("--approved-hash", required=True)
    apply.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    trust = commands.add_parser("trust")
    trust.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    trust_operations = trust.add_subparsers(dest="operation", required=True)
    accept = trust_operations.add_parser("accept")
    accept.add_argument("hash")
    accept.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    tools = commands.add_parser("tools")
    tools.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    plan = commands.add_parser("plan")
    plan.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    plan_operations = plan.add_subparsers(dest="operation", required=True)
    resolve = plan_operations.add_parser("resolve")
    resolve.add_argument("argument", nargs="?", default="")
    resolve.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    check = plan_operations.add_parser("check")
    check.add_argument("plan", type=Path)
    check.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    run = repo_option(commands.add_parser("run"))
    run_operations = run.add_subparsers(dest="operation", required=True)
    start = repo_option(run_operations.add_parser("start"))
    start.add_argument("plan", type=Path)
    start.add_argument("--takeover")
    run_option(run_operations.add_parser("end"))
    dispatch = run_option(commands.add_parser("dispatch"))
    dispatch_operations = dispatch.add_subparsers(dest="operation", required=True)
    tester = repo_option(dispatch_operations.add_parser("tester"))
    tester.add_argument("--section", required=True, choices=["FE", "BE"])
    tester.add_argument("--phase", required=True, choices=["baseline", "retry", "iteration", "final"])
    repo_option(dispatch_operations.add_parser("fix")).add_argument("--qa", required=True)
    fix = repo_option(commands.add_parser("fix"))
    done = run_option(fix.add_subparsers(dest="operation", required=True).add_parser("done"))
    done.add_argument("--dispatch", required=True)
    done.add_argument("--result", required=True, choices=["fixed", "partial", "failed"])
    results = run_option(commands.add_parser("ingest"))
    results.add_argument("--dispatch", required=True)
    results.add_argument("file", type=Path)
    run_option(commands.add_parser("issues"))
    run_option(commands.add_parser("candidates"))
    iteration = repo_option(commands.add_parser("iteration"))
    iteration_operations = iteration.add_subparsers(dest="operation", required=True)
    run_option(iteration_operations.add_parser("open"))
    run_option(iteration_operations.add_parser("close"))
    for command, action_names in (("accounts", ("provision", "refresh", "teardown")), ("services", ("check", "up", "prepare", "down"))):
        group = repo_option(commands.add_parser(command))
        actions = group.add_subparsers(dest="operation", required=True)
        for operation in action_names:
            run_option(actions.add_parser(operation))
    return root


def repository(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_dir():
            raise UsageError("repository root is not a directory")
        return explicit.resolve()
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise UsageError("repository root unavailable; supply --repo")
    return Path(result.stdout.strip()).resolve()


def tools() -> dict[str, object]:
    result: dict[str, object] = {name: shutil.which(name) is not None for name in ("curl", "jq", "psql", "mysql", "sqlite3")}
    perl = shutil.which("perl")
    result["perl_json_pp"] = perl is not None and subprocess.run([perl, "-MJSON::PP", "-e", "1"], capture_output=True, check=False).returncode == 0
    result["httpie"] = shutil.which("http") is not None
    return result


def read_proposal(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as error:
        raise UsageError("proposal: unreadable or invalid JSON") from error
    if not isinstance(value, dict):
        raise UsageError("proposal: expected a JSON object")
    return value


def run_state(repo: Path, args: argparse.Namespace) -> dict[str, object]:
    """Run one loop bookkeeping subcommand under the run's state lock."""
    with open_run(repo, args.run) as run:
        if args.command in {"accounts", "services"}:
            config = Config(repo)
            if args.operation not in {"teardown", "down"}:
                check_drift(run, config)
            if args.command == "services":
                return services(run, config, args.operation)
            if args.operation == "provision":
                return provision(run, config)
            if args.operation == "refresh":
                return {"refreshed": refresh(run, config)}
            return teardown(run, config)
        if args.command == "dispatch":
            config = Config(repo)
            if args.operation == "tester":
                return dispatch_tester(run, config, args.section, args.phase)
            return dispatch_fix(run, config, args.qa)
        if args.command == "fix":
            return fix_done(run, args.dispatch, args.result)
        if args.command == "ingest":
            path = args.file if args.file.is_absolute() else repo / args.file
            return ingest(run, args.dispatch, path.read_text())
        if args.command == "issues":
            return assign_issues(run)
        if args.command == "candidates":
            return candidates(run)
        return iteration_open(run) if args.operation == "open" else iteration_close(run)


def execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    repo = repository(args.repo)
    if args.command == "tools":
        return tools(), 0
    if args.command == "plan" and args.operation == "resolve":
        return resolve_plan(repo, args.argument), 0
    if args.command == "run":
        if args.operation == "end":
            return end_run(repo, args.run), 0
        path = args.plan if args.plan.is_absolute() else repo / args.plan
        return start_run(Config(repo), path, args.takeover), 0
    if args.command in STATE_COMMANDS:
        return run_state(repo, args), 0
    config = Config(repo)
    if args.command == "trust":
        return config.accept(args.hash), 0
    if args.command == "plan":
        if config.errors:
            return config.report(), 2
        path = args.plan if args.plan.is_absolute() else repo / args.plan
        result = check_plan(parse_plan(path), config)
        return result, 0 if result["ok"] else 1
    if args.operation == "preview":
        result = config.preview(read_proposal(args.proposal))
        return result, 0 if result["ok"] else 2
    if args.operation == "apply":
        return config.apply(read_proposal(args.proposal), args.snapshot, args.approved_hash), 0
    return config.report(), 2 if config.errors else 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        result, code = execute(args)
    except (UsageError, InvalidConfig) as error:
        result, code = {"error": str(error)}, 2
    except StateStop as error:
        result, code = {"error": str(error), **error.details}, 1
    except ConfigError as error:
        result, code = {"error": str(error)}, 1
    except (OSError, UnicodeError):
        # File/process errors may contain paths or values in their details.
        result, code = {"error": "engine I/O operation failed"}, 1
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
