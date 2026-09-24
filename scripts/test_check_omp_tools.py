#!/usr/bin/env python3
"""Contract tests for the OMP built-in tool name checker.

Run: python3 scripts/test_check_omp_tools.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_omp_edition import OMP_TOOLS
from check_omp_tools import builtin_tool_names
from check_omp_tools import main


FIXTURE = 'export const BUILTIN_TOOL_NAMES = [\n\t"read",\n\t"wait",\n] as const;\n\nexport const HIDDEN_TOOL_NAMES = ["yield"] as const;\n'


def package(root: Path, names: set[str]) -> None:
    names_file = root / "src/tools/builtin-names.ts"
    names_file.parent.mkdir(parents=True)
    names_file.write_text(
        "export const BUILTIN_TOOL_NAMES = [\n"
        + "".join(f'    "{name}",\n' for name in sorted(names))
        + "] as const;\n"
    )
    (root / "package.json").write_text(json.dumps({"version": "18.3.0"}))


class CheckerTest(unittest.TestCase):
    def test_only_builtin_names_are_selected(self) -> None:
        self.assertEqual(builtin_tool_names(FIXTURE), {"read", "wait"})

    def test_missing_constant_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "BUILTIN_TOOL_NAMES not found"):
            builtin_tool_names('export const HIDDEN_TOOL_NAMES = ["yield"] as const;')

    def test_matching_package_reports_version_and_tool_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package(root, OMP_TOOLS)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = main([str(root)])

            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue(), f"OMP_TOOLS matches OMP 18.3.0 ({len(OMP_TOOLS)} tools)\n")

    def test_mismatch_reports_each_direction(self) -> None:
        cases = (
            (OMP_TOOLS | {"hub"}, "missing from OMP_TOOLS: hub"),
            (OMP_TOOLS - {"wait"}, "not in OMP: wait"),
        )
        for names, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                package(root, names)
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    result = main([str(root)])

                self.assertEqual(result, 1)
                self.assertIn(expected, stderr.getvalue())

    def test_missing_installation_explains_how_to_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = main([tmp])

            self.assertEqual(result, 2)
            self.assertIn("builtin-names.ts not found", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
