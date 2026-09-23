#!/usr/bin/env python3
"""Route delivery tasks to the developer agent that owns their stack.

A task's stack comes from the files it touches: `.py` is Python, `.php` is
PHP, and TypeScript/JavaScript/CSS is React frontend only when the nearest
manifest above the file is a `package.json` that depends on React. Docs and
config files do not vote. Nothing here calls a model.

Usage:
    route_task.py plan <root> <plan.md>   # JSON list, one entry per task
    route_task.py files <root> <path>...  # JSON routing for these paths
    route_task.py layout <root>           # JSON repo layout for a judge
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

AGENTS = {
    "python": "python-developer:developer",
    "frontend": "frontend-developer:developer",
    "php": "php-developer:developer",
    "generic": "delivery:implementer",
}
PY_MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
FRONTEND_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss"}
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "__pycache__", "venv"}
LAYOUT_DEPTH = 3

PY_FRAMEWORKS = ("fastapi", "django", "flask", "celery", "sqlalchemy", "pydantic")
PHP_FRAMEWORKS = {
    "symfony/framework-bundle": "symfony",
    "doctrine/orm": "doctrine",
    "laravel/framework": "laravel",
}
JS_FRAMEWORKS = (
    "next", "vite", "tailwindcss", "zustand",
    "@tanstack/react-query", "@tanstack/react-router", "react-hook-form",
)

TASK_HEADING = re.compile(r"^### Task (\d+):\s*(.+?)\s*$", re.MULTILINE)
SECTION_END = re.compile(r"^#{2,3} ", re.MULTILINE)
FILE_LINE = re.compile(r"^\s*[-*]\s*(?:Create|Modify|Test|Delete):\s*(.+?)\s*$", re.MULTILINE)
COMMIT_LINE = re.compile(r"^\*\*Commit:\*\*\s*(.+?)\s*$", re.MULTILINE)
BACKTICKED = re.compile(r"`([^`]+)`")


def clean_path(raw: str) -> str:
    """Strip backticks, whitespace and a `:12-34` line-range suffix."""
    return raw.strip().strip("`").strip().split(":", 1)[0]


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def js_dependencies(package_json: Path) -> set[str]:
    data = read_json(package_json)
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)
    return deps


def has_manifest(directory: Path) -> bool:
    return any((directory / name).is_file() for name in (*PY_MANIFESTS, "composer.json", "package.json"))


def frontend_or_generic(root: Path, rel: Path) -> str:
    """A web file is frontend only when its nearest manifest is a React package.json."""
    directory = (root / rel).parent
    for candidate in [directory, *directory.parents]:
        if candidate != root and root not in candidate.parents:
            break
        if candidate.is_dir() and has_manifest(candidate):
            package_json = candidate / "package.json"
            if package_json.is_file() and "react" in js_dependencies(package_json):
                return "frontend"
            return "generic"
        if candidate == root:
            break
    return "generic"


def classify(root: Path, raw: str) -> str | None:
    """Stack of one path, or None when the file is neutral (docs, config)."""
    rel = Path(clean_path(raw))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    suffix = rel.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".php":
        return "php"
    if suffix in FRONTEND_EXT:
        return frontend_or_generic(root, rel)
    return None


def route(root: Path, paths: list[str]) -> dict:
    files = [clean_path(p) for p in paths]
    groups: dict[str, list[str]] = {}
    for raw, path in zip(paths, files):
        stack = classify(root, raw)
        if stack is not None:
            groups.setdefault(stack, []).append(path)
    if not files:
        stack = "unknown"
    elif not groups:
        stack = "generic"
    elif len(groups) == 1:
        stack = next(iter(groups))
    else:
        stack = "split"
    return {"files": files, "stack": stack, "agent": AGENTS.get(stack), "groups": groups}


def parse_plan(text: str) -> list[dict]:
    headings = list(TASK_HEADING.finditer(text))
    tasks = []
    for heading in headings:
        rest = text[heading.end():]
        stop = SECTION_END.search(rest)
        block = text[heading.start(): heading.end() + (stop.start() if stop else len(rest))].rstrip()
        paths = []
        for match in FILE_LINE.finditer(block):
            value = match.group(1)
            quoted = BACKTICKED.search(value)
            paths.append(quoted.group(1) if quoted else value.split()[0])
        commit = COMMIT_LINE.search(block)
        tasks.append({
            "task": int(heading.group(1)),
            "title": heading.group(2),
            "commit": commit.group(1) if commit else None,
            "block": block,
            "paths": paths,
        })
    return tasks


def manifest_kinds(directory: Path) -> list[tuple[str, str]]:
    """(kind, evidence) for every manifest in one directory."""
    kinds: list[tuple[str, str]] = []
    py_files = [name for name in PY_MANIFESTS if (directory / name).is_file()]
    if py_files:
        text = " ".join((directory / name).read_text(errors="replace").lower() for name in py_files)
        found = [fw for fw in PY_FRAMEWORKS if fw in text]
        kinds.append(("python", ", ".join(found) or py_files[0]))
    composer = directory / "composer.json"
    if composer.is_file():
        require = read_json(composer).get("require")
        keys = set(require) if isinstance(require, dict) else set()
        found = [label for key, label in PHP_FRAMEWORKS.items() if key in keys]
        kinds.append(("php", ", ".join(found) or "composer.json"))
    package_json = directory / "package.json"
    if package_json.is_file():
        deps = js_dependencies(package_json)
        found = [fw for fw in JS_FRAMEWORKS if fw in deps]
        kind = "frontend" if "react" in deps else "node"
        kinds.append((kind, ", ".join(found) or "package.json"))
    return kinds


def layout(root: Path) -> dict:
    lines: list[str] = []
    present: set[str] = set()
    for current, dirs, _ in os.walk(root):
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        if depth >= LAYOUT_DEPTH:
            dirs[:] = []
        rel = here.relative_to(root).as_posix()
        for kind, evidence in manifest_kinds(here):
            lines.append(f"{rel}/: {kind} ({evidence})")
            present.add(kind)
    stacks = [s for s in ("python", "frontend", "php") if s in present]
    return {"stacks": stacks, "lines": lines}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in {"plan", "files", "layout"}:
        print(__doc__, file=sys.stderr)
        return 2
    command, root = argv[0], Path(argv[1]).resolve()
    if command == "layout":
        print(json.dumps(layout(root)))
        return 0
    if command == "files":
        print(json.dumps(route(root, argv[2:])))
        return 0
    if len(argv) != 3:
        print("usage: route_task.py plan <root> <plan.md>", file=sys.stderr)
        return 2
    plan_path = Path(argv[2])
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    if not plan_path.is_file():
        print(f"plan not found: {plan_path}", file=sys.stderr)
        return 2
    tasks = parse_plan(plan_path.read_text())
    if not tasks:
        print(f"no '### Task N:' headings in {plan_path}", file=sys.stderr)
        return 2
    out = []
    for task in tasks:
        routed = route(root, task["paths"])
        out.append({
            "task": task["task"],
            "title": task["title"],
            "commit": task["commit"],
            "block": task["block"],
            "files": routed["files"],
            "stack": routed["stack"],
            "agent": routed["agent"],
            "groups": routed["groups"],
        })
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
