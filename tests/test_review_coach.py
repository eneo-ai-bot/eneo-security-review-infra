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

from eneo_review_coach import build_coach_export, dumps_coach_export
from eneo_review_private_io import write_private_file


def state_with_signals(reason: str = "Human confirmed this was wrong.") -> dict[str, object]:
    return {
        "schema_version": 5,
        "exported_at": "2026-06-24T00:00:00Z",
        "finding_observations": [
            {
                "id": 11,
                "repository": "eneo-ai/eneo",
                "pr_number": 240,
                "head_sha": "a" * 40,
                "fingerprint": "abcdef1234567890",
                "title": "All-tenant migration can choose the wrong model row",
                "path": "backend/src/intric/sysadmin/sysadmin_router.py",
            }
        ],
        "pr_finding_references": [
            {
                "repository": "eneo-ai/eneo",
                "pr_number": 240,
                "fingerprint": "abcdef1234567890",
                "local_reference": "F1",
            }
        ],
        "decisions": [
            {
                "id": 1,
                "fingerprint": "abcdef1234567890",
                "observation_id": 11,
                "decision": "reopen",
                "reason": "Old suppression was stale.",
                "actor": "github:alice",
            },
            {
                "id": 2,
                "fingerprint": "abcdef1234567890",
                "observation_id": 11,
                "decision": "false_positive",
                "reason": reason,
                "actor": "github:bob",
            },
        ],
        "review_quality_feedback": [
            {
                "id": 7,
                "repository": "eneo-ai/eneo",
                "pr_number": 240,
                "local_reference": "F2",
                "category": "missed_issue",
                "reason": "Reviewer missed a tenant-boundary issue.",
                "actor_login": "carol",
                "source_comment_url": "https://github.test/comment/7",
            }
        ],
    }


class CoachExportTests(unittest.TestCase):
    def test_coach_export_is_incremental_and_allowlisted(self) -> None:
        payload = build_coach_export(
            state_with_signals(),
            repository="eneo-ai/eneo",
            after_decision_id=1,
            after_feedback_id=0,
        )
        rendered = dumps_coach_export(payload)
        decoded = json.loads(rendered)

        self.assertEqual(decoded["schema_version"], 1)
        self.assertEqual(decoded["cursor"]["max_decision_id"], 2)
        self.assertEqual(decoded["cursor"]["max_feedback_id"], 7)
        self.assertEqual(
            {event["event_id"] for event in decoded["events"]},
            {"decision:2", "feedback:7"},
        )
        decision_event = next(
            event for event in decoded["events"] if event["event_id"] == "decision:2"
        )
        self.assertEqual(
            decision_event["decision_chain"],
            ["reopen", "false_positive"],
        )
        self.assertEqual(decision_event["source"]["observation_id"], 11)
        self.assertIn("human_reason_untrusted", decision_event)
        self.assertNotIn("github:bob", rendered)
        self.assertNotIn("carol", rendered)
        self.assertNotIn("github.test", rendered)

    def test_coach_export_bounds_untrusted_reason_and_hash_is_stable(self) -> None:
        payload = build_coach_export(state_with_signals("x" * 1400))
        rendered = dumps_coach_export(payload)
        decoded = json.loads(rendered)
        event = next(
            item for item in decoded["events"] if item["event_id"] == "decision:2"
        )

        self.assertLessEqual(len(event["human_reason_untrusted"]), 1000)
        self.assertTrue(event["human_reason_untrusted"].endswith("..."))
        self.assertEqual(decoded["snapshot_id"], json.loads(dumps_coach_export(payload))["snapshot_id"])

    def test_event_set_id_ignores_export_timestamp(self) -> None:
        first_state = state_with_signals()
        second_state = state_with_signals()
        first_state["exported_at"] = "2026-06-24T00:00:00Z"
        second_state["exported_at"] = "2026-06-25T00:00:00Z"

        first = build_coach_export(first_state)
        second = build_coach_export(second_state)

        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["event_set_id"], second["event_set_id"])

    def test_event_set_id_ignores_unemitted_unrelated_repository_rows(self) -> None:
        first_state = state_with_signals()
        second_state = state_with_signals()
        second_state["finding_observations"].append(
            {
                "id": 99,
                "repository": "other/repo",
                "pr_number": 7,
                "head_sha": "b" * 40,
                "fingerprint": "feedfacefeed",
                "title": "Other repo finding",
                "path": "backend/other.py",
            }
        )
        second_state["decisions"].append(
            {
                "id": 99,
                "fingerprint": "feedfacefeed",
                "observation_id": 99,
                "decision": "false_positive",
                "reason": "Unrelated repo signal.",
            }
        )

        first = build_coach_export(first_state, repository="eneo-ai/eneo")
        second = build_coach_export(second_state, repository="eneo-ai/eneo")

        self.assertEqual(first["cursor"]["max_decision_id"], 2)
        self.assertEqual(second["cursor"]["max_decision_id"], 99)
        self.assertEqual(first["events"], second["events"])
        self.assertEqual(first["event_set_id"], second["event_set_id"])

    def test_decision_chain_is_capped_but_total_is_preserved(self) -> None:
        state = state_with_signals()
        state["decisions"] = [
            {
                "id": index,
                "fingerprint": "abcdef1234567890",
                "observation_id": 11,
                "decision": "reopen" if index < 25 else "false_positive",
                "reason": f"decision {index}",
            }
            for index in range(1, 26)
        ]

        payload = build_coach_export(state)
        event = payload["events"][0]

        self.assertEqual(event["decision_chain_total"], 25)
        self.assertEqual(len(event["decision_chain"]), 20)
        self.assertEqual(event["decision_chain"][-1], "false_positive")
        self.assertEqual(event["related_event_ids_total"], 25)
        self.assertEqual(event["related_event_ids"][0], "decision:6")

    def test_coach_export_collapses_untrusted_reason_whitespace(self) -> None:
        payload = build_coach_export(state_with_signals("line one\n\tline two"))
        event = next(
            item for item in payload["events"] if item["event_id"] == "decision:2"
        )

        self.assertEqual(event["human_reason_untrusted"], "line one line two")

    def test_incomplete_signals_are_excluded_by_default(self) -> None:
        state = state_with_signals("")
        default_payload = build_coach_export(state, after_feedback_id=7)
        inclusive_payload = build_coach_export(
            state, after_feedback_id=7, include_incomplete=True
        )

        self.assertEqual(default_payload["events"], [])
        self.assertEqual(len(inclusive_payload["events"]), 1)

    def test_private_writer_uses_0600_and_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "coach.json"
            write_private_file(destination, "{}\n")

            mode = stat.S_IMODE(destination.stat().st_mode)
            self.assertEqual(mode, 0o600)

            if hasattr(os, "symlink"):
                target = Path(temp) / "target.json"
                link = Path(temp) / "link.json"
                target.write_text("", encoding="utf-8")
                os.symlink(target, link)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    write_private_file(link, "{}\n")


if __name__ == "__main__":
    unittest.main()
