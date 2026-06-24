from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from eneo_review_coach_proposals import (
    POSITIVE_PATTERN_REASON,
    PROPOSAL_SUPPORTED_EVENT_TYPES,
    PROPOSAL_SUPPORTED_SUGGESTED_ROUTES,
    REVIEW_QUALITY_PROVENANCE_REASON,
    build_proposal,
    render_markdown,
)
from eneo_review_learning import (
    EMITTED_EVENT_TYPES,
    EMITTED_SUGGESTED_ROUTES,
)


def coach_export(events: list[dict[str, object]]) -> dict[str, object]:
    return {
        "snapshot_id": "sha256:snapshot",
        "event_set_id": "sha256:events",
        "schema_version": 1,
        "repository_untrusted": "eneo-ai/eneo",
        "cursor": {"after_decision_id": 0, "after_feedback_id": 0},
        "events": events,
    }


def proposal_json(
    payload: dict[str, object],
    *,
    max_candidates: int = 3,
) -> dict[str, object]:
    return build_proposal(payload, max_candidates=max_candidates).to_json_obj()


def event(
    event_id: str,
    *,
    event_type: str = "false_positive",
    route: str = "judgment_or_procedure",
    group: str = "decision_candidate",
    observation_id: int | None = 1,
    fingerprint: str = "abcdef1234567890",
    pr_number: int = 240,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_group": group,
        "event_type": event_type,
        "signal_strength": "strong",
        "suggested_route": route,
        "promotion_eligible": True,
        "missing_evidence": [],
        "human_reason_untrusted": "Reviewer marked this as a false positive.",
        "reviewer_title_untrusted": "Tenant scope claim was wrong",
        "next_step_untrusted": "Add replay before changing policy.",
        "related_event_ids": [event_id],
        "source": {
            "repository_untrusted": "eneo-ai/eneo",
            "pr_number": pr_number,
            "fingerprint": fingerprint,
            "observation_id": observation_id,
            "local_reference": "F1",
        },
    }


