import unittest

from benchsmith.queue import build_queue


class QueueIdeasTest(unittest.TestCase):
    def test_terminal_board_sections_do_not_reenter_queue(self):
        queue = build_queue([], ideas=[
            {"name": "T1", "title": "done", "kind": "done"},
            {"name": "T2", "title": "ready", "kind": "gsd_scaffold"},
        ])
        self.assertEqual([item.task for item in queue], ["T2"])


if __name__ == "__main__":
    unittest.main()
