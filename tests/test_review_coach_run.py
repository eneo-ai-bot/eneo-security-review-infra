from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from eneo_review_coach_run import build_coach_run_artifacts


def memory_export() -> dict[str, object]:
    return {
        "schema_version": 5,
        "finding_observations": [
            {
                "id": 11,
                "repository": "eneo-ai/eneo",
                "pr_number": 240,
                "head_sha": "a" * 40,
                "fingerprint": "abcdef1234567890",
                "title": "Tenant scope claim was wrong",
                "path": "backend/src/intric/sysadmin/sysadmin_router.py",
            },
            {
                "id": 22,
                "repository": "eneo-ai/eneo",
                "pr_number": 241,
                "head_sha": "b" * 40,
                "fingerprint": "abcdef1234567890",
                "title": "Tenant scope claim was wrong",
                "path": "backend/src/intric/sysadmin/sysadmin_router.py",
            },
        ],
        "pr_finding_references": [],
        "decisions": [
            {
                "id": 1,
                "fingerprint": "abcdef1234567890",
                "observation_id": 11,
                "decision": "false_positive",
                "reason": "Existing guard disproves this in PR 240.",
            },
            {
                "id": 2,
                "fingerprint": "abcdef1234567890",
                "observation_id": 22,
                "decision": "false_positive",
                "reason": "Existing guard disproves this in PR 241.",
            },
        ],
        "review_quality_feedback": [],
    }


class CoachRunArtifactTests(unittest.TestCase):
    def test_builds_private_artifacts_and_typed_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export_path = root / "memory-export.json"
            output_dir = root / "coach-run"
            export_path.write_text(json.dumps(memory_export()), encoding="utf-8")

            artifacts = build_coach_run_artifacts(
                export_path=export_path,
                output_dir=output_dir,
                repository="eneo-ai/eneo",
            )

            self.assertEqual(artifacts.bundle.decision, "propose")
            self.assertEqual(artifacts.bundle.events_considered, 2)
            self.assertEqual(len(artifacts.bundle.candidates), 1)
            self.assertEqual(
                artifacts.paths.to_json_obj()["proposal"],
                str(output_dir / "proposal.json"),
            )
            for path in [
                artifacts.paths.coach_export,
                artifacts.paths.proposal,
                artifacts.paths.summary,
            ]:
                self.assertTrue(path.exists())
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
