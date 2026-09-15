from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from benchsmith import hold
from benchsmith import remote_ref


class RepositoryHoldTest(unittest.TestCase):
    def test_current_preserves_expired_token_sha_for_cas_replacement(self):
        token = "a" * 40
        message = (
            "benchsmith repo hold holder=alice host=devvm until=100 "
            "why=expired_hold"
        )
        with (
            patch.object(
                hold.remote_ref,
                "read",
                return_value={"readable": True, "sha": token, "detail": ""},
            ),
            patch.object(
                hold,
                "_git",
                return_value=subprocess.CompletedProcess([], 0, message, ""),
            ),
            patch.object(hold.time, "time", return_value=101),
        ):
            result = hold.current("/repo")

        self.assertTrue(result["readable"])
        self.assertFalse(result["held"])
        self.assertEqual(result["sha"], token)
        self.assertEqual(result["expired"]["holder"], "alice")

    def test_take_replaces_expired_token_with_compare_and_swap(self):
        expired = "a" * 40
        replacement = "b" * 40
        update = {
            "state": remote_ref.CONFIRMED,
            "attempts": 0,
            "sha": replacement,
            "detail": "push returned zero",
        }
        with (
            patch.object(
                hold,
                "current",
                return_value={
                    "readable": True,
                    "held": False,
                    "sha": expired,
                    "expired": {"holder": "alice"},
                },
            ),
            patch.object(
                hold,
                "_git",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "tree\n", ""),
                    subprocess.CompletedProcess([], 0, replacement + "\n", ""),
                ],
            ),
            patch.object(hold.remote_ref, "update", return_value=update) as replace,
        ):
            result = hold.take("/repo", why="renew", minutes=30)

        self.assertTrue(result["taken"])
        replace.assert_called_once_with(
            "/repo", "origin", hold.REF, replacement, expected=expired
        )

    def test_take_creates_when_no_remote_token_exists(self):
        replacement = "b" * 40
        update = {
            "state": remote_ref.CONFIRMED,
            "attempts": 0,
            "sha": replacement,
            "detail": "push returned zero",
        }
        with (
            patch.object(
                hold,
                "current",
                return_value={"readable": True, "held": False},
            ),
            patch.object(
                hold,
                "_git",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "tree\n", ""),
                    subprocess.CompletedProcess([], 0, replacement + "\n", ""),
                ],
            ),
            patch.object(hold.remote_ref, "update", return_value=update) as create,
        ):
            result = hold.take("/repo", why="new", minutes=30)

        self.assertTrue(result["taken"])
        create.assert_called_once_with(
            "/repo", "origin", hold.REF, replacement, expected=None
        )


if __name__ == "__main__":
    unittest.main()
