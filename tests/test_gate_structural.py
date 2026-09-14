from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchsmith.gate import FAIL, PASS, Report, check_structural


class StructuralGateTest(unittest.TestCase):
    def setUp(self):
        self.task = Path(tempfile.mkdtemp())

    @staticmethod
    def response(checks):
        class Result:
            stdout = json.dumps({"structural": {"checks": checks}})
            stderr = ""
            returncode = 0

        return Result()

    @patch("benchsmith.gate.subprocess.run")
    def test_upstream_warning_is_preserved_without_blocking(self, run):
        run.return_value = self.response(
            [
                {"name": "Structure", "status": "PASS", "details": "complete"},
                {
                    "name": "Internet Access",
                    "status": "WARN",
                    "details": "private-repo dependency advisory",
                },
            ]
        )
        report = Report()

        check_structural(self.task, report)

        self.assertTrue(report.ok)
        self.assertEqual(report.checks[0].state, PASS)
        self.assertIn("warnings: Internet Access", report.checks[0].detail)
        self.assertIn("private-repo dependency advisory", report.checks[0].detail)

    @patch("benchsmith.gate.subprocess.run")
    def test_warning_does_not_mask_a_real_failure(self, run):
        run.return_value = self.response(
            [
                {"name": "Internet Access", "status": "WARN", "details": "advisory"},
                {"name": "Structure", "status": "FAIL", "details": "missing task.toml"},
            ]
        )
        report = Report()

        check_structural(self.task, report)

        self.assertFalse(report.ok)
        self.assertEqual(report.checks[0].state, FAIL)
        self.assertIn("Structure: missing task.toml", report.checks[0].detail)
        self.assertIn("warnings: Internet Access", report.checks[0].detail)

    @patch("benchsmith.gate.subprocess.run")
    def test_unknown_upstream_status_fails_closed(self, run):
        run.return_value = self.response(
            [{"name": "Structure", "status": "MYSTERY", "details": "new state"}]
        )
        report = Report()

        check_structural(self.task, report)

        self.assertFalse(report.ok)
        self.assertEqual(report.checks[0].state, FAIL)
        self.assertIn("Structure: new state", report.checks[0].detail)


if __name__ == "__main__":
    unittest.main()
