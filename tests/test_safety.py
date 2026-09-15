from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from benchsmith import safety


class SafetyFingerprintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "benchsmith"
        source_root = safety.installation_root()
        copied: set[str] = set()
        for manifest in safety._MANIFESTS.values():
            for relative, _symbols in manifest:
                if relative in copied:
                    continue
                copied.add(relative)
                destination = self.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_root / relative, destination)
        (self.root / "bin").mkdir(exist_ok=True)
        (self.root / "bin" / "benchsmith").write_text("#!/bin/sh\n")
        (self.root / "SKILL.md").write_text("# test\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unrelated_projection_and_docs_do_not_change_fingerprints(self) -> None:
        before = {
            name: safety.component_digest(name, root=self.root)
            for name in safety._MANIFESTS
        }
        projection = self.root / "lib" / "benchsmith" / "task_status.py"
        projection.write_text(projection.read_text() + "\n# unrelated projection change\n")
        (self.root / "README.md").write_text("unrelated documentation\n")
        after = {
            name: safety.component_digest(name, root=self.root)
            for name in safety._MANIFESTS
        }
        self.assertEqual(after, before)

    def test_component_drift_is_scoped(self) -> None:
        before = {
            name: safety.component_digest(name, root=self.root)
            for name in safety._MANIFESTS
        }
        controller = self.root / "lib" / "benchsmith" / "controller.py"
        controller.write_text(controller.read_text() + "\n# controller drift\n")
        after_controller = {
            name: safety.component_digest(name, root=self.root)
            for name in safety._MANIFESTS
        }
        self.assertNotEqual(
            after_controller["controller_dispatch"], before["controller_dispatch"]
        )
        self.assertEqual(after_controller["gate"], before["gate"])
        self.assertEqual(after_controller["publish_policy"], before["publish_policy"])

        shutil.copy2(
            safety.installation_root() / "lib" / "benchsmith" / "controller.py",
            controller,
        )
        gate = self.root / "lib" / "benchsmith" / "gate.py"
        gate.write_text(gate.read_text() + "\n# gate drift\n")
        after_gate = {
            name: safety.component_digest(name, root=self.root)
            for name in safety._MANIFESTS
        }
        self.assertNotEqual(after_gate["gate"], before["gate"])
        self.assertEqual(
            after_gate["controller_dispatch"], before["controller_dispatch"]
        )
        self.assertEqual(after_gate["publish_policy"], before["publish_policy"])

    def test_missing_declared_component_fails_closed(self) -> None:
        (self.root / "lib" / "benchsmith" / "publish.py").unlink()
        with self.assertRaisesRegex(safety.SafetyRefused, "manifest path is missing"):
            safety.component_digest("publish_policy", root=self.root)

    def test_dirty_checkout_fails_closed(self) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Benchsmith Test"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "baseline"], check=True
        )
        clean = safety.snapshot("controller_dispatch", root=self.root)
        self.assertTrue(clean["clean"])

        controller = self.root / "lib" / "benchsmith" / "controller.py"
        controller.write_text(controller.read_text() + "\n# dirty\n")
        with self.assertRaisesRegex(safety.SafetyRefused, "checkout is dirty"):
            safety.snapshot("controller_dispatch", root=self.root)


if __name__ == "__main__":
    unittest.main()
