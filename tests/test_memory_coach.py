from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


def sha256_id(seed: str) -> str:
    return f"sha256:{seed * 64}"


def candidate(key: str = "judgment-false-positive-abc123") -> memory_db.CoachCandidateInput:
    return memory_db.CoachCandidateInput(
        candidate_key=key,
        target_owner="replay_then_skill",
        suggested_route="judgment_or_procedure",
        event_type="false_positive",
        independent_episode_count=2,
        evidence_event_ids=("decision:2", "decision:1", "decision:1"),
        evidence_events_total=3,
    )


def run_input(
    *,
    decision: memory_db.CoachRunDecision = "propose",
    candidates: tuple[memory_db.CoachCandidateInput, ...] = (candidate(),),
) -> memory_db.CoachRunInput:
    return memory_db.CoachRunInput(
        repository="eneo-ai/eneo",
        source_event_set_id=sha256_id("a"),
        source_snapshot_id=sha256_id("b"),
        proposal_set_id=sha256_id("c"),
        decision=decision,
        events_considered=7,
        artifact_dir="/tmp/coach-artifacts",
        candidates=candidates,
    )


class MemoryCoachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = memory_db.connect(str(Path(self.temp.name) / "memory.sqlite3"))

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_fresh_schema_creates_current_coach_and_feedback_tables(self) -> None:
        tables = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        self.assertEqual(memory_db.SCHEMA_VERSION, 14)
        self.assertIn("coach_runs", tables)
        self.assertIn("coach_candidates", tables)
        self.assertIn("review_quality_feedback", tables)
        self.assertIn("publication_findings", tables)
        self.assertIn("review_verification_runs", tables)
        self.assertIn("candidate_verifications", tables)
        self.assertIn("candidate_reconciliations", tables)

    def test_no_change_run_records_without_candidates(self) -> None:
        item = run_input(decision="no_change", candidates=())
        run = memory_db.record_coach_run(self.connection, item)

        self.assertEqual(run.decision, "no_change")
        self.assertEqual(run.candidates_count, 0)
        self.assertEqual(run.events_considered, 7)
        self.assertEqual(memory_db.list_coach_candidates(self.connection), ())

    def test_candidate_runs_append_but_candidates_deduplicate(self) -> None:
        first_time = memory_db.utc_now() - timedelta(minutes=5)
        second_time = memory_db.utc_now()
        first = memory_db.record_coach_run(
            self.connection, run_input(), now=first_time
        )
        second = memory_db.record_coach_run(
            self.connection, run_input(), now=second_time
        )

        runs = memory_db.list_coach_runs(self.connection, repository="eneo-ai/eneo")
        candidates = memory_db.list_coach_candidates(
            self.connection, repository="eneo-ai/eneo"
        )

        self.assertEqual([run.id for run in runs], [second.id, first.id])
        self.assertEqual(len(candidates), 1)
        stored = candidates[0]
        self.assertEqual(stored.candidate_key, "judgment-false-positive-abc123")
        self.assertEqual(stored.seen_count, 2)
        self.assertEqual(stored.first_seen_at, first.recorded_at)
        self.assertEqual(stored.last_seen_at, second.recorded_at)
        self.assertEqual(stored.evidence_event_ids, ("decision:1", "decision:2"))
        self.assertEqual(stored.evidence_events_total, 3)

    def test_different_candidate_key_inserts_new_candidate(self) -> None:
        memory_db.record_coach_run(
            self.connection,
            run_input(candidates=(candidate("judgment-false-positive-aaa"),)),
        )
        memory_db.record_coach_run(
            self.connection,
            run_input(candidates=(candidate("judgment-false-positive-bbb"),)),
        )

        self.assertEqual(len(memory_db.list_coach_runs(self.connection)), 2)
        self.assertEqual(len(memory_db.list_coach_candidates(self.connection)), 2)

    def test_one_run_can_record_multiple_candidates(self) -> None:
        run = memory_db.record_coach_run(
            self.connection,
            run_input(
                candidates=(
                    candidate("judgment-false-positive-aaa"),
                    candidate("judgment-false-positive-bbb"),
                )
            ),
        )

        candidates = memory_db.list_coach_candidates(self.connection)
        self.assertEqual(run.candidates_count, 2)
        self.assertEqual(
            {item.candidate_key for item in candidates},
            {"judgment-false-positive-aaa", "judgment-false-positive-bbb"},
        )

    def test_invalid_stable_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "proposal_set_id"):
            memory_db.record_coach_run(
                self.connection,
                memory_db.CoachRunInput(
                    repository="eneo-ai/eneo",
                    source_event_set_id=sha256_id("a"),
                    source_snapshot_id=sha256_id("b"),
                    proposal_set_id="not-a-sha256-id",
                    decision="propose",
                    events_considered=7,
                    artifact_dir="/tmp/coach-artifacts",
                    candidates=(candidate(),),
                ),
            )

        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "source_event_set_id"):
            memory_db.record_coach_run(
                self.connection,
                memory_db.CoachRunInput(
                    repository="eneo-ai/eneo",
                    source_event_set_id="not-a-sha256-id",
                    source_snapshot_id=sha256_id("b"),
                    proposal_set_id=sha256_id("c"),
                    decision="propose",
                    events_considered=7,
                    artifact_dir="/tmp/coach-artifacts",
                    candidates=(candidate(),),
                ),
            )

    def test_decision_invariant_is_checked_in_code_and_database(self) -> None:
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.record_coach_run(
                self.connection, run_input(decision="no_change", candidates=(candidate(),))
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO coach_runs (
                    repository, source_event_set_id, proposal_set_id, decision,
                    events_considered, candidates_count, recorded_at
                ) VALUES ('eneo-ai/eneo', ?, ?, 'junk', 0, 0, '2026-06-24T00:00:00Z')
                """,
                (sha256_id("d"), sha256_id("e")),
            )


if __name__ == "__main__":
    unittest.main()
