#!/usr/bin/env python3
"""Routing contract for delivery tasks: which developer agent owns a task.

Run: python3 omp/native/delivery/tests/test_route_task.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from route_task import check, layout, parse_plan, route  # noqa: E402

ROUTER = SCRIPTS / "route_task.py"


def write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class RoutingFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        write(cls.root, "backend/pyproject.toml", '[project]\nname = "api"\ndependencies = ["fastapi"]\n')
        write(cls.root, "web/package.json", json.dumps({"dependencies": {"react": "^19", "@tanstack/react-query": "^5"}}))
        write(cls.root, "shop/composer.json", json.dumps({"require": {"symfony/framework-bundle": "^7"}}))
        write(cls.root, "tools/package.json", json.dumps({"dependencies": {"commander": "^12"}}))
        write(cls.root, "services/reports/pyproject.toml", '[project]\nname = "reports"\n')
        write(cls.root, "services/reports/web/package.json", json.dumps({"dependencies": {"react": "^19"}}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def routed(self, *paths: str) -> dict:
        return route(self.root, list(paths))


class RouteTest(RoutingFixture):
    def test_python_backend_task(self) -> None:
        r = self.routed("backend/app/orders.py", "backend/tests/test_orders.py")
        self.assertEqual((r["stack"], r["agent"]), ("python", "python-developer:developer"))

    def test_react_task(self) -> None:
        r = self.routed("web/src/List.tsx", "web/src/List.test.tsx")
        self.assertEqual((r["stack"], r["agent"]), ("frontend", "frontend-developer:developer"))

    def test_php_task(self) -> None:
        r = self.routed("shop/src/Controller/OrderController.php")
        self.assertEqual((r["stack"], r["agent"]), ("php", "php-developer:developer"))

    def test_new_file_in_missing_directory(self) -> None:
        self.assertEqual(self.routed("backend/app/billing/invoices.py")["stack"], "python")

    def test_backticks_and_line_range_are_stripped(self) -> None:
        r = self.routed("`backend/app/orders.py:120-145`")
        self.assertEqual(r["stack"], "python")
        self.assertEqual(r["files"], ["backend/app/orders.py"])

    def test_two_stacks_split(self) -> None:
        r = self.routed("backend/app/orders.py", "web/src/api.ts")
        self.assertEqual(r["stack"], "split")
        self.assertIsNone(r["agent"])
        self.assertEqual(set(r["groups"]), {"python", "frontend"})

    def test_docs_only_is_generic(self) -> None:
        r = self.routed("docs/a.md", "README.md")
        self.assertEqual((r["stack"], r["agent"]), ("generic", "delivery:implementer"))

    def test_node_without_react_is_generic(self) -> None:
        self.assertEqual(self.routed("tools/cli.ts")["stack"], "generic")

    def test_nearest_manifest_wins_for_nested_react_app(self) -> None:
        self.assertEqual(self.routed("services/reports/web/src/Chart.tsx")["stack"], "frontend")

    def test_python_file_of_nested_service(self) -> None:
        self.assertEqual(self.routed("services/reports/build.py")["stack"], "python")

    def test_python_file_outside_any_project(self) -> None:
        self.assertEqual(self.routed("scripts/gen.py")["stack"], "python")

    def test_web_file_under_python_project_is_generic(self) -> None:
        self.assertEqual(self.routed("backend/app/widget.ts")["stack"], "generic")

    def test_docs_beside_code_do_not_vote(self) -> None:
        r = self.routed("backend/app/orders.py", "docs/orders.md", "backend/pyproject.toml")
        self.assertEqual(r["stack"], "python")

    def test_no_paths_is_unknown(self) -> None:
        r = self.routed()
        self.assertEqual(r["stack"], "unknown")
        self.assertIsNone(r["agent"])


PLAN = """# Plan

## Goal
Something.

### Task 1: Orders endpoint
**Commit:** feat: add orders endpoint

**Files:**
- Create: `backend/app/orders.py`
- Modify: `backend/app/main.py:10-20`
- Test: `backend/tests/test_orders.py`

Steps.

