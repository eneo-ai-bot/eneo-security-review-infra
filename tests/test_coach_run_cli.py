from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


def memory_export() -> dict[str, object]:
    return {
        "schema_version": 5,
        "exported_at": "2026-06-24T00:00:00Z",
        "findings": [],
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


class CoachRunCliTests(unittest.TestCase):
    def test_coach_run_writes_private_artifacts_and_records_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export_path = root / "memory-export.json"
            output_dir = root / "coach-run"
            db_path = root / "memory.sqlite3"
            export_path.write_text(json.dumps(memory_export()), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "eneo_review_memory.py"),
                    "--db",
                    str(db_path),
                    "coach-run",
                    "--export",
                    str(export_path),
                    "--output-dir",
                    str(output_dir),
                    "--repo",
                    "eneo-ai/eneo",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            receipt = json.loads(completed.stdout)
            self.assertEqual(receipt["run"]["decision"], "propose")
            self.assertEqual(receipt["run"]["candidates_count"], 1)
            self.assertEqual(receipt["run"]["events_considered"], 2)

            for name in ["coach-export.json", "proposal.json", "SUMMARY.md"]:
                path = output_dir / name
                self.assertTrue(path.exists())
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

            with closing(memory_db.connect(str(db_path))) as connection:
                runs = memory_db.list_coach_runs(connection, repository="eneo-ai/eneo")
                candidates = memory_db.list_coach_candidates(
                    connection, repository="eneo-ai/eneo"
                )

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].proposal_set_id, receipt["run"]["proposal_set_id"])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].seen_count, 1)
            self.assertEqual(candidates[0].evidence_event_ids, ("decision:1", "decision:2"))


if __name__ == "__main__":
    unittest.main()