class CoachProposalTests(unittest.TestCase):
    def test_vocabulary_maps_cover_learning_signal_routes_and_events(self) -> None:
        self.assertLessEqual(
            EMITTED_SUGGESTED_ROUTES,
            PROPOSAL_SUPPORTED_SUGGESTED_ROUTES,
        )
        self.assertLessEqual(EMITTED_EVENT_TYPES, PROPOSAL_SUPPORTED_EVENT_TYPES)

    def test_single_false_positive_is_not_promoted(self) -> None:
        bundle = proposal_json(coach_export([event("decision:1")]))

        self.assertEqual(bundle["decision"], "no_change")
        self.assertEqual(bundle["candidates"], [])
        rejected = bundle["rejected_groups"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("requires 2 independent episodes", rejected[0]["reason"])

    def test_two_independent_episodes_promote_one_candidate(self) -> None:
        bundle = proposal_json(
            coach_export(
                [
                    event("decision:1", observation_id=11, pr_number=240),
                    event("decision:2", observation_id=22, pr_number=241),
                ]
            )
        )

        self.assertEqual(bundle["decision"], "propose")
        candidates = bundle["candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["event_type"], "false_positive")
        self.assertEqual(candidate["target_owner"], "replay_then_skill")
        self.assertEqual(candidate["independent_episode_count"], 2)
        self.assertEqual(candidate["evidence_event_ids"], ["decision:1", "decision:2"])
        self.assertTrue(str(bundle["proposal_set_id"]).startswith("sha256:"))
        markdown = render_markdown(
            build_proposal(
                coach_export(
                    [
                        event("decision:1", observation_id=11, pr_number=240),
                        event("decision:2", observation_id=22, pr_number=241),
                    ]
                )
            )
        )
        self.assertIn("## Copyable next step", markdown)
        self.assertIn(candidate["candidate_key"], markdown)

    def test_same_observation_chain_counts_once(self) -> None:
        bundle = proposal_json(
            coach_export(
                [
                    event("decision:1", observation_id=11),
                    event("decision:2", observation_id=11),
                ]
            )
        )

        self.assertEqual(bundle["decision"], "no_change")
        self.assertEqual(bundle["rejected_groups"][0]["independent_episode_count"], 1)

    def test_single_accepted_risk_is_governance_not_candidate(self) -> None:
        bundle = proposal_json(
            coach_export(
                [
                    event(
                        "decision:9",
                        event_type="accepted_risk",
                        route="exact_decision",
                        observation_id=99,
                    )
                ]
            )
        )

        self.assertEqual(bundle["decision"], "no_change")
        self.assertEqual(bundle["candidates"], [])
        self.assertEqual(len(bundle["governance_observations"]), 1)
        self.assertEqual(bundle["governance_observations"][0]["event_id"], "decision:9")
        self.assertEqual(bundle["rejected_groups"], [])

    def test_repeated_accepted_risk_can_become_governance_candidate(self) -> None:
        bundle = proposal_json(
            coach_export(
                [
                    event(
                        "decision:9",
                        event_type="accepted_risk",
                        route="exact_decision",
                        observation_id=99,
                        pr_number=240,
                    ),
                    event(
                        "decision:10",
                        event_type="accepted_risk",
                        route="exact_decision",
                        observation_id=100,
                        pr_number=241,
                    ),
                ]
            )
        )

        self.assertEqual(bundle["decision"], "propose")
        self.assertEqual(bundle["candidates"][0]["event_type"], "accepted_risk")
        self.assertEqual(bundle["candidates"][0]["target_owner"], "governance_or_adr")
        self.assertEqual(bundle["governance_observations"], [])

    def test_unprovenanced_review_quality_feedback_is_explicitly_deferred(self) -> None:
        bundle = proposal_json(
            coach_export(
                [
                    event(
                        "feedback:7",
                        event_type="missed_issue",
                        route="procedure_or_mechanical_gap",
                        group="review_quality_signal",
                        observation_id=None,
                        fingerprint="",
                    )
                ]
            )
        )

        self.assertEqual(bundle["decision"], "no_change")
        self.assertEqual(bundle["candidates"], [])
        self.assertEqual(
            bundle["rejected_groups"][0]["reason"],
            REVIEW_QUALITY_PROVENANCE_REASON,
        )

    def test_positive_patterns_are_not_improvement_candidates_yet(self) -> None:
        bundle = proposal_json(
            coach_export(
                [
                    event(
                        "decision:30",
                        event_type="resolved",
                        route="positive_pattern",
                        group="positive_pattern",
                        observation_id=30,
                    ),
                    event(
                        "decision:31",
                        event_type="resolved",
                        route="positive_pattern",
                        group="positive_pattern",
                        observation_id=31,
                    ),
                ]
            )
        )

        self.assertEqual(bundle["decision"], "no_change")
        self.assertEqual(bundle["candidates"], [])
        self.assertEqual(bundle["rejected_groups"][0]["reason"], POSITIVE_PATTERN_REASON)

    def test_contradictory_outcome_route_sorts_before_normal_false_positive(self) -> None:
        bundle = proposal_json(
            coach_export(
                [
                    event(
                        "decision:10",
                        event_type="false_positive",
                        route="judgment_or_procedure",
                        observation_id=10,
                    ),
                    event(
                        "decision:11",
                        event_type="false_positive",
                        route="judgment_or_procedure",
                        observation_id=11,
                    ),
                    event(
                        "decision:20",
                        event_type="resolved",
                        route="contradictory_outcome",
                        observation_id=20,
                    ),
                    event(
                        "decision:21",
                        event_type="resolved",
                        route="contradictory_outcome",
                        observation_id=21,
                    ),
                ]
            ),
            max_candidates=1,
        )

        self.assertEqual(len(bundle["candidates"]), 1)
        self.assertEqual(bundle["candidates"][0]["suggested_route"], "contradictory_outcome")

    def test_evidence_events_are_capped_but_total_is_preserved(self) -> None:
        bundle = proposal_json(
            coach_export(
                [
                    event(
                        f"decision:{index}",
                        observation_id=index,
                        pr_number=240 + index,
                    )
                    for index in range(1, 8)
                ]
            )
        )

        candidate = bundle["candidates"][0]
        self.assertEqual(candidate["evidence_events_total"], 7)
        self.assertEqual(len(candidate["evidence_event_ids"]), 5)
        self.assertEqual(len(candidate["evidence"]), 5)

    def test_malformed_coach_export_fails_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema_version"):
            proposal_json(
                {"schema_version": 999, "event_set_id": "sha256:x", "events": []}
            )
        with self.assertRaisesRegex(ValueError, "event_set_id"):
            proposal_json({"schema_version": 1, "events": []})
        with self.assertRaisesRegex(ValueError, "missing events"):
            proposal_json({"schema_version": 1, "event_set_id": "sha256:x"})

    def test_malformed_event_fields_fail_loudly(self) -> None:
        bad_source = event("decision:1")
        bad_source["source"] = "not-object"
        with self.assertRaisesRegex(ValueError, "source must be an object"):
            proposal_json(coach_export([bad_source]))

        bad_bool = event("decision:2")
        bad_bool["promotion_eligible"] = "yes"
        with self.assertRaisesRegex(ValueError, "promotion_eligible must be a boolean"):
            proposal_json(coach_export([bad_bool]))

        bad_list = event("decision:3")
        bad_list["missing_evidence"] = "none"
        with self.assertRaisesRegex(ValueError, "missing_evidence must be a list"):
            proposal_json(coach_export([bad_list]))

    def test_incomplete_events_are_filtered_even_when_present(self) -> None:
        incomplete = event("decision:1")
        incomplete["missing_evidence"] = ["exact observation provenance"]
        bundle = proposal_json(coach_export([incomplete]))

        self.assertEqual(bundle["decision"], "no_change")
        self.assertEqual(bundle["candidates"], [])
        self.assertEqual(bundle["rejected_groups"], [])

    def test_extra_private_fields_are_not_rendered(self) -> None:
        noisy = event("decision:1", observation_id=11, pr_number=240)
        noisy["actor_login"] = "alice"
        noisy["source_comment_url"] = "https://github.test/comment/1"
        bundle = build_proposal(
            coach_export([noisy, event("decision:2", observation_id=22, pr_number=241)])
        )
        rendered_json = json.dumps(bundle.to_json_obj(), sort_keys=True)
        markdown = render_markdown(bundle)

        self.assertNotIn("alice", rendered_json)
        self.assertNotIn("github.test", rendered_json)
        self.assertNotIn("alice", markdown)
        self.assertNotIn("github.test", markdown)

    def test_proposal_set_id_is_stable_for_equivalent_evidence(self) -> None:
        events = [
            event("decision:1", observation_id=11, pr_number=240),
            event("decision:2", observation_id=22, pr_number=241),
        ]
        first = proposal_json(coach_export(events))
        second_export = coach_export(
            [
                event("decision:1", observation_id=11, pr_number=240),
                event("decision:2", observation_id=22, pr_number=241),
            ]
        )
        second_export["snapshot_id"] = "sha256:different-snapshot"
        second = proposal_json(second_export)

        self.assertEqual(first["proposal_set_id"], second["proposal_set_id"])

    def test_cli_writes_private_proposal_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export_path = root / "coach.json"
            output_dir = root / "proposal"
            export_path.write_text(
                json.dumps(
                    coach_export(
                        [
                            event("decision:1", observation_id=11, pr_number=240),
                            event("decision:2", observation_id=22, pr_number=241),
                        ]
                    )
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "eneo_review_memory.py"),
                    "coach-propose",
                    "--events",
                    str(export_path),
                    "--output-dir",
                    str(output_dir),
                ],
                cwd=ROOT,
                check=True,
            )

            for name in ["proposal.json", "SUMMARY.md"]:
                path = output_dir / name
                self.assertTrue(path.exists())
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
