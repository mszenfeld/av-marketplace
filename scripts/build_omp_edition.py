#!/usr/bin/env python3
"""Build the OMP edition of this marketplace from the Claude Code sources.

`plugins/<name>/` stays the single source of truth. For every plugin with an
overlay at `omp/overlay/<name>.json`, this script writes `plugins-omp/<name>/`:

- `scripts/` is copied verbatim;
- `skills/` is copied verbatim except each `SKILL.md` frontmatter `name`,
  which becomes `<plugin>:<skill>` so same-named skills of different plugins
  stay distinct;
- `commands/*.md` keep `description` / `argument-hint` and gain the harness
  preamble (`omp/preamble.md`) above the unchanged body;
- `agents/*.md` get OMP frontmatter — `<plugin>:<agent>` names, OMP tool
  names, a role-routed `model` — and the preamble above the unchanged body;
- `.omp-plugin/plugin.json` mirrors the Claude manifest.

`omp/native/<name>/` holds OMP-only plugins. They are copied as-is (minus
`tests/` and caches) and listed with the version from their own
`.omp-plugin/plugin.json`.

It also writes `.omp-plugin/marketplace.json`, listing only plugins that have
an OMP edition, with versions of generated plugins taken from the Claude
catalog.

Every mapping is total: an unknown source tool, frontmatter key, or an agent
without an overlay entry fails the build instead of disappearing silently.

Usage:
    python3 scripts/build_omp_edition.py          # (re)generate
    python3 scripts/build_omp_edition.py --check  # exit 1 if output is stale
"""

from __future__ import annotations

import argparse
import filecmp
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO / "plugins"
OUTPUT_DIR_NAME = "plugins-omp"
OVERLAY_DIR = REPO / "omp" / "overlay"
PREAMBLE_PATH = REPO / "omp" / "preamble.md"
CLAUDE_CATALOG = REPO / ".claude-plugin" / "marketplace.json"
OMP_CATALOG_REL = Path(".omp-plugin") / "marketplace.json"

VERBATIM_DIRS = ("scripts",)
COMMAND_KEYS = ("description", "argument-hint")
AGENT_SOURCE_KEYS = {"name", "description", "tools", "disallowedTools", "model", "skills"}
OVERLAY_KEYS = {"plugin", "agents", "fallback_model"}
AGENT_SPEC_KEYS = {"role", "add_tools", "thinking", "autoload"}

NATIVE_DIR = REPO / "omp" / "native"
NATIVE_MANIFEST_KEYS = {"name", "version", "description", "category"}
NATIVE_AGENT_KEYS = {
    "name", "description", "tools", "spawns", "model", "thinking-level", "output",
    "blocking", "autoloadSkills", "read-summarize", "prewalk", "advisor",
}

# Claude Code tool name -> OMP tool name. None = no equivalent; dropped on
# purpose (the preamble tells the model what replaces it).
TOOL_MAP: dict[str, str | None] = {
    "Read": "read",
    "Grep": "grep",
    "Glob": "glob",
    "Bash": "bash",
    "Edit": "edit",
    "NotebookEdit": "edit",
    "Write": "write",
    "WebSearch": "web_search",
    "WebFetch": "read",
    "Task": "task",
    "TaskCreate": "todo",
    "TaskUpdate": "todo",
    "TaskList": "todo",
    "AskUserQuestion": "ask",
    "Skill": None,
}
SCOPED_GRANT = re.compile(r"^(?P<tool>[A-Za-z]+)\(.*\)$")
SKILL_NAME_LINE = re.compile(r"^name:.*$", re.MULTILINE)


class BuildError(Exception):
    pass


