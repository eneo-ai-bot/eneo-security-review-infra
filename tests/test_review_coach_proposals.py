from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from eneo_review_coach import COACH_EVENT_GROUPS, build_coach_export
from eneo_review_coach_proposals import (
    POSITIVE_PATTERN_REASON,
    PROPOSAL_FORBIDDEN_ACTIONS,
    PROPOSAL_SUPPORTED_EVENT_GROUPS,
    PROPOSAL_SUPPORTED_EVENT_TYPES,
    PROPOSAL_SUPPORTED_SIGNAL_STRENGTHS,
    PROPOSAL_SUPPORTED_SUGGESTED_ROUTES,
    REVIEW_QUALITY_PROVENANCE_REASON,
    build_proposal,
    dumps_proposal_bundle,
    load_proposal_bundle,
    render_markdown,
    verify_proposal_bundle,
)
from eneo_review_learning import (
    EMITTED_EVENT_TYPES,
    EMITTED_SIGNAL_STRENGTHS,
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


def learning_state() -> dict[str, object]:
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
            {
                "id": 33,
                "repository": "eneo-ai/eneo",
                "pr_number": 242,
                "head_sha": "c" * 40,
                "fingerprint": "1111222233334444",
                "title": "Finding was resolved",
                "path": "backend/src/intric/sysadmin/sysadmin_router.py",
            },
            {
                "id": 44,
                "repository": "eneo-ai/eneo",
                "pr_number": 243,
                "head_sha": "d" * 40,
                "fingerprint": "9999000011112222",
                "title": "Incomplete false positive",
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
            {
                "id": 3,
                "fingerprint": "1111222233334444",
                "observation_id": 33,
                "decision": "resolved",
                "reason": "Fixed with a regression test.",
            },
            {
                "id": 4,
                "fingerprint": "9999000011112222",
                "observation_id": 44,
                "decision": "false_positive",
                "reason": "",
            },
        ],
        "review_quality_feedback": [
            {
                "id": 7,
                "repository": "eneo-ai/eneo",
                "pr_number": 240,
                "local_reference": "F7",
                "category": "unclear",
                "reason": "The review was hard to act on.",
            }
        ],
    }


def proposal_json(
    payload: dict[str, object],
    *,
    max_candidates: int = 3,
) -> dict[str, object]:
    return build_proposal(payload, max_candidates=max_candidates).to_json_obj()


