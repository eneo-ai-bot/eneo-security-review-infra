from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGINS))

from eneo_review_tools import failure_codes  # noqa: E402


class FailureCodesTests(unittest.TestCase):
    def test_canonical_values_are_stable(self):
        self.assertEqual(failure_codes.REVIEW_FAILED, "review_failed")
        self.assertEqual(failure_codes.STALE_TIMEOUT, "stale_timeout")
        self.assertEqual(failure_codes.REVIEW_DELIVER_ERROR, "review_deliver_error")
        self.assertEqual(
            failure_codes.UNEXPECTED_REVIEW_DELIVER_FAILURE,
            "unexpected_review_deliver_failure",
        )

    def test_all_enumerates_every_code_without_duplicates(self):
        codes = [
            failure_codes.REVIEW_FAILED,
            failure_codes.STALE_TIMEOUT,
            failure_codes.SUPERSEDED_BY_FORCE,
            failure_codes.SUPERSEDED_DUPLICATE_MIGRATION,
            failure_codes.REVIEW_DELIVER_ERROR,
            failure_codes.UNEXPECTED_REVIEW_DELIVER_FAILURE,
        ]
        self.assertEqual(failure_codes.ALL, frozenset(codes))
        self.assertEqual(len(failure_codes.ALL), len(codes))


if __name__ == "__main__":
    unittest.main()