def split_frontmatter(text: str, origin: Path) -> tuple[list[tuple[str, str]], str]:
    """Return ordered (key, raw value) pairs and the body after the closing fence."""
    if not text.startswith("---\n"):
        raise BuildError(f"{origin}: missing frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise BuildError(f"{origin}: unterminated frontmatter")
    pairs: list[tuple[str, str]] = []
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or line[0].isspace():
            raise BuildError(f"{origin}: unsupported frontmatter line {line!r}")
        pairs.append((key.strip(), value.strip()))
    return pairs, text[end + len("\n---\n") :]


def yaml_str(value: str) -> str:
    # A JSON string literal is a valid double-quoted YAML scalar.
    return json.dumps(value, ensure_ascii=False)


def scalar(raw: str) -> str:
    """Re-emit a raw source value as a string scalar.

    Claude Code reads `argument-hint: [pr-number]` as text; a YAML parser reads
    it as a list. Quote every value unless the source already quoted it.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw
    return yaml_str(raw)


def unquote(raw: str) -> str:
    """Inverse of `scalar` for the quoting styles frontmatter values use."""
    if raw.startswith('"'):
        return json.loads(raw)
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def build_skill(src: Path, plugin: str) -> str:
    """Rename the skill to `<plugin>:<skill>`; every other byte is unchanged."""
    text = src.read_text()
    pairs, _ = split_frontmatter(text, src)
    skill_dir = src.parent.name
    if unquote(dict(pairs).get("name", "")) != skill_dir:
        raise BuildError(f"{src}: skill name must equal its directory {skill_dir!r}")
    end = text.find("\n---\n", 4)
    head = SKILL_NAME_LINE.sub(f"name: {yaml_str(f'{plugin}:{skill_dir}')}", text[:end], count=1)
    return head + text[end:]


def render(frontmatter: list[tuple[str, str]], preamble: str, body: str) -> str:
    lines = ["---", *(f"{key}: {value}" for key, value in frontmatter), "---", ""]
    return "\n".join(lines) + preamble + "\n" + body.lstrip("\n")


def map_tools(raw: str, origin: Path) -> list[str]:
    mapped: list[str] = []
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        scoped = SCOPED_GRANT.match(token)
        name = scoped.group("tool") if scoped else token
        if name not in TOOL_MAP:
            raise BuildError(f"{origin}: no OMP mapping for tool {token!r}")
        target = TOOL_MAP[name]
        if target and target not in mapped:
            mapped.append(target)
    return mapped


def build_agent(src: Path, plugin: str, overlay: dict, preamble: str, skill_names: set[str]) -> str:
    pairs, body = split_frontmatter(src.read_text(), src)
    fields = dict(pairs)
    unknown = set(fields) - AGENT_SOURCE_KEYS
    if unknown:
        raise BuildError(f"{src}: no OMP mapping for frontmatter keys {sorted(unknown)}")
    name = fields.get("name")
    if not name or not fields.get("description"):
        raise BuildError(f"{src}: agent needs name and description")
    spec = overlay["agents"].get(name)
    if spec is None:
        raise BuildError(f"{src}: agent {name!r} has no entry in omp/overlay/{plugin}.json")
    extra = set(spec) - AGENT_SPEC_KEYS
    if extra:
        raise BuildError(f"{src}: unknown keys {sorted(extra)} for agent {name!r} in omp/overlay/{plugin}.json")
    if "role" not in spec:
        raise BuildError(f"{src}: agent {name!r} has no role in omp/overlay/{plugin}.json")

    # `todo` is parent-owned in OMP: the task executor strips it from every
    # subagent, so granting it would only mislead a reader of the frontmatter.
    tools = [t for t in map_tools(fields.get("tools", ""), src) if t != "todo"]
    for extra in spec.get("add_tools", []):
        if extra not in tools:
            tools.append(extra)
    # OMP's `tools:` is an allowlist, so `disallowedTools` has nothing to say
    # there — but the denial must still hold after mapping and overlay extras.
    denied = set(map_tools(fields.get("disallowedTools", ""), src)) & set(tools)
    if denied:
        raise BuildError(f"{src}: granted tools {sorted(denied)} are in disallowedTools")
    # Claude Code's `inherit` (and an absent `model:`) means "the parent's
    # model"; OMP falls back to the parent when no listed entry resolves, so
    # the role alias alone expresses it.
    selectors = [f"@{spec['role']}"]
    fallback = fields.get("model") or overlay.get("fallback_model")
    if fallback and fallback != "inherit":
        selectors.append(fallback)

    out: list[tuple[str, str]] = [
        ("name", yaml_str(f"{plugin}:{name}")),
        ("description", scalar(fields["description"])),
    ]
    if tools:
        out.append(("tools", ", ".join(tools)))
    out.append(("model", yaml_str(", ".join(selectors))))
    if "thinking" in spec:
        out.append(("thinking-level", spec["thinking"]))
    if "autoload" in spec:
        autoload = list(spec["autoload"])
    else:
        autoload = [s.strip() for s in fields.get("skills", "").split(",") if s.strip()]
    missing = [s for s in autoload if s not in skill_names]
    if missing:
        raise BuildError(f"{src}: autoload skills {missing} do not exist in plugins/{plugin}/skills/")
    if autoload:
        # OMP matches autoload entries by skill name, which build_skill prefixed.
        out.append(("autoloadSkills", json.dumps([f"{plugin}:{s}" for s in autoload])))
    relpath = src.relative_to(SOURCE_ROOT / plugin).as_posix()
    return render(out, preamble.format(plugin=plugin, relpath=relpath), body)


def build_command(src: Path, plugin: str, preamble: str) -> str:
    pairs, body = split_frontmatter(src.read_text(), src)
    kept = [(k, scalar(v)) for k, v in pairs if k in COMMAND_KEYS]
    relpath = src.relative_to(SOURCE_ROOT / plugin).as_posix()
    return render(kept, preamble.format(plugin=plugin, relpath=relpath), body)


def guarded(path: Path, out_root: Path) -> Path:
    # The one invariant this script guarantees: nothing outside the output root.
    resolved = path.resolve()
    if out_root.resolve() not in resolved.parents:
        raise BuildError(f"refusing to write outside {out_root}: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def write(path: Path, content: str, out_root: Path) -> None:
    guarded(path, out_root).write_text(content)


def copy(src: Path, path: Path, out_root: Path) -> None:
    # copy2 keeps the executable bit the scripts rely on.
    shutil.copy2(src, guarded(path, out_root))


def native_skipped(rel: Path) -> bool:
    return (
        rel.parts[0] == "tests"
        or "__pycache__" in rel.parts
        or rel.suffix == ".pyc"
        or rel.name == ".DS_Store"
    )


def build_native(src_root: Path, out_root: Path, taken: set[str]) -> dict:
    """Copy an OMP-only plugin and return its catalog entry."""
    manifest_path = src_root / ".omp-plugin" / "plugin.json"
    if not manifest_path.is_file():
        raise BuildError(f"{src_root}: missing .omp-plugin/plugin.json")
    manifest = json.loads(manifest_path.read_text())
    missing = NATIVE_MANIFEST_KEYS - set(manifest)
    if missing:
        raise BuildError(f"{manifest_path}: missing keys {sorted(missing)}")
    name = manifest["name"]
    if name != src_root.name:
        raise BuildError(f"{manifest_path}: name {name!r} must equal its directory {src_root.name!r}")
    if name in taken:
        raise BuildError(f"native plugin {name!r} collides with a generated plugin")
    package_path = src_root / "package.json"
    if package_path.is_file():
        package = json.loads(package_path.read_text())
        if package.get("name") != name or package.get("version") != manifest["version"]:
            raise BuildError(f"{package_path}: name and version must equal {name!r} and {manifest['version']!r} from .omp-plugin/plugin.json")
        entries = (package.get("omp") or {}).get("extensions")
        if not isinstance(entries, list) or not entries:
            raise BuildError(f"{package_path}: omp.extensions must list the extension entry files")
        for entry in entries:
            if not isinstance(entry, str) or not (src_root / entry).is_file():
                raise BuildError(f"{package_path}: omp.extensions entry {entry!r} is not a file in the plugin")

    for src in sorted((src_root / "agents").glob("*.md")):
        fields = dict(split_frontmatter(src.read_text(), src)[0])
        extra = set(fields) - NATIVE_AGENT_KEYS
        if extra:
            raise BuildError(f"{src}: unknown OMP agent frontmatter keys {sorted(extra)}")
        if not unquote(fields.get("name", "")).startswith(f"{name}:"):
            raise BuildError(f"{src}: agent name must start with {name + ':'!r}")
    for src in sorted((src_root / "skills").glob("*/SKILL.md")):
        expected = f"{name}:{src.parent.name}"
        if unquote(dict(split_frontmatter(src.read_text(), src)[0]).get("name", "")) != expected:
            raise BuildError(f"{src}: skill name must be {expected!r}")

    for src in sorted(src_root.rglob("*")):
        rel = src.relative_to(src_root)
        if src.is_file() and not native_skipped(rel):
            copy(src, out_root / name / rel, out_root)
    return {
        "name": name,
        "source": f"./{name}",
        "description": manifest["description"],
        "version": manifest["version"],
        "category": manifest["category"],
    }


def build(dest_repo: Path) -> None:
    preamble = PREAMBLE_PATH.read_text()
    catalog = json.loads(CLAUDE_CATALOG.read_text())
    entries = {p["name"]: p for p in catalog["plugins"]}
    out_root = dest_repo / OUTPUT_DIR_NAME
    omp_plugins = []

    for overlay_path in sorted(OVERLAY_DIR.glob("*.json")):
        overlay = json.loads(overlay_path.read_text())
        extra = set(overlay) - OVERLAY_KEYS
        if extra:
            raise BuildError(f"{overlay_path}: unknown overlay keys {sorted(extra)}")
        plugin = overlay["plugin"]
        if plugin != overlay_path.stem:
            raise BuildError(f"{overlay_path}: 'plugin' must equal the file name")
        if plugin not in entries:
            raise BuildError(f"{overlay_path}: {plugin!r} is not in the Claude catalog")
        src_root = SOURCE_ROOT / plugin
        dst_root = out_root / plugin

        for name in VERBATIM_DIRS:
            if (src_root / name).is_dir():
                for src in sorted((src_root / name).rglob("*")):
                    if src.is_file():
                        copy(src, dst_root / src.relative_to(src_root), out_root)

        skills_root = src_root / "skills"
        skill_names: set[str] = set()
        if skills_root.is_dir():
            for src in sorted(skills_root.rglob("*")):
                if not src.is_file():
                    continue
                dst = dst_root / src.relative_to(src_root)
                if src.name == "SKILL.md" and src.parent.parent == skills_root:
                    skill_names.add(src.parent.name)
                    write(dst, build_skill(src, plugin), out_root)
                else:
                    copy(src, dst, out_root)

        for src in sorted((src_root / "commands").glob("*.md")):
            write(dst_root / "commands" / src.name, build_command(src, plugin, preamble), out_root)

        seen = set()
        for src in sorted((src_root / "agents").glob("*.md")):
            seen.add(dict(split_frontmatter(src.read_text(), src)[0])["name"])
            write(
                dst_root / "agents" / src.name,
                build_agent(src, plugin, overlay, preamble, skill_names),
                out_root,
            )
        stale = set(overlay["agents"]) - seen
        if stale:
            raise BuildError(f"{overlay_path}: overlay names agents that do not exist: {sorted(stale)}")

        manifest = json.loads((src_root / ".claude-plugin" / "plugin.json").read_text())
        write(
            dst_root / ".omp-plugin" / "plugin.json",
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            out_root,
        )
        entry = entries[plugin]
        omp_plugins.append(
            {
                "name": plugin,
                "source": f"./{plugin}",
                "description": entry["description"],
                "version": entry["version"],
                "category": entry["category"],
            }
        )

    if NATIVE_DIR.is_dir():
        taken = set(entries) | {entry["name"] for entry in omp_plugins}
        for native in sorted(p for p in NATIVE_DIR.iterdir() if p.is_dir()):
            omp_plugins.append(build_native(native, out_root, taken))
    omp_plugins.sort(key=lambda entry: entry["name"])

    omp_catalog = {
        "name": catalog["name"],
        "owner": catalog["owner"],
        "metadata": {
            "description": "OMP edition, generated from .claude-plugin/marketplace.json by scripts/build_omp_edition.py",
            "pluginRoot": f"./{OUTPUT_DIR_NAME}",
        },
        "plugins": omp_plugins,
    }
    # The catalog is the only output outside plugins-omp/, at a fixed path.
    catalog_path = dest_repo / OMP_CATALOG_REL
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps(omp_catalog, indent=2, ensure_ascii=False) + "\n")


def diff_trees(expected: Path, actual: Path) -> list[str]:
    problems: list[str] = []

    def walk(cmp: filecmp.dircmp, rel: Path) -> None:
        problems.extend(f"missing: {rel / n}" for n in cmp.left_only)
        problems.extend(f"unexpected: {rel / n}" for n in cmp.right_only)
        _, mismatch, errors = filecmp.cmpfiles(cmp.left, cmp.right, cmp.common_files, shallow=False)
        problems.extend(f"stale: {rel / n}" for n in mismatch + errors)
        for name, sub in cmp.subdirs.items():
            walk(sub, rel / name)

    if not actual.exists():
        return [f"missing: {actual.name}/"]
    walk(filecmp.dircmp(expected, actual), Path(actual.name))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed output is stale")
    args = parser.parse_args()
    try:
        if not args.check:
            shutil.rmtree(REPO / OUTPUT_DIR_NAME, ignore_errors=True)
            build(REPO)
            print(f"wrote {OUTPUT_DIR_NAME}/ and {OMP_CATALOG_REL}")
            return 0
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp))
            problems = diff_trees(Path(tmp) / OUTPUT_DIR_NAME, REPO / OUTPUT_DIR_NAME)
            committed = REPO / OMP_CATALOG_REL
            if not committed.exists():
                problems.append(f"missing: {OMP_CATALOG_REL}")
            elif not filecmp.cmp(Path(tmp) / OMP_CATALOG_REL, committed, shallow=False):
                problems.append(f"stale: {OMP_CATALOG_REL}")
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print("OMP edition is stale: run python3 scripts/build_omp_edition.py", file=sys.stderr)
            return 1
        print("OMP edition is up to date")
        return 0
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
