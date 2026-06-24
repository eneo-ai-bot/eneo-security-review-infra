"""Offline dry-run orchestration for reviewer-coach proposal artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import eneo_review_coach
import eneo_review_coach_proposals
import eneo_review_learning
from eneo_review_private_io import write_private_file


@dataclass(frozen=True)
class CoachRunArtifactPaths:
    output_dir: Path
    coach_export: Path
    proposal: Path
    summary: Path

    def to_json_obj(self) -> dict[str, str]:
        return {
            "coach_export": str(self.coach_export),
            "proposal": str(self.proposal),
            "summary": str(self.summary),
        }


@dataclass(frozen=True)
class CoachRunArtifacts:
    bundle: eneo_review_coach_proposals.ProposalBundle
    paths: CoachRunArtifactPaths


def build_coach_run_artifacts(
    *,
    export_path: Path,
    output_dir: Path,
    repository: str | None = None,
    after_decision_id: int = 0,
    after_feedback_id: int = 0,
    include_incomplete: bool = False,
    max_candidates: int = eneo_review_coach_proposals.DEFAULT_MAX_CANDIDATES,
    min_independent_episodes: int = (
        eneo_review_coach_proposals.DEFAULT_MIN_INDEPENDENT_EPISODES
    ),
) -> CoachRunArtifacts:
    state = eneo_review_learning.load_export(export_path)
    coach_payload = eneo_review_coach.build_coach_export(
        state,
        repository=repository,
        after_decision_id=after_decision_id,
        after_feedback_id=after_feedback_id,
        include_incomplete=include_incomplete,
    )
    bundle = eneo_review_coach_proposals.build_proposal(
        coach_payload,
        max_candidates=max_candidates,
        min_independent_episodes=min_independent_episodes,
    )
    paths = CoachRunArtifactPaths(
        output_dir=output_dir,
        coach_export=output_dir / "coach-export.json",
        proposal=output_dir / "proposal.json",
        summary=output_dir / "SUMMARY.md",
    )
    write_private_file(
        paths.coach_export,
        eneo_review_coach.dumps_coach_export(coach_payload),
    )
    write_private_file(
        paths.proposal,
        eneo_review_coach_proposals.dumps_proposal_bundle(bundle),
    )
    write_private_file(paths.summary, eneo_review_coach_proposals.render_markdown(bundle))
    return CoachRunArtifacts(bundle=bundle, paths=paths)