def maximal_proposal_bundle():
    return build_proposal(
        coach_export(
            [
                event("decision:1", observation_id=11, pr_number=240),
                event("decision:2", observation_id=22, pr_number=241),
                event(
                    "decision:3",
                    event_type="accepted_risk",
                    route="exact_decision",
                    observation_id=33,
                    fingerprint="1111222233334444",
                    pr_number=242,
                ),
                event(
                    "decision:4",
                    event_type="resolved",
                    route="positive_pattern",
                    group="positive_pattern",
                    observation_id=44,
                    fingerprint="5555666677778888",
                    pr_number=243,
                ),
            ]
        )
    )


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

    def test_emitted_learning_and_coach_vocabularies_build_proposals(self) -> None:
        payload = build_coach_export(
            learning_state(),
            repository="eneo-ai/eneo",
            include_incomplete=True,
        )
        events = payload["events"]
        assert isinstance(events, list)
        emitted_groups: set[str] = set()
        emitted_strengths: set[str] = set()
        for raw_item in events:
            assert isinstance(raw_item, dict)
            item = cast(dict[str, object], raw_item)
            event_group = item["event_group"]
            signal_strength = item["signal_strength"]
            suggested_route = item["suggested_route"]
            event_type = item["event_type"]
            assert isinstance(event_group, str)
            assert isinstance(signal_strength, str)
            assert isinstance(suggested_route, str)
            assert isinstance(event_type, str)
            emitted_groups.add(event_group)
            emitted_strengths.add(signal_strength)
            self.assertIn(event_group, PROPOSAL_SUPPORTED_EVENT_GROUPS)
            self.assertIn(signal_strength, PROPOSAL_SUPPORTED_SIGNAL_STRENGTHS)
            self.assertIn(suggested_route, PROPOSAL_SUPPORTED_SUGGESTED_ROUTES)
            self.assertIn(event_type, PROPOSAL_SUPPORTED_EVENT_TYPES)

        bundle = build_proposal(payload)

        self.assertEqual(
            emitted_groups,
            set(COACH_EVENT_GROUPS),
        )
        self.assertEqual(emitted_strengths, set(EMITTED_SIGNAL_STRENGTHS))
        self.assertEqual(bundle.decision, "propose")
        self.assertEqual(bundle.candidates[0].event_type, "false_positive")

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
        with self.assertRaisesRegex(ValueError, "schema_version"):
            proposal_json(
                {"schema_version": True, "event_set_id": "sha256:x", "events": []}
            )
        with self.assertRaisesRegex(ValueError, "event_set_id"):
            proposal_json({"schema_version": 1, "events": []})
        with self.assertRaisesRegex(ValueError, "event_set_id must be a string"):
            proposal_json({"schema_version": 1, "event_set_id": 12, "events": []})
        with self.assertRaisesRegex(ValueError, "missing events"):
            proposal_json({"schema_version": 1, "event_set_id": "sha256:x"})
        payload = coach_export([])
        payload["actor_login"] = "alice"
        with self.assertRaisesRegex(ValueError, "unknown keys: actor_login"):
            proposal_json(payload)

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

        bad_list_item = event("decision:4")
        bad_list_item["related_event_ids"] = [42]
        with self.assertRaisesRegex(ValueError, "related_event_ids\\[0\\]"):
            proposal_json(coach_export([bad_list_item]))

    def test_strict_event_fields_reject_coercion_and_unknown_values(self) -> None:
        bad_title = event("decision:1")
        bad_title["reviewer_title_untrusted"] = 12
        with self.assertRaisesRegex(
            ValueError, "reviewer_title_untrusted must be a string"
        ):
            proposal_json(coach_export([bad_title]))

        bad_pr = event("decision:2")
        bad_pr["source"]["pr_number"] = "240"
        with self.assertRaisesRegex(ValueError, "pr_number must be an integer"):
            proposal_json(coach_export([bad_pr]))

        bad_observation = event("decision:3")
        bad_observation["source"]["observation_id"] = True
        with self.assertRaisesRegex(ValueError, "observation_id must be an integer"):
            proposal_json(coach_export([bad_observation]))

        bad_route = event("decision:4")
        bad_route["suggested_route"] = "made_up_route"
        with self.assertRaisesRegex(ValueError, "suggested_route has unsupported"):
            proposal_json(coach_export([bad_route]))

        bad_event_type = event("decision:5")
        bad_event_type["event_type"] = "made_up_event"
        with self.assertRaisesRegex(ValueError, "event_type has unsupported"):
            proposal_json(coach_export([bad_event_type]))

    def test_unknown_event_and_source_keys_fail_loudly(self) -> None:
        bad_event = event("decision:1")
        bad_event["actor_login"] = "alice"
        with self.assertRaisesRegex(ValueError, "unknown keys: actor_login"):
            proposal_json(coach_export([bad_event]))

        bad_source = event("decision:2")
        bad_source["source"]["source_comment_url"] = "https://github.test/comment/1"
        with self.assertRaisesRegex(ValueError, "unknown keys: source_comment_url"):
            proposal_json(coach_export([bad_source]))

    def test_duplicate_event_ids_fail_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate event_id"):
            proposal_json(
                coach_export(
                    [
                        event("decision:1", observation_id=11),
                        event("decision:1", observation_id=22),
                    ]
                )
            )

    def test_incomplete_events_are_filtered_even_when_present(self) -> None:
        incomplete = event("decision:1")
        incomplete["missing_evidence"] = ["exact observation provenance"]
        bundle = proposal_json(coach_export([incomplete]))

        self.assertEqual(bundle["decision"], "no_change")
        self.assertEqual(bundle["candidates"], [])
        self.assertEqual(bundle["rejected_groups"], [])

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

    def test_proposal_bundle_round_trips_from_maximal_artifact(self) -> None:
        bundle = maximal_proposal_bundle()
        self.assertEqual(len(bundle.candidates), 1)
        self.assertEqual(len(bundle.governance_observations), 1)
        self.assertEqual(len(bundle.rejected_groups), 1)

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "proposal.json"
            path.write_text(dumps_proposal_bundle(bundle), encoding="utf-8")

            loaded = load_proposal_bundle(path)
            verification = verify_proposal_bundle(loaded)

        self.assertEqual(loaded, bundle)
        self.assertEqual(verification.proposal_set_id, bundle.proposal_set_id)
        self.assertEqual(verification.forbidden_actions, PROPOSAL_FORBIDDEN_ACTIONS)

    def test_proposal_parser_rejects_unknown_keys_at_each_level(self) -> None:
        bundle = maximal_proposal_bundle().to_json_obj()

        cases: list[tuple[str, dict[str, object]]] = []
        top_level = json.loads(json.dumps(bundle))
        top_level["actor_login"] = "alice"
        cases.append(("proposal bundle contains unknown keys: actor_login", top_level))

        candidate_level = json.loads(json.dumps(bundle))
        candidate_level["candidates"][0]["extra"] = "value"
        cases.append(("candidates[0] contains unknown keys: extra", candidate_level))

        evidence_level = json.loads(json.dumps(bundle))
        evidence_level["candidates"][0]["evidence"][0]["extra"] = "value"
        cases.append(
            ("candidates[0].evidence[0] contains unknown keys: extra", evidence_level)
        )

        rejected_level = json.loads(json.dumps(bundle))
        rejected_level["rejected_groups"][0]["extra"] = "value"
        cases.append(("rejected_groups[0] contains unknown keys: extra", rejected_level))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, (message, payload) in enumerate(cases):
                path = root / f"proposal-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, re.escape(message)):
                        load_proposal_bundle(path)

    def test_proposal_parser_rejects_bad_decision_and_non_strict_ints(self) -> None:
        bundle = maximal_proposal_bundle().to_json_obj()

        cases: list[tuple[str, dict[str, object]]] = []
        bad_decision = json.loads(json.dumps(bundle))
        bad_decision["decision"] = "ready_for_human_review"
        cases.append(("unsupported value", bad_decision))

        bad_schema_bool = json.loads(json.dumps(bundle))
        bad_schema_bool["schema_version"] = True
        cases.append(("schema_version", bad_schema_bool))

        bad_count_string = json.loads(json.dumps(bundle))
        bad_count_string["candidates"][0]["independent_episode_count"] = "2"
        cases.append(("independent_episode_count must be an integer", bad_count_string))

        bad_events_bool = json.loads(json.dumps(bundle))
        bad_events_bool["events_considered"] = True
        cases.append(("events_considered must be an integer", bad_events_bool))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, (message, payload) in enumerate(cases):
                path = root / f"proposal-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        load_proposal_bundle(path)

    def test_proposal_parser_rejects_tampered_content_hash(self) -> None:
        payload = maximal_proposal_bundle().to_json_obj()
        payload["candidates"][0]["evidence_event_ids"][0] = "decision:999"

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "proposal.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "proposal_set_id"):
                load_proposal_bundle(path)

    def test_proposal_verifier_rejects_structural_invariant_drift(self) -> None:
        base = maximal_proposal_bundle().to_json_obj()
        cases: list[tuple[str, dict[str, object]]] = []

        bad_decision = json.loads(json.dumps(base))
        bad_decision["decision"] = "no_change"
        cases.append(("decision does not match candidate presence", bad_decision))

        bad_episode_count = json.loads(json.dumps(base))
        bad_episode_count["candidates"][0]["independent_episode_count"] = 99
        cases.append(
            ("independent_episode_count does not match keys", bad_episode_count)
        )

        bad_evidence_total = json.loads(json.dumps(base))
        bad_evidence_total["candidates"][0]["evidence_events_total"] = 1
        cases.append(("evidence_events_total is smaller", bad_evidence_total))

        bad_nested_evidence = json.loads(json.dumps(base))
        bad_nested_evidence["candidates"][0]["evidence"][0]["event_id"] = "decision:999"
        cases.append(("evidence_event_ids do not match evidence", bad_nested_evidence))

        bad_validation = json.loads(json.dumps(base))
        bad_validation["candidates"][0]["required_validation"] = []
        cases.append(("required_validation must not be empty", bad_validation))

        bad_rejected_total = json.loads(json.dumps(base))
        bad_rejected_total["rejected_groups"][0]["events_total"] = 0
        cases.append(("events_total is smaller than event ids", bad_rejected_total))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, (message, payload) in enumerate(cases):
                path = root / f"proposal-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, re.escape(message)):
                        load_proposal_bundle(path)

    def test_proposal_parser_round_trips_null_optional_evidence_fields(self) -> None:
        first = event("decision:1", observation_id=11, pr_number=240)
        second = event("decision:2", observation_id=22, pr_number=241)
        first["source"]["pr_number"] = None
        first["source"]["observation_id"] = None
        second["source"]["pr_number"] = None
        second["source"]["observation_id"] = None
        bundle = build_proposal(coach_export([first, second]))

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "proposal.json"
            path.write_text(dumps_proposal_bundle(bundle), encoding="utf-8")
            loaded = load_proposal_bundle(path)

        self.assertIsNone(loaded.candidates[0].evidence[0].pr_number)
        self.assertIsNone(loaded.candidates[0].evidence[0].observation_id)
        self.assertEqual(loaded, bundle)

    def test_cli_verifies_proposal_artifact_without_writing_or_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proposal_path = root / "proposal.json"
            proposal_path.write_text(
                dumps_proposal_bundle(maximal_proposal_bundle()),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "eneo_review_memory.py"),
                    "coach-verify-proposal",
                    "--proposal",
                    str(proposal_path),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

        receipt = json.loads(completed.stdout)
        self.assertEqual(receipt["decision"], "propose")
        self.assertEqual(receipt["candidates_count"], 1)
        self.assertEqual(receipt["governance_observations_count"], 1)
        self.assertEqual(receipt["rejected_groups_count"], 1)
        self.assertEqual(receipt["forbidden_actions"], list(PROPOSAL_FORBIDDEN_ACTIONS))

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
