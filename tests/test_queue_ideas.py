import unittest

from benchsmith import resolve
from benchsmith.queue import build_queue


class QueueIdeasTest(unittest.TestCase):
    def test_work_and_publication_eligibility_are_independent(self):
        self.assertTrue(resolve.work_eligibility("draft").eligible)
        self.assertTrue(resolve.publication_eligibility("draft").eligible)
        self.assertFalse(resolve.work_eligibility("being_reviewed").eligible)
        self.assertFalse(resolve.publication_eligibility("being_reviewed").eligible)
        self.assertTrue(
            resolve.publication_eligibility(
                "being_reviewed", allow_review_status="being_reviewed"
            ).eligible
        )
        self.assertFalse(resolve.work_eligibility("accepted").eligible)
        self.assertFalse(resolve.publication_eligibility("accepted").eligible)
        self.assertFalse(resolve.work_eligibility("new-status").eligible)
        self.assertFalse(resolve.publication_eligibility("new-status").eligible)

    def test_queue_exposes_publication_hold_without_blocking_work(self):
        item = build_queue(
            [
                {
                    "name": "generation",
                    "status": "draft",
                    "validationStatus": "passed",
                    "publicationEligibility": {
                        "eligible": False,
                        "reason": "missing-replay-evidence",
                    },
                }
            ]
        )[0]

        self.assertTrue(item.dispatchable)
        self.assertFalse(item.publication_eligible)
        self.assertEqual(item.publication_reason, "missing-replay-evidence")

    def test_all_pending_validation_states_share_pending_tier(self):
        queue = build_queue(
            [
                {"name": state, "status": "draft", "validationStatus": state}
                for state in ("pending", "running", "queued", "in_progress", "validating")
            ]
        )
        self.assertEqual(len({item.tier for item in queue}), 1)

    def test_error_and_unknown_validation_are_not_labelled_green(self):
        errored = build_queue(
            [{"name": "errored", "status": "draft", "validationStatus": "error"}]
        )[0]
        unknown = build_queue(
            [{"name": "unknown", "status": "draft", "validationStatus": "mystery"}]
        )[0]

        self.assertIn("validation is error", errored.reason)
        self.assertNotIn("green", errored.reason)
        self.assertFalse(unknown.dispatchable)
        self.assertIn("unrecognised validation status", unknown.skip)

    def test_terminal_board_sections_do_not_reenter_queue(self):
        queue = build_queue([], ideas=[
            {"name": "T1", "title": "done", "kind": "done"},
            {"name": "T2", "title": "ready", "kind": "gsd_scaffold"},
        ])
        self.assertEqual([item.task for item in queue], ["T2"])


if __name__ == "__main__":
    unittest.main()
