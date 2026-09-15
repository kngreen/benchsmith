import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchsmith import diffcheck
from benchsmith import gate


class ScriptPatchDiffCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.safety = mock.patch.object(
            gate.safety,
            "snapshot",
            side_effect=lambda component: {
                "component": component,
                "digest": f"sha256:{component}-test",
                "sourceHead": "a" * 40,
                "clean": True,
            },
        )
        self.safety.start()
        self.addCleanup(self.safety.stop)
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name) / "repo"
        (self.repo / "task" / "tests").mkdir(parents=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "benchsmith@test")
        self._git("config", "user.name", "Benchsmith Test")
        (self.repo / "task" / "instruction.md").write_text("initial instruction\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _patch(path: str, body: str) -> str:
        added = "\n".join("+" + line for line in body.splitlines())
        line_count = len(body.splitlines())
        return (
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{line_count} @@\n"
            f"{added}\n"
        )

    def _write_patch(self, path: str, body: str) -> None:
        patch_file = self.repo / "task" / "tests" / "test.patch"
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file.write_text(self._patch(path, body))
        self._git("add", "-A")

    def _commit_patch(self, path: str, body: str) -> None:
        self._write_patch(path, body)
        self._git("commit", "-qm", "test patch")

    def test_typescript_and_javascript_test_additions_are_examined(self) -> None:
        cases = {
            "web/src/auth.test.ts": (
                "interface User { id: string }\n"
                "test('preserves auth', (): void => {\n"
                "  const user: User = {id: '1'};\n"
                "  expect(resolveAuth(user)).toEqual(user);\n"
                "});"
            ),
            "web/src/auth.test.js": (
                "test('preserves auth', () => {\n"
                "  expect(resolveAuth({id: '1'})).toEqual({id: '1'});\n"
                "});"
            ),
            "web/src/auth.test.tsx": (
                "test('renders auth', () => {\n"
                "  const view: JSX.Element = <div data-user='1' />;\n"
                "  expect(view.type).toBe('div');\n"
                "});"
            ),
            "web/src/auth.test.jsx": (
                "test('renders auth', () => {\n"
                "  const view = <div data-user='1' />;\n"
                "  expect(view.type).toBe('div');\n"
                "});"
            ),
        }
        for path, source in cases.items():
            with self.subTest(path=path):
                self._write_patch(path, source)
                result = diffcheck.run(self.repo)
                self.assertEqual(
                    {
                        name: (row["state"], row["examined"], row["applicable"])
                        for name, row in result.items()
                    },
                    {
                        "diff-ratchet": ("PASS", 1, True),
                        "diff-weakening": ("PASS", 1, True),
                    },
                )
                self._git("reset", "-q", "--hard")
                self._git("clean", "-qfd")

    def test_removed_typescript_test_fails_ratchet(self) -> None:
        first = (
            "test('keeps the user', () => { expect(context().user).toBe('a'); });\n"
            "test('keeps the token', () => { expect(context().token).toBe('b'); });"
        )
        self._commit_patch("web/src/auth.test.ts", first)
        self._write_patch(
            "web/src/auth.test.ts",
            "test('keeps the user', () => { expect(context().user).toBe('a'); });",
        )
        result = diffcheck.run(self.repo)["diff-ratchet"]
        self.assertEqual(result["state"], "FAIL")
        self.assertIn("keeps the token", result["detail"])

    def test_typescript_assertion_broadening_and_skip_fail_weakening(self) -> None:
        strong = "test('auth', () => { expect(context().user).toBe(user); });"
        self._commit_patch("web/src/auth.test.ts", strong)
        for weakened, finding in (
            (
                "test('auth', () => { expect(context().user || user).toBe(user); });",
                "assertion broadened",
            ),
            (
                "test.skip('auth', () => { expect(context().user).toBe(user); });",
                "skip test added",
            ),
        ):
            with self.subTest(finding=finding):
                self._write_patch("web/src/auth.test.ts", weakened)
                result = diffcheck.run(self.repo)["diff-weakening"]
                self.assertEqual(result["state"], "FAIL")
                self.assertIn(finding, result["detail"])
                self._git("reset", "-q", "--hard")

    def test_moving_skip_to_duplicate_test_name_is_detected(self) -> None:
        strong = (
            "describe('A', () => {\n"
            "  test.skip('same', () => { expect(flaky()).toBe(true); });\n"
            "  test('same', () => { expect(critical()).toBe(true); });\n"
            "});"
        )
        self._commit_patch("web/src/auth.test.ts", strong)
        self._write_patch(
            "web/src/auth.test.ts",
            "describe('A', () => {\n"
            "  test.skip('same', () => { expect(critical()).toBe(true); });\n"
            "  test('same', () => { expect(flaky()).toBe(true); });\n"
            "});",
        )
        result = diffcheck.run(self.repo)
        self.assertEqual(result["diff-ratchet"]["state"], "PASS")
        self.assertEqual(result["diff-weakening"]["state"], "FAIL")
        self.assertIn("skip test added", result["diff-weakening"]["detail"])

    def test_editing_one_skipped_duplicate_is_not_a_new_skip_or_removal(self) -> None:
        first = (
            "test.skip('same', () => { const value = first(); "
            "expect(value).toBe(true); });\n"
            "test('same', () => { const value = second(); expect(value).toBe(true); });"
        )
        self._commit_patch("web/src/auth.test.ts", first)
        self._write_patch(
            "web/src/auth.test.ts",
            "test.skip('same', () => { const actual = first(); "
            "expect(actual).toBe(true); });\n"
            "test('same', () => { const value = second(); expect(value).toBe(true); });",
        )
        result = diffcheck.run(self.repo)
        self.assertEqual(result["diff-ratchet"]["state"], "PASS")
        self.assertEqual(result["diff-weakening"]["state"], "PASS")

    def test_option_based_test_disabling_is_detected(self) -> None:
        cases = (
            (
                "test('critical', () => { expect(critical()).toBe(true); });",
                "test('critical', {skip: true}, () => { expect(critical()).toBe(true); });",
                "skip option added",
            ),
            (
                "test('critical', () => { expect(critical()).toBe(true); });",
                "test('critical', {skip: process.env.CI}, () => { "
                "expect(critical()).toBe(true); });",
                "skip option added",
            ),
            (
                "Deno.test({name: 'critical', fn() { assert(critical()); }});",
                "Deno.test({name: 'critical', ignore: 'CI only', "
                "fn() { assert(critical()); }});",
                "ignore option added",
            ),
            (
                "test('critical', () => { expect(critical()).toBe(true); });",
                "const skip = process.env.CI; "
                "test('critical', {skip}, () => { expect(critical()).toBe(true); });",
                "skip option added",
            ),
        )
        for strong, weakened, finding in cases:
            with self.subTest(finding=finding):
                self._commit_patch("web/src/auth.test.ts", strong)
                self._write_patch("web/src/auth.test.ts", weakened)
                result = diffcheck.run(self.repo)["diff-weakening"]
                self.assertEqual(result["state"], "FAIL")
                self.assertIn(finding, result["detail"])
                self._git("reset", "-q", "--hard", "HEAD~1")
                self._git("clean", "-qfd")

    def test_explicit_false_skip_option_is_not_a_weakening(self) -> None:
        strong = "test('critical', () => { expect(critical()).toBe(true); });"
        self._commit_patch("web/src/auth.test.ts", strong)
        self._write_patch(
            "web/src/auth.test.ts",
            "test('critical', {skip: false}, () => { expect(critical()).toBe(true); });",
        )
        self.assertEqual(
            diffcheck.run(self.repo)["diff-weakening"]["state"],
            "PASS",
        )

    def test_script_strings_and_comments_do_not_create_assertions_or_weakenings(
        self,
    ) -> None:
        strong = "test('auth', () => { expect(context().user).toBe(user); });"
        self._commit_patch("web/src/auth.test.ts", strong)
        self._write_patch(
            "web/src/auth.test.ts",
            "test('auth', () => {\n"
            "  const example = 'expect(value || true).toBe(true)';\n"
            "  // test.skip('not a test', () => expect(false));\n"
            "  expect(context().user).toBe(user);\n"
            "});",
        )
        result = diffcheck.run(self.repo)
        self.assertEqual(result["diff-ratchet"]["state"], "PASS")
        self.assertEqual(result["diff-weakening"]["state"], "PASS")

    def test_patch_data_fixture_does_not_become_unsupported_source(self) -> None:
        patch_file = self.repo / "task" / "tests" / "test.patch"
        patch_file.write_text(
            self._patch(
                "web/tests/auth.test.ts",
                "test('auth', () => { expect(auth()).toBe(true); });",
            )
            + self._patch("web/tests/fixtures/test-data.json", '{"id": "1"}')
            + self._patch("web/fixtures/test-user.json", '{"id": "2"}')
        )
        self._git("add", "-A")
        result = diffcheck.run(self.repo)
        self.assertEqual(result["diff-ratchet"]["state"], "PASS")
        self.assertEqual(result["diff-weakening"]["state"], "PASS")
        self.assertEqual(result["diff-ratchet"]["examined"], 1)

    def test_invalid_or_unsupported_patch_source_fails_closed(self) -> None:
        cases = {
            "web/src/auth.test.ts": "test('auth', (): void => { expect(true);",
            "pkg/auth_test.rs": "fn test_auth() { assert!(true); }",
        }
        for path, source in cases.items():
            with self.subTest(path=path):
                self._write_patch(path, source)
                result = diffcheck.run(self.repo)
                self.assertEqual(result["diff-ratchet"]["state"], "FAIL")
                self.assertEqual(result["diff-weakening"]["state"], "FAIL")
                self.assertTrue(result["diff-ratchet"]["applicable"])
                self._git("reset", "-q", "--hard")
                self._git("clean", "-qfd")

    def test_direct_verifier_environment_assets_are_not_graded_sources(self) -> None:
        cases = {
            "task/tests/Dockerfile": "FROM python:3.12-slim\n",
            "task/tests/ollo-repo.tar.gz.b64": "Y2xlYW4tc25hcHNob3Q=\n",
        }
        for relative, content in cases.items():
            with self.subTest(path=relative):
                source = self.repo / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(content)
                self._git("add", "-A")
                result = diffcheck.run(self.repo)
                self.assertEqual(result["diff-ratchet"]["state"], "NOT_RUN")
                self.assertEqual(result["diff-weakening"]["state"], "NOT_RUN")
                self.assertFalse(result["diff-ratchet"]["applicable"])
                self._git("reset", "-q", "--hard")
                self._git("clean", "-qfd")

    def test_task_local_benchsmith_control_is_not_a_graded_test_source(self) -> None:
        source = self.repo / "task" / ".benchsmith" / "test_control_runner.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("raise SystemExit(run_control())\n")
        self._git("add", "-A")

        result = diffcheck.run(self.repo)

        self.assertEqual(result["diff-ratchet"]["state"], "NOT_RUN")
        self.assertEqual(result["diff-weakening"]["state"], "NOT_RUN")
        self.assertFalse(result["diff-ratchet"]["applicable"])

    def test_task_local_qa_fixture_is_not_a_graded_test_source(self) -> None:
        for relative in (
            "task/qa/negative/n23-replace-graded-binary-between-tests.sh",
            "task/qa/positive/compatible-tests.py",
            "task/qa/variants/alternate_test.go",
        ):
            with self.subTest(relative=relative):
                source = self.repo / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("assert true\n")
                self._git("add", "-A")
                result = diffcheck.run(self.repo)
                self.assertEqual(result["diff-ratchet"]["state"], "NOT_RUN")
                self.assertEqual(result["diff-weakening"]["state"], "NOT_RUN")
                self.assertFalse(result["diff-ratchet"]["applicable"])
                self._git("reset", "-q", "--hard")
                self._git("clean", "-qfd")

    def test_direct_unsupported_test_source_is_not_misclassified_as_docs(self) -> None:
        for relative in (
            "task/tests/AuthSpec.scala",
            "task/tests/helper.scala",
            "task/tests/run_checks",
        ):
            with self.subTest(path=relative):
                source = self.repo / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('test("auth") { assert(true) }\n')
                self._git("add", "-A")
                result = diffcheck.run(self.repo)
                self.assertTrue(result["diff-ratchet"]["applicable"])
                self.assertEqual(result["diff-ratchet"]["state"], "FAIL")
                self.assertEqual(result["diff-weakening"]["state"], "FAIL")
                self._git("reset", "-q", "--hard")

    def test_changed_test_patch_with_zero_examined_sources_fails(self) -> None:
        self._write_patch("web/src/auth.ts", "export const auth = true;")
        result = diffcheck.run(self.repo)
        self.assertEqual(result["diff-ratchet"]["state"], "FAIL")
        self.assertEqual(result["diff-weakening"]["state"], "FAIL")
        self.assertEqual(result["diff-ratchet"]["examined"], 0)

    def test_docs_only_commit_makes_diff_checks_inapplicable_to_receipt(self) -> None:
        (self.repo / "task" / "instruction.md").write_text("clarified instruction\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "docs only")

        report = gate.Report()
        for name in gate.PUSH_REQUIRED:
            if name not in {"diff-ratchet", "diff-weakening"}:
                report.add(name, gate.PASS, "fixture")
        gate.check_diff(self.repo, report)
        report.require(gate.PUSH_REQUIRED)

        diff_checks = {
            check.name: check
            for check in report.checks
            if check.name in {"diff-ratchet", "diff-weakening"}
        }
        self.assertTrue(report.ok)
        self.assertTrue(
            all(check.state == gate.NOT_RUN for check in diff_checks.values())
        )
        self.assertTrue(all(not check.blocking for check in diff_checks.values()))
        self.assertTrue(all(not check.required for check in diff_checks.values()))

        receipt_dir = Path(self.tempdir.name) / "receipts"
        hook_receipt = Path(self.tempdir.name) / "hook-receipt.json"
        with mock.patch.dict(
            os.environ,
            {
                "BENCHSMITH_RECEIPT_DIR": str(receipt_dir),
                "GATE_RECEIPT": str(hook_receipt),
            },
        ):
            receipt = gate.write_receipt(self.repo, "task", report)
            self.assertTrue(receipt["ok"])
            self.assertEqual(
                set(receipt["inapplicableChecks"]),
                {"diff-ratchet", "diff-weakening"},
            )
            self.assertTrue(gate.verify_receipt(self.repo, "task")[0])
            emitted = json.loads(hook_receipt.read_text())
            self.assertNotIn("diff-ratchet", emitted["gates"])
            self.assertNotIn("diff-weakening", emitted["gates"])

    def test_fingerprint_failure_removes_hook_receipt(self) -> None:
        report = gate.Report()
        for name in gate.PUSH_REQUIRED:
            report.add(name, gate.PASS, "fixture")
        hook_receipt = Path(self.tempdir.name) / "hook-receipt.json"
        hook_receipt.write_text('{"stale":true}\n')
        gate.safety.snapshot.side_effect = gate.safety.SafetyRefused("dirty install")
        with mock.patch.dict(os.environ, {"GATE_RECEIPT": str(hook_receipt)}):
            receipt = gate.write_receipt(self.repo, "task", report)

        self.assertEqual(receipt["state"], "not_written")
        self.assertFalse(hook_receipt.exists())

    def test_legacy_receipt_without_fingerprints_requires_regate(self) -> None:
        report = gate.Report()
        for name in gate.PUSH_REQUIRED:
            report.add(name, gate.PASS, "fixture")
        receipt_dir = Path(self.tempdir.name) / "receipts"
        with mock.patch.dict(os.environ, {"BENCHSMITH_RECEIPT_DIR": str(receipt_dir)}):
            receipt = gate.write_receipt(self.repo, "task", report)
            receipt.pop("gateFingerprint")
            receipt.pop("publishPolicyFingerprint")
            receipt["digest"] = gate._receipt_digest(receipt)
            gate.receipt_path(self.repo, "task").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )

            ok, reason = gate.verify_receipt(self.repo, "task")

        self.assertFalse(ok)
        self.assertIn("predates safety fingerprints", reason)

    def test_changed_unsupported_patch_remains_required(self) -> None:
        self._write_patch("pkg/auth_test.rs", "fn test_auth() { assert!(true); }")
        report = gate.Report()
        for name in gate.PUSH_REQUIRED:
            if name not in {"diff-ratchet", "diff-weakening"}:
                report.add(name, gate.PASS, "fixture")
        gate.check_diff(self.repo, report)
        report.require(gate.PUSH_REQUIRED)
        self.assertFalse(report.ok)
        for check in report.checks:
            if check.name in {"diff-ratchet", "diff-weakening"}:
                self.assertTrue(check.blocking)
                self.assertTrue(check.required)
                self.assertEqual(check.state, gate.FAIL)


if __name__ == "__main__":
    unittest.main()
