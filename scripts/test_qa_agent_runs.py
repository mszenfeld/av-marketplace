#!/usr/bin/env python3
"""Contract tests for QA agent transcript metrics.

Run: python3 scripts/test_qa_agent_runs.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qa_agent_runs import DEFAULT_AGENTS
from qa_agent_runs import HEAD_LINES
from qa_agent_runs import advisor_stats
from qa_agent_runs import canonical_path
from qa_agent_runs import classify_path
from qa_agent_runs import iter_transcripts
from qa_agent_runs import parse_result
from qa_agent_runs import parse_run
from qa_agent_runs import read_head
from qa_agent_runs import repo_label
from qa_agent_runs import strip_selector
from qa_agent_runs import summarize
from qa_agent_runs import token_map
from qa_agent_runs import unmask


CWD = "/checkout/project"
PLAN = "docs/testing/plans/example.md"
SCRIPT = Path(__file__).with_name("qa_agent_runs.py")
RUN_KEYS = set("""
session_id path duplicates bad_lines agent mode round cwd repo parent_dir plan_path
cycle_key start end disposed_at wall_s llm_s omp_versions model model_role thinking_level
effort fallback fallback_turn advisor agent_prompt_sha context_sha system_prompt_chars
non_message_tokens turns tool_calls output_tokens input_tokens cache_read_tokens
cache_write_tokens cost_usd probe_cost_usd total_cost_usd first_context_tokens
max_context_tokens long_turns long_turn_llm_s compactions plan_write_attempts
plan_writes_ok first_plan_write_turn first_plan_write_s plan_write_bytes budget_notice
parent_irc_messages turns_before_parent_irc first_parent_irc_turn masked_tokens result
changed_files changed_files_count changed_files_unresolved scenarios findings_by_severity
advisor_cost_usd advisor_turns advisor_notes advisor_notes_by_severity
advisor_notes_delivered advisor_notes_queued advisor_text_messages
""".split())
TURN_KEYS = set("""
session_id index timestamp duration_s ttft_s output_tokens context_tokens cost_usd tools
read_paths read_unresolved tool_result_chars thinking_chars before_first_plan_write
before_parent_irc
""".split())


def stamp(second: int) -> str:
    return f"2026-11-03T00:{second // 60:02d}:{second % 60:02d}.000Z"


def headers(agent: str = "qa:test-planner", task: str = "Mode: draft", **extra: object) -> list[dict]:
    init = {
        "type": "session_init", "agent": agent, "task": task,
        "resolvedModel": "provider/model:max", "modelRole": "plan", "advisor": "on",
        "systemPrompt": ["project rules", "§ Role\nAgent instructions\n§ Context\nContext A\n§ Coop\nPeers"],
    }
    init.update(extra)
    return [
        {"type": "session", "id": "session-1", "timestamp": stamp(0), "cwd": CWD},
        {"type": "thinking_level_change", "thinkingLevel": "high"},
        {"type": "thinking_level_change", "thinkingLevel": "max"},
        init,
        message("user", "task", 0),
    ]


def message(role: str, content: str | list[dict], second: int, **extra: object) -> dict:
    return {"type": "message", "timestamp": stamp(second),
            "message": {"role": role, "content": content, **extra}}


def call(name: str, identifier: str, **arguments: object) -> dict:
    return {"type": "toolCall", "name": name, "id": identifier, "arguments": arguments}


def turn(second: int, content: str | list[dict] = "", output: int = 100,
         cost: float = 0.25, model: str = "model", **extra: object) -> dict:
    return message(
        "assistant", content, second, model=model, duration=2000, ttft=500,
        usage={"input": 10, "output": output, "cacheRead": 20, "cacheWrite": 30,
               "cost": {"total": cost}},
        requestControls={"effort": {"tail": "max", "topLevel": "max"}},
        contextSnapshot={"nonMessageTokens": 55}, **extra,
    )


def result(identifier: str, text: str = "done", **extra: object) -> dict:
    return message("toolResult", [{"type": "text", "text": text}], 0,
                   toolCallId=identifier, **extra)


def transcript(root: Path, entries: list[dict], name: str = "Whatever.jsonl") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
                    encoding="utf-8")
    return path


class AgentRunsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def parse(self, entries: list[dict], name: str = "Whatever.jsonl",
              threshold: int = 10_000) -> tuple[dict, list[dict]]:
        return parse_run(transcript(self.root, entries, name), threshold, {})

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, HOME=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, check=False)

    def test_head_identifies_agent_not_filename_and_limits_lines(self) -> None:
        planner = transcript(self.root, headers())
        scout = transcript(self.root, headers("scout"), "QAPlanDraft.jsonl")
        self.assertEqual(read_head(planner, set(DEFAULT_AGENTS)), ("session-1", "qa:test-planner"))
        self.assertIsNone(read_head(scout, set(DEFAULT_AGENTS)))
        late = transcript(self.root, [{"type": "title"}] * HEAD_LINES + headers(), "late.jsonl")
        self.assertIsNone(read_head(late, set(DEFAULT_AGENTS)))
        planner.write_text("broken\n" + planner.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(read_head(planner, set(DEFAULT_AGENTS))[0], "session-1")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertIsNone(read_head(self.root / "missing.jsonl", set(DEFAULT_AGENTS)))
        self.assertIn("missing.jsonl", stderr.getvalue())

    def test_discovery_skips_advisor_and_directory_symlinks(self) -> None:
        normal = transcript(self.root, headers(), "nested/run.jsonl")
        transcript(self.root, headers(), "nested/__advisor.jsonl")
        outside = self.root / "outside"
        transcript(outside, headers())
        (self.root / "nested/link").symlink_to(outside, target_is_directory=True)
        self.assertEqual(list(iter_transcripts([self.root / "nested", self.root / "missing"])), [normal])

    def test_cli_deduplicates_by_session_id_and_appends_roots(self) -> None:
        first = transcript(self.root / ".omp/agent/sessions/a", headers())
        transcript(self.root / "z", headers())
        transcript(self.root / "z", headers("scout"), "QAPlanDraft.jsonl")
        output = self.cli("--root", str(self.root / "z"))
        self.assertEqual(output.returncode, 0, output.stderr)
        rows = [json.loads(line) for line in output.stdout.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], str(first))
        self.assertEqual(rows[0]["duplicates"], 1)

    def test_duplicate_path_order_is_lexical_not_component_order(self) -> None:
        first = transcript(self.root / "sessions", headers(), "a.jsonl")
        transcript(self.root / "sessions/a", headers())
        output = self.cli("--root", str(self.root / "sessions"))
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(json.loads(output.stdout)["path"], str(first))

    def test_draft_metrics_end_and_exact_record_keys(self) -> None:
        final = {"plan": PLAN, "changed_files": ["src/a.py", "tests/test_a.py"],
                 "fe_scenarios": 2, "be_scenarios": 3, "desktop_scenarios": 4}
        entries = headers() + [
            turn(10, output=10_000), {"type": "compaction"},
            message("user", "[budget notice] stop soon", 11),
            turn(20, [{"type": "thinking", "thinking": "thought"},
                      {"type": "text", "text": "Result:\n```json\n" + json.dumps(final) + "\n```"}],
                 output=10_001),
            {"type": "session_exit", "timestamp": stamp(80)},
        ]
        entries[5]["runtime"] = {"version": "18.2"}
        entries[-2]["runtime"] = {"version": "18.1"}
        run, turns = self.parse(entries)
        self.assertEqual(set(run), RUN_KEYS)
        self.assertEqual(set(turns[0]), TURN_KEYS)
        self.assertEqual(run["mode"], "draft")
        self.assertEqual(run["turns"], 2)
        self.assertEqual(run["long_turns"], 1)
        self.assertEqual(run["long_turn_llm_s"], 2)
        self.assertEqual(run["compactions"], 1)
        self.assertTrue(run["budget_notice"])
        self.assertEqual(run["changed_files_count"], 2)
        self.assertEqual(run["scenarios"], 9)
        self.assertEqual(run["end"], stamp(20))
        self.assertEqual(run["disposed_at"], stamp(80))
        self.assertEqual(run["wall_s"], 20)
        self.assertEqual(run["llm_s"], 4)
        self.assertEqual(run["omp_versions"], ["18.1", "18.2"])
        self.assertEqual(run["model_role"], "plan")
        self.assertEqual(run["thinking_level"], "max")
        self.assertEqual(run["non_message_tokens"], 55)
        self.assertEqual(run["effort"], ['{"tail": "max", "topLevel": "max"}'])
        self.assertEqual(run["output_tokens"], 20_001)
        self.assertEqual(run["input_tokens"], 20)
        self.assertEqual(run["cache_read_tokens"], 40)
        self.assertEqual(run["cache_write_tokens"], 60)
        self.assertEqual(run["first_context_tokens"], 60)
        self.assertEqual(run["max_context_tokens"], 60)
        self.assertEqual(turns[1]["thinking_chars"], 7)
        self.assertEqual(turns[0]["ttft_s"], 0.5)
        self.assertEqual(run["plan_path"], PLAN)
        self.assertEqual(run["cycle_key"], f"{self.root}|{PLAN}")

    def test_plan_write_success_and_byte_count_prefer_tool_result(self) -> None:
        run, turns = self.parse(headers() + [
            turn(10),
            turn(20, [call("write", "bad", path=PLAN, content="bad")]),
            result("bad", "error", isError=True),
            turn(30, [call("write", "ok", path=PLAN, content="different length")]),
            result("ok", "Successfully wrote 1234 bytes"),
            turn(40, [call("edit", "edit", input=f"[{PLAN}#AB12]\nPUT >1:\n+x")]),
            result("edit"),
            turn(50, [call("write", "missing", path="src/x.py", content="not a plan")]),
        ])
        self.assertEqual(run["plan_write_attempts"], 3)
        self.assertEqual(run["plan_writes_ok"], 2)
        self.assertEqual(run["first_plan_write_turn"], 3)
        self.assertEqual(run["first_plan_write_s"], 30)
        self.assertEqual(run["plan_write_bytes"], 1234)
        self.assertEqual([t["before_first_plan_write"] for t in turns], [True, True, False, False, False])
        self.assertEqual(turns[2]["tool_result_chars"], len("Successfully wrote 1234 bytes"))
        self.assertEqual(run["tool_calls"], {"write": 3, "edit": 1})

    def test_exact_two_write_attempts_and_utf8_fallback(self) -> None:
        for reply, content, expected in (("Successfully wrote 1234 bytes", "x", 1234),
                                         ("saved", "zażółć", len("zażółć".encode("utf-8")))):
            with self.subTest(reply=reply):
                run, _ = self.parse(headers() + [turn(10),
                    turn(20, [call("write", "bad", path=PLAN, content="x")]),
                    result("bad", isError=True),
                    turn(30, [call("write", "ok", path=PLAN, content=content)]), result("ok", reply)])
                self.assertEqual(run["plan_write_attempts"], 2)
                self.assertEqual(run["plan_writes_ok"], 1)
                self.assertEqual(run["first_plan_write_turn"], 3)
                self.assertEqual(run["plan_write_bytes"], expected)
        run, _ = self.parse(headers() + [turn(10, [call("write", "pending", path=PLAN, content="x")])])
        self.assertIsNone(run["first_plan_write_turn"])
        self.assertIsNone(run["plan_write_bytes"])

    def test_parent_irc_after_initial_user_message(self) -> None:
        entries = headers()
        entries[-1] = message("user", '<irc from="parent">initial task', 0)
        entries += [turn(10), turn(20), turn(30),
                    message("user", '<irc from="parent">finish now', 35), turn(40)]
        run, turns = self.parse(entries)
        self.assertEqual(run["parent_irc_messages"], 1)
        self.assertEqual(run["turns_before_parent_irc"], 3)
        self.assertEqual(run["first_parent_irc_turn"], 4)
        self.assertEqual([t["before_parent_irc"] for t in turns], [True, True, True, False])

    def test_cost_includes_probes_and_all_advisor_files(self) -> None:
        path = transcript(self.root, headers() + [turn(10, cost=1.25),
            {"type": "model_usage", "usage": {"cost": {"total": 0.5}}}])
        transcript(path.with_suffix(""), [turn(1, cost=2),
            {"type": "model_usage", "usage": {"cost": {"total": 0.75}}}], "__advisor.jsonl")
        transcript(path.with_suffix(""), [turn(1, cost=0.25)], "__advisor.other.jsonl")
        run, _ = parse_run(path, 10_000, {})
        self.assertEqual(run["cost_usd"], 1.25)
        self.assertEqual(run["probe_cost_usd"], 0.5)
        self.assertEqual(run["advisor_cost_usd"], 3)
        self.assertEqual(run["total_cost_usd"], 4.75)

    def test_token_reconstruction_and_read_paths(self) -> None:
        masked = "src/$$AB12:L$$_engine/x/$$AB12:L$$s.py"
        clear = "src/workflow_engine/x/workflows.py"
        entries = headers() + [
            turn(10, [call("read", "masked", path=masked + ":1-5")]),
            result("masked", details={"meta": {"source": {"type": "path", "value": CWD + "/" + clear}}}),
            turn(20, [call("read", "clear", path=clear + ":raw:3-4")]), result("clear"),
            turn(30, [call("read", "internal", path="artifact://392"),
                      call("read", "unresolved", path="src/$$UNKNOWN$$.py"),
                      call("read", "directory", path="src/"),
                      call("read", "url", path="https://example.com")]),
            result("internal", details={"meta": {"source": {"type": "internal", "value": "artifact://392"}}}),
            turn(40, json.dumps({"changed_files": [masked, "src/$$UNKNOWN$$.py"]})),
        ]
        self.assertEqual(token_map(entries, CWD), {"$$AB12:L$$": "workflow"})
        self.assertEqual(unmask(masked, token_map(entries, CWD)), clear)
        run, turns = self.parse(entries)
        self.assertEqual(turns[0]["read_paths"], [clear])
        self.assertEqual(turns[1]["read_paths"], [clear])
        self.assertEqual(turns[2]["read_paths"], [])
        self.assertEqual(turns[2]["read_unresolved"], 1)
        self.assertEqual(run["changed_files"], [clear, "src/$$UNKNOWN$$.py"])
        self.assertEqual(run["changed_files_unresolved"], 1)
        self.assertEqual(run["masked_tokens"], 2)

    def test_conflicting_tokens_and_repeated_token_backreferences(self) -> None:
        entries = headers() + [
            turn(1, [call("read", "a", path="src/$$A$$/$$A$$.py")]),
            result("a", details={"meta": {"source": {"type": "path", "value": CWD + "/src/one/one.py"}}}),
            turn(2, [call("read", "b", path="src/$$A$$/$$A$$.py")]),
            result("b", details={"meta": {"source": {"type": "path", "value": CWD + "/src/two/two.py"}}}),
            turn(3, [call("read", "c", path="src/$$B$$/$$B$$.py")]),
            result("c", details={"meta": {"source": {"type": "path", "value": CWD + "/src/one/two.py"}}}),
        ]
        self.assertEqual(token_map(entries, CWD), {})

    def test_selectors_and_canonical_paths(self) -> None:
        for raw, expected in (("src/a.py:10-20", "src/a.py"), ("src/a.py:raw:3-4", "src/a.py"),
                              ("./src/c.py:5", "src/c.py"), ("././src/c.py:img", "src/c.py"),
                              ("src/c.py:conflicts", "src/c.py"), ("src/c.py:-5:raw", "src/c.py"),
                              ("https://example.com", "https://example.com")):
            with self.subTest(raw=raw):
                self.assertEqual(strip_selector(raw), expected)
        self.assertEqual(canonical_path(CWD + "/src/x.py", CWD), "src/x.py")
        self.assertEqual(canonical_path(CWD + "-other/x.py", CWD), CWD + "-other/x.py")
        self.assertEqual(canonical_path("relative.py", CWD), "relative.py")
        self.assertEqual(canonical_path("/external/x.py", None), "/external/x.py")

    def test_reviewer_yield_and_revise_mode(self) -> None:
        data = {"findings": [{"severity": "concern"}, {"severity": "concern"}, {"severity": "nit"}]}
        run, _ = self.parse(headers("qa:test-plan-reviewer", f"Round: 2 of 3\nPlan: {PLAN}") +
                            [turn(1, [call("yield", "y", data=data)])])
        self.assertEqual(run["mode"], "review")
        self.assertEqual(run["round"], 2)
        self.assertEqual(run["plan_path"], PLAN)
        self.assertEqual(run["findings_by_severity"], {"concern": 2, "nit": 1})
        run, _ = self.parse(headers(task="Mode: revise") + [turn(1)])
        self.assertEqual(run["mode"], "revise")

    def test_result_recovery_is_not_completion_classification(self) -> None:
        cases = [
            ([turn(1, [call("yield", "a", data={"plan": "a"})]),
              turn(2, [call("yield", "b", data={"error": "cannot write"})])], {"error": "cannot write"}),
            ([turn(1, '{"intermediate": true}'), turn(2, "runtime stopped")], {"intermediate": True}),
            ([turn(1, "runtime stopped")], None),
            ([turn(1, '[1, 2]')], None),
            ([turn(1, 'text {"error": "bad source"} trailing')], {"error": "bad source"}),
        ]
        for entries, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(parse_result(entries), expected)
                run, _ = self.parse(headers() + entries)
                self.assertEqual(run["result"], expected)
                self.assertNotIn("status", run)
                self.assertFalse(run["budget_notice"])

    def test_path_classification_excludes_dependencies_inside_checkout(self) -> None:
        changed = {"src/a.py", ".venv/lib/x.py"}
        cases = ((".venv/lib/python3.14/site-packages/fastapi/security/http.py", "dependency"),
                 (CWD + "/.venv/lib/x.py", "dependency"), (".venv/lib/x.py", "dependency"),
                 ("venv/lib/x.py", "dependency"), ("node_modules/pkg/index.js", "dependency"),
                 ("/opt/site-packages/pkg.py", "dependency"), ("src/a.py", "diff"),
                 ("tests/unit/test_x.py", "test"), ("src/x.test.ts", "test"),
                 ("docs/adr/1.md", "docs"), ("README.md", "docs"), ("docs/schema.json", "docs"),
                 ("/opt/x/guide.md", "external"), ("src/$$A$$.py", "unknown"),
                 ("src/b.py", "other"), ("myvenv/x.py", "other"))
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(classify_path(path, changed), expected)
        dependency = canonical_path(CWD + "/.venv/lib/python3.14/site-packages/fastapi/security/http.py", CWD)
        self.assertEqual(classify_path(dependency, changed), "dependency")

    def test_advisor_notes_are_calls_not_assistant_prose(self) -> None:
        path = transcript(self.root, headers())
        transcript(path.with_suffix(""), [
            turn(1, [call("advise", "a", severity="concern", note="check")]), result("a", "Delivered."),
            turn(2, "No actionable issue"),
        ], "__advisor.jsonl")
        stats = advisor_stats(path)
        self.assertEqual(stats["advisor_notes"], 1)
        self.assertEqual(stats["advisor_notes_delivered"], 1)
        self.assertEqual(stats["advisor_notes_by_severity"], {"concern": 1})
        self.assertEqual(stats["advisor_text_messages"], 1)
        transcript(path.with_suffix(""), [turn(3, [call("advise", "b", severity="nit")]),
                   result("b", "Queued for the end of the turn. Do not re-raise.")], "__advisor.second.jsonl")
        self.assertEqual(advisor_stats(path)["advisor_notes_queued"], 1)
        missing = advisor_stats(self.root / "no-advisor.jsonl")
        self.assertEqual(missing["advisor_notes_by_severity"], {})
        self.assertEqual(missing["advisor_cost_usd"], 0)

    def test_prompt_hashes_exclude_context_plan_and_cooperation(self) -> None:
        for section in ("§ Context", "§ Plan"):
            with self.subTest(section=section):
                prompt = f"§ Role\nInstructions\n{section}\nA\n§ Coop\nPeers"
                first, _ = self.parse(headers(systemPrompt=prompt))
                second, _ = self.parse(headers(systemPrompt=prompt.replace("\nA\n", "\nB\n")))
                self.assertEqual(first["agent_prompt_sha"], second["agent_prompt_sha"])
                self.assertNotEqual(first["context_sha"], second["context_sha"])
                self.assertEqual(first["agent_prompt_sha"], hashlib.sha256("§ Role\nInstructions".encode()).hexdigest()[:12])
                self.assertEqual(first["system_prompt_chars"], [len(prompt)])
        run, _ = self.parse(headers(systemPrompt=["plain", "last instructions"]))
        self.assertIsNone(run["context_sha"])
        self.assertEqual(run["agent_prompt_sha"], hashlib.sha256(b"last instructions").hexdigest()[:12])

    def test_repo_labels(self) -> None:
        cases = (("/Users/me/.omp/wt/kb-market-place-t_ab12cd34/src", "market-place"),
                 ("/private/tmp/x", "tmp"), ("/tmp/x", "tmp"),
                 ("/home/me/wt/branch-abcdef0", "project"), ("/home/me/wt/branch-1234567/", "wt-1234567"),
                 ("/home/me/branch-1234567", "branch-1234567"), ("/home/me/repo/", "repo"), (None, "unknown"))
        for cwd, expected in cases:
            with self.subTest(cwd=cwd):
                self.assertEqual(repo_label(cwd, {"abcdef0": "project"}), expected)

    def test_fallback_and_pre_turn_model_change(self) -> None:
        for event in ([], [{"type": "model_change", "resolvedModelIsFallback": True}]):
            with self.subTest(event=event):
                run, _ = self.parse(headers() + [turn(1), turn(2)] + event + [turn(3, model="backup")])
                self.assertTrue(run["fallback"])
                self.assertEqual(run["fallback_turn"], 3)
        run, _ = self.parse(headers() + [{"type": "model_change", "resolvedModelIsFallback": True}, turn(1)])
        self.assertEqual(run["fallback_turn"], 1)

    def test_bad_line_and_empty_run(self) -> None:
        path = transcript(self.root, headers() + [turn(10)])
        path.write_text(path.read_text(encoding="utf-8") + "not JSON\n", encoding="utf-8")
        run, _ = parse_run(path, 10_000, {})
        self.assertEqual(run["bad_lines"], 1)
        self.assertEqual(run["turns"], 1)
        run, turns = self.parse(headers())
        self.assertEqual(turns, [])
        self.assertEqual(run["end"], run["start"])
        self.assertEqual(run["wall_s"], 0)
        self.assertIsNone(run["result"])
        self.assertIsNone(run["changed_files"])
        self.assertEqual(run["changed_files_count"], 0)
        self.assertIsNone(run["scenarios"])
        self.assertIsNone(run["first_context_tokens"])

    def test_cli_filters_dates_inclusively_sorting_and_agent_override(self) -> None:
        for index, day in enumerate(("2026-11-04", "2026-11-03", "2026-11-05")):
            entries = headers()
            entries[0].update(id=f"session-{index}", timestamp=day + "T00:00:00.000Z")
            transcript(self.root / "sessions", entries, f"{index}.jsonl")
        output = self.cli("--root", str(self.root / "sessions"), "--since", "2026-11-03", "--until", "2026-11-04",
                          "--cwd-contains", "project")
        self.assertEqual(output.returncode, 0, output.stderr)
        rows = [json.loads(line) for line in output.stdout.splitlines()]
        self.assertEqual([r["start"][:10] for r in rows], ["2026-11-03", "2026-11-04"])
        scout = transcript(self.root / "scouts", headers("scout"))
        output = self.cli("--root", str(scout.parent), "--agent", "scout")
        self.assertEqual(json.loads(output.stdout)["agent"], "scout")
        output = self.cli("--root", str(self.root / "sessions"), "--cwd-contains", "not-present")
        self.assertEqual(output.stdout, "")

    def test_cli_globs_turns_threshold_and_suffix_mapping(self) -> None:
        entries = headers() + [turn(1, output=101)]
        entries[0]["cwd"] = "/home/me/wt/branch-abcdef0"
        transcript(self.root / "custom/a", entries)
        output = self.cli("--root", str(self.root / "custom/*"), "--repo-suffix", "abcdef0=project", "--long-turn-tokens", "100")
        run = json.loads(output.stdout)
        self.assertEqual(run["repo"], "project")
        self.assertEqual(run["long_turns"], 1)
        output = self.cli("--root", str(self.root / "custom/*"), "--turns")
        self.assertEqual(json.loads(output.stdout)["index"], 1)

    def test_cli_argument_errors_use_exit_code_two(self) -> None:
        for args in (("--turns", "--summary"), ("--since", "bad"), ("--until", "2026-02-30"),
                     ("--since", "2026-1-2"), ("--repo-suffix", "not-a-map"),
                     ("--repo-suffix", "abc=name"), ("--long-turn-tokens", "-1"),
                     ("--long-turn-tokens", "not-int"), ("--unknown",)):
            with self.subTest(args=args):
                output = self.cli(*args)
                self.assertEqual(output.returncode, 2)
                self.assertNotIn("Traceback", output.stderr)

    def test_summary_grouping_medians_nearest_rank_and_missing_values(self) -> None:
        runs = []
        for index in range(1, 11):
            run, _ = self.parse(headers() + [turn(index)])
            run.update(wall_s=index * 60, llm_s=index * 60, turns=index, output_tokens=index * 1000,
                       total_cost_usd=index, advisor_cost_usd=0.5, advisor_notes=1,
                       long_turns=1, long_turn_llm_s=index * 30,
                       first_plan_write_turn=1 if index == 1 else None)
            runs.append(run)
        reviewer, _ = self.parse(headers("qa:test-plan-reviewer"))
        revise, _ = self.parse(headers(task="Mode: revise"))
        table = summarize(runs + [reviewer, revise])
        lines = [[cell.strip() for cell in line.strip("|").split("|")] for line in table.splitlines()]
        draft = next(row for row in lines if row[:2] == ["qa:test-planner", "draft"])
        self.assertEqual(draft[2], "10")
        self.assertEqual(float(draft[3]), 5.5)
        self.assertEqual(float(draft[4]), 9)
        self.assertEqual(float(draft[6]), 9)
        self.assertEqual(float(draft[10]), 55)
        self.assertEqual(float(draft[14]), 0.5)
        self.assertEqual(float(draft[15]), 100)
        self.assertTrue(any(row[:2] == ["qa:test-planner", "revise"] for row in lines))
        review = next(row for row in lines if row[:2] == ["qa:test-plan-reviewer", "review"])
        self.assertEqual(review[14:], ["-", "-"])
        self.assertEqual(len(summarize([]).splitlines()), 2)
        transcript(self.root / "summary", headers())
        output = self.cli("--root", str(self.root / "summary"), "--summary")
        self.assertEqual(output.returncode, 0)
        self.assertIn("qa:test-planner | draft | 1", output.stdout)


if __name__ == "__main__":
    unittest.main()
