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
from typing import TextIO

# Check before importing tomllib or any engine module, so older interpreters
# receive the actionable version error rather than an import traceback.
if sys.version_info < (3, 11):
    print(json.dumps({"error": f"Error: the qa engine needs Python 3.11 or newer (found {sys.version_info.major}.{sys.version_info.minor})"}))
    raise SystemExit(2)

from av_config import ConfigError
from av_config import InvalidConfig
from qa_engine.config import Config
from qa_engine.plan import check_plan
from qa_engine.plan import parse_plan
from qa_engine.plan import resolve_plan


class UsageError(ConfigError):
    """Invalid command syntax, with no user-provided value echoed."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError("invalid command arguments; use --help")

    def print_help(self, file: TextIO | None = None) -> None:
        print(json.dumps({"help": self.format_help()}), file=file)


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


def execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    repo = repository(args.repo)
    if args.command == "tools":
        return tools(), 0
    if args.command == "plan" and args.operation == "resolve":
        return resolve_plan(repo, args.argument), 0
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
    except ConfigError as error:
        result, code = {"error": str(error)}, 1
    except (OSError, UnicodeError):
        # File/process errors may contain paths or values in their details.
        result, code = {"error": "engine I/O operation failed"}, 1
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
