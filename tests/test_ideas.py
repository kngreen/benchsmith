import json
import tempfile
import unittest
from pathlib import Path

from benchsmith import ideas


def response(document):
    return 0, json.dumps(document), ""


class FakeRun:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if not self.replies:
            raise AssertionError(f"unexpected command: {argv}")
        return self.replies.pop(0)


class IdeasTest(unittest.TestCase):
    def test_parse_idea_form(self):
        fields = ideas.parse_idea_description("""<!-- aai-ideation-form v1 -->
Build a deterministic engine.

## Domain
Games

## Capability under test
Global reasoning

## Why SOTA should fail this
Local choices conflict later.

## Difficulty levers (conceptual)
Several coupled modes.

## Verification intent
Replay the final state.
<!-- /aai-ideation-form -->""")
        self.assertEqual(fields["idea"], "Build a deterministic engine.")
        self.assertEqual(fields["domain"], "Games")
        self.assertEqual(fields["why_fail"], "Local choices conflict later.")
        self.assertEqual(fields["verification"], "Replay the final state.")

    def test_init_is_plan_only_without_apply(self):
        run = FakeRun([response([])])
        with tempfile.TemporaryDirectory() as tmp:
            result = ideas.init_board(Path(tmp), run=run)
            self.assertFalse(result["applied"])
            self.assertEqual(result["actions"][0]["action"], "create_project")
            self.assertFalse((Path(tmp) / ideas.BOARD_FILE).exists())
        self.assertEqual(len(run.calls), 1)

    def test_init_creates_board_sections_and_repo_config(self):
        replies = [response([]), response({"project": {"id": "123", "name": "Mine",
                                                          "url": "https://example/123"}}),
                   response([])]
        replies.extend(response({"section": {"id": str(i)}}) for i in range(len(ideas.STAGES)))
        run = FakeRun(replies)
        with tempfile.TemporaryDirectory() as tmp:
            result = ideas.init_board(Path(tmp), name="Mine", owner="me", apply=True, run=run)
            config = json.loads((Path(tmp) / ideas.BOARD_FILE).read_text())
        self.assertTrue(result["applied"])
        self.assertEqual(config["gsd"]["projectId"], "123")
        self.assertEqual(config["gsd"]["sections"]["Hard calibrated"], "done")
        section_calls = [call for call in run.calls if call[1:3] == ["tasks.gsd.section", "create"]]
        self.assertEqual(len(section_calls), len(ideas.STAGES))

    def test_harvest_preserves_human_seed_and_does_not_mutate_by_default(self):
        row = {
            "id": "252", "title": "FoldForm", "track": "tbench", "status": "up_for_grabs",
            "creator": {"username": "human"}, "noveltyLevel": "MEDIUM",
            "description": """A coupled layout problem.
## Domain
Printing
## Capability under test
Spatial composition
## Why SOTA should fail this
Later folds invalidate local choices.
## Difficulty levers (conceptual)
Mixed folds and several signatures.
## Verification intent
Replay every physical operation.""",
        }
        run = FakeRun([response({"ideas": [row]}), response([]), response([])])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ideas.BOARD_FILE
            path.parent.mkdir()
            path.write_text(json.dumps({"gsd": {"projectId": "123"}}))
            result = ideas.harvest(Path(tmp), run=run)
        self.assertFalse(result["applied"])
        self.assertEqual(result["planned"][0]["ideaId"], "252")
        self.assertTrue(result["planned"][0]["screenReady"])
        self.assertFalse(any(call[1:3] == ["tasks.task", "create"] for call in run.calls))

    def test_harvest_skips_incomplete_seed(self):
        row = {"id": "1", "title": "Thin", "track": "tbench", "status": "up_for_grabs",
               "creator": {"username": "human"}, "noveltyLevel": "HIGH",
               "description": "Just do a thing."}
        run = FakeRun([response({"ideas": [row]}), response([])])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ideas.BOARD_FILE
            path.parent.mkdir()
            path.write_text(json.dumps({"gsd": {"projectId": "123"}}))
            result = ideas.harvest(Path(tmp), run=run)
        self.assertEqual(result["planned"], [])
        self.assertIn("missing human intake fields", result["skipped"][0]["reason"])

    def test_harvest_skips_unassessed_novelty_by_default(self):
        row = {
            "id": "2", "title": "Unassessed", "track": "tbench",
            "status": "up_for_grabs", "creator": {"username": "human"},
            "description": """A complete seed.
## Domain
Systems
## Capability under test
Global reasoning
## Why SOTA should fail this
Several distant constraints interact.
## Difficulty levers (conceptual)
Multiple coupled states and adversarial cases.
## Verification intent
Replay and compare all final states.""",
        }
        run = FakeRun([response({"ideas": [row]}), response([])])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ideas.BOARD_FILE
            path.parent.mkdir()
            path.write_text(json.dumps({"gsd": {"projectId": "123"}}))
            result = ideas.harvest(Path(tmp), run=run)
        self.assertEqual(result["planned"], [])
        self.assertIn("not MEDIUM/HIGH", result["skipped"][0]["reason"])

    def test_board_title_deduplicates_before_eventual_external_index(self):
        row = {
            "id": "252", "title": "FoldForm", "track": "tbench", "status": "up_for_grabs",
            "creator": {"username": "human"}, "noveltyLevel": "MEDIUM",
            "description": """A coupled layout problem.
## Domain
Printing
## Capability under test
Spatial composition
## Why SOTA should fail this
Later folds invalidate local choices.
## Difficulty levers (conceptual)
Mixed folds and several signatures.
## Verification intent
Replay every physical operation.""",
        }
        card = {"number": "T123", "title": "[T-Bench seed #252] FoldForm"}
        run = FakeRun([response({"ideas": [row]}), response([card])])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ideas.BOARD_FILE
            path.parent.mkdir()
            path.write_text(json.dumps({"gsd": {"projectId": "123"}}))
            result = ideas.harvest(Path(tmp), run=run)
        self.assertEqual(result["planned"], [])
        self.assertEqual(result["existing"][0]["task"], "T123")
        self.assertEqual(len(run.calls), 2)

    def test_harvest_uses_configured_assignee_for_created_card(self):
        row = {
            "id": "252", "title": "FoldForm", "track": "tbench", "status": "up_for_grabs",
            "creator": {"username": "human"}, "noveltyLevel": "MEDIUM",
            "description": """A coupled layout problem.
## Domain
Printing
## Capability under test
Spatial composition
## Why SOTA should fail this
Later folds invalidate local choices.
## Difficulty levers (conceptual)
Mixed folds and several signatures.
## Verification intent
Replay every physical operation.""",
        }
        run = FakeRun([
            response({"ideas": [row]}), response([]), response([]),
            response({"task": {"number": "T123"}}),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ideas.BOARD_FILE
            path.parent.mkdir()
            path.write_text(json.dumps({"gsd": {"projectId": "123", "assignee": "owner"}}))
            result = ideas.harvest(Path(tmp), apply=True, run=run)
        self.assertEqual(result["created"][0]["task"], "T123")
        create = next(call for call in run.calls if call[1:3] == ["tasks.task", "create"])
        self.assertIn("--owner=owner", create)

    def test_go_requires_two_independent_cores(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ideas.BOARD_FILE
            path.parent.mkdir()
            path.write_text(json.dumps({"gsd": {"projectId": "123"}}))
            with self.assertRaises(ideas.IdeasError):
                ideas.mark(Path(tmp), "T1", "GO", evidence="screen complete")

    def test_reference_summary_never_claims_hard_calibration(self):
        task = {
            "id": "9", "name": "candidate", "status": "accepted", "difficulty": "hard",
            "headCommitSha": "a", "validationCommitSha": "a", "validationStatus": "passing",
            "oracleStatus": "validated", "tbdReviewStatus": "pass",
            "qualitativeResult": {
                "difficulty": {"classification": "GOOD", "passRate": 0.4},
                "contaminationV2": {"level": "LOW"},
                "provenanceCheck": {"verdict": "CLEAN"},
            },
        }
        result = ideas.inspect_reference(["9"], run=FakeRun([response(task)]))
        reference = result["references"][0]
        self.assertTrue(reference["eligibleForDeepAudit"])
        self.assertFalse(reference["hardCalibrated"])


if __name__ == "__main__":
    unittest.main()