### Task 2: Docs
**Files:**
- Create: docs/orders.md

## Notes
Not part of task 2: `- Create: ignored.py`
"""


class ParsePlanTest(unittest.TestCase):
    def test_tasks_paths_and_commit(self) -> None:
        tasks = parse_plan(PLAN)
        self.assertEqual([t["task"] for t in tasks], [1, 2])
        self.assertEqual(tasks[0]["title"], "Orders endpoint")
        self.assertEqual(tasks[0]["commit"], "feat: add orders endpoint")
        self.assertEqual(
            tasks[0]["paths"],
            ["backend/app/orders.py", "backend/app/main.py:10-20", "backend/tests/test_orders.py"],
        )
        self.assertIsNone(tasks[1]["commit"])
        self.assertEqual(tasks[1]["paths"], ["docs/orders.md"])
        self.assertNotIn("Notes", tasks[1]["block"])

    def test_fenced_markdown_stays_inside_the_task(self) -> None:
        tasks = parse_plan(FENCED_PLAN)
        self.assertEqual([t["task"] for t in tasks], [1])
        self.assertEqual(tasks[0]["paths"], ["README.md"])
        self.assertIn("## Usage", tasks[0]["block"])
        self.assertTrue(tasks[0]["block"].endswith("Append it after the intro."), tasks[0]["block"])


FENCED_PLAN = """# Plan

### Task 1: Usage docs
**Files:**
- Modify: `README.md`

````markdown
## Usage

```python
from calc.ops import add
```

### Task 9: Not a task
- Create: `not/a/task/file.py`
````

Append it after the intro.

## Verification
- pytest
"""


SPLIT_PLAN = """# Plan

### Task 1: Orders
**Files:**
- Modify: `backend/app/orders.py`
- Modify: `web/src/api.ts`

### Task 2: Notes
Write the release notes.

### Task 2: Docs
**Files:**
- Create: `docs/orders.md`
"""


class CheckTest(RoutingFixture):
    def test_reports_every_unroutable_task(self) -> None:
        result = check(self.root, SPLIT_PLAN)
        self.assertEqual(result["tasks"], 3)
        problems = result["problems"]
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(problems[0].startswith("Task 1 (Orders): touches several stacks"), problems[0])
        self.assertIn("python: backend/app/orders.py; frontend: web/src/api.ts", problems[0])
        self.assertTrue(problems[1].startswith("Task 2 (Notes): lists no files"), problems[1])
        self.assertTrue(problems[2].startswith("Task 2 appears 2 times"), problems[2])

    def test_routable_plan_has_no_problems(self) -> None:
        self.assertEqual(check(self.root, PLAN), {"tasks": 2, "problems": []})


class LayoutTest(RoutingFixture):
    def test_stacks_and_framework_evidence(self) -> None:
        result = layout(self.root)
        self.assertEqual(result["stacks"], ["python", "frontend", "php"])
        self.assertIn("backend/: python (fastapi)", result["lines"])
        self.assertIn("tools/: node (package.json)", result["lines"])


class CliTest(RoutingFixture):
    def run_router(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROUTER), *args], capture_output=True, text=True)

    def test_plan_without_tasks_exits_2(self) -> None:
        write(self.root, "empty-plan.md", "# Nothing here\n")
        result = self.run_router("plan", str(self.root), "empty-plan.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no '### Task N:' headings", result.stderr)

    def test_check_without_tasks_reports_zero(self) -> None:
        write(self.root, "empty-plan.md", "# Nothing here\n")
        result = self.run_router("check", str(self.root), "empty-plan.md")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"tasks": 0, "problems": []})

    def test_plan_routes_every_task(self) -> None:
        write(self.root, "plan.md", PLAN)
        result = self.run_router("plan", str(self.root), "plan.md")
        self.assertEqual(result.returncode, 0, result.stderr)
        routed = json.loads(result.stdout)
        self.assertEqual([(t["task"], t["agent"]) for t in routed], [(1, "python-developer:developer"), (2, "delivery:implementer")])


if __name__ == "__main__":
    unittest.main()
