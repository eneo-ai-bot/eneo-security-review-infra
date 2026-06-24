from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from eneo_review_replay import validate_replay_path


VALID_FIXTURE = """{
  "schema_version": 1,
  "id": "unit-fixture",
  "source": {
    "repository": "eneo-ai/eneo",
    "pull_request": 240,
    "base_sha": "43edef11a5959162e98ba2fc9d06b2ccf940cf65",
    "head_sha": "518cc8a235777527177a9097e5a6be191771cf83",
    "trust": "human_confirmed"
  },
  "model_expectations": {
    "mode": "advisory",
    "required_root_causes": [
      {
        "rule_id": "migration.ambiguous-model-resolution",
        "severity": {"highest_allowed": "High", "lowest_allowed": "High"},
        "semantic_claim": "tenant mapping ignores provider identity"
      }
    ],
    "forbidden_root_causes": []
  },
  "deterministic_invariants": [
    {
      "id": "stable-fingerprint",
      "covered_by": [
        "tests.test_memory_db.ReviewMemoryTests.test_fingerprint_is_stable_across_line_moves"
      ]
    }
  ],
  "advisory_notes": ["wording may vary"]
}"""


class ReplayValidationTests(unittest.TestCase):
    def test_repository_fixture_is_valid(self) -> None:
        results = validate_replay_path(ROOT / "review-learning" / "replay")

        self.assertEqual(
            {result.fixture_id for result in results},
            {"pr-240-ambiguous-model-resolution"},
        )

    def test_unknown_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.json"
            path.write_text(
                VALID_FIXTURE.replace('"advisory_notes"', '"surprise": true, "advisory_notes"'),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unknown keys"):
                validate_replay_path(path)

    def test_invalid_test_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.json"
            path.write_text(
                VALID_FIXTURE.replace(
                    "test_fingerprint_is_stable_across_line_moves",
                    "test_does_not_exist",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "does not exist"):
                validate_replay_path(path)

    def test_missing_exact_sha_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.json"
            path.write_text(
                VALID_FIXTURE.replace(
                    '"base_sha": "43edef11a5959162e98ba2fc9d06b2ccf940cf65",',
                    "",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "missing keys: base_sha"):
                validate_replay_path(path)

    def test_non_json_fixture_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.json"
            path.write_text("id: yaml-only\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be JSON"):
                validate_replay_path(path)

    def test_empty_replay_directory_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "contains no replay fixtures"):
                validate_replay_path(Path(temp))

    def test_yaml_extension_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.yaml"
            path.write_text(VALID_FIXTURE, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, ".json replay fixture"):
                validate_replay_path(path)


if __name__ == "__main__":
    unittest.main()
