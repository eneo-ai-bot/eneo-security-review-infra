#!/usr/bin/env python3
"""Human administration for the Eneo review-memory SQLite database."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast

from eneo_review_coach_proposals import ProposalBundle
from eneo_review_coach_proposals import ProposalVerification
from eneo_review_coach_run import build_coach_run_artifacts
from eneo_review_learning import LearningReport
from eneo_review_private_io import write_private_file
from eneo_review_replay import ReplayValidationResult

if TYPE_CHECKING:
    from eneo_review_tools.memory_coach import (
        CoachCandidateInput as MemoryCoachCandidateInput,
        CoachRunDecision as MemoryCoachRunDecision,
        CoachRunInput as MemoryCoachRunInput,
    )

JsonObject = Mapping[str, object]


class CoachRunRow(Protocol):
    def to_json_obj(self) -> dict[str, object]: ...


class MemoryDbModule(Protocol):
    ReviewMemoryError: type[Exception]
    CoachCandidateInput: type[MemoryCoachCandidateInput]
    CoachRunInput: type[MemoryCoachRunInput]

    def connect(self, explicit: str | None = None) -> sqlite3.Connection: ...
    def connect_existing(self, explicit: str | None = None) -> sqlite3.Connection: ...
    def database_path(self, explicit: str | None = None) -> Path: ...
    def json_dumps(self, value: object) -> str: ...
    def migrate_volume(
        self,
        source: str,
        destination: str,
        *,
        owner_uid: int | None = None,
        owner_gid: int | None = None,
    ) -> Mapping[str, object]: ...
    def list_findings(
        self,
        connection: sqlite3.Connection,
        *,
        repository: str | None = None,
        limit: int = 50,
        include_suppressed: bool = True,
    ) -> list[dict[str, object]]: ...
    def resolve_fingerprint(
        self, connection: sqlite3.Connection, prefix: str
    ) -> str: ...
    def active_suppression(
        self, connection: sqlite3.Connection, fingerprint: str
    ) -> dict[str, object] | None: ...
    def add_decision(
        self,
        connection: sqlite3.Connection,
        fingerprint_or_prefix: str,
        decision: str,
        reason: str,
        actor: str,
        *,
        expires_days: int | None = None,
        observation_id: int | None = None,
        repository: str | None = None,
        pr_number: int | None = None,
        local_reference: str = "",
        latest: bool = False,
    ) -> dict[str, object]: ...
    def export_state(self, connection: sqlite3.Connection) -> dict[str, object]: ...
    def compute_stats(
        self,
        connection: sqlite3.Connection,
        *,
        repository: str | None = None,
        expiring_within_days: int = 30,
    ) -> dict[str, object]: ...
    def run_stats(
        self,
        connection: sqlite3.Connection,
        *,
        repository: str | None = None,
        days: int = 30,
    ) -> dict[str, object]: ...
    def list_runs(
        self,
        connection: sqlite3.Connection,
        *,
        repository: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]: ...
    def list_publications(
        self,
        connection: sqlite3.Connection,
        *,
        repository: str | None = None,
        pr_number: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]: ...
    def mark_stale_runs_failed(
        self,
        connection: sqlite3.Connection,
        *,
        older_than_minutes: int = 30,
        repository: str | None = None,
        pr_number: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]: ...
    def run_is_stale(self, run: Mapping[str, object]) -> bool: ...
    def record_coach_run(
        self, connection: sqlite3.Connection, item: MemoryCoachRunInput
    ) -> CoachRunRow: ...


class LearningModule(Protocol):
    def load_export(self, path: Path) -> Mapping[str, object]: ...
    def build_learning_report(
        self, state: Mapping[str, object], *, repository: str | None = None
    ) -> LearningReport: ...
    def render_markdown(self, report: LearningReport) -> str: ...


class CoachModule(Protocol):
    def build_coach_export(
        self,
        state: Mapping[str, object],
        *,
        repository: str | None = None,
        after_decision_id: int = 0,
        after_feedback_id: int = 0,
        include_incomplete: bool = False,
    ) -> dict[str, object]: ...
    def dumps_coach_export(self, payload: Mapping[str, object]) -> str: ...


class CoachProposalsModule(Protocol):
    def load_coach_export(self, path: Path) -> Mapping[str, object]: ...
    def load_proposal_bundle(self, path: Path) -> ProposalBundle: ...
    def verify_proposal_bundle(self, bundle: ProposalBundle) -> ProposalVerification: ...
    def build_proposal(
        self,
        coach_export: Mapping[str, object],
        *,
        max_candidates: int = 3,
        min_independent_episodes: int = 2,
    ) -> ProposalBundle: ...
    def dumps_proposal_bundle(self, bundle: ProposalBundle) -> str: ...
    def render_markdown(self, bundle: ProposalBundle) -> str: ...


class ReplayModule(Protocol):
    def validate_replay_path(self, path: Path) -> tuple[ReplayValidationResult, ...]: ...


def _import_module(name: str) -> ModuleType:
    return importlib.import_module(name)


def memory_module_candidates() -> tuple[Path, ...]:
    return (
        Path("/opt/eneo-bootstrap/plugins/eneo_review_tools"),
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins" / "eneo_review_tools",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools",
    )


def _module_is_from_candidate(module: ModuleType, candidate: Path) -> bool:
    raw = getattr(module, "__file__", None)
    if not isinstance(raw, str) or not raw:
        return False
    try:
        path = Path(raw).resolve()
        root = candidate.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def _evict_stale_memory_modules(candidate: Path) -> None:
    for name in (
        "memory_db",
        "memory_schema",
        "memory_migration",
        "memory_identity",
        "memory_decisions",
        "memory_findings",
        "memory_publications",
        "memory_feedback",
        "memory_reporting",
        "memory_runs",
        "memory_coach",
        "memory_validation",
        "feedback_authorization",
        "feedback_commands",
        "feedback_contract",
    ):
        module = sys.modules.get(name)
        if module is not None and not _module_is_from_candidate(module, candidate):
            sys.modules.pop(name, None)


def _describe_memory_source(module: ModuleType, candidate: Path) -> None:
    raw = getattr(module, "__file__", "unknown")
    print(
        f"review memory plugin source: {raw} (path={candidate})",
        file=sys.stderr,
        flush=True,
    )


def load_memory_module() -> MemoryDbModule:
    for candidate in memory_module_candidates():
        if (candidate / "memory_db.py").exists():
            # Installed Hermes plugins are path-loaded as top-level modules; the
            # Protocol above keeps this dynamic boundary explicit and typed.
            sys.path.insert(0, str(candidate))
            _evict_stale_memory_modules(candidate)
            module = _import_module("memory_db")
            _describe_memory_source(module, candidate)
            return cast(MemoryDbModule, module)
    raise SystemExit("Could not locate the eneo_review_tools plugin")


def load_learning_module() -> LearningModule:
    try:
        return cast(LearningModule, _import_module("eneo_review_learning"))
    except ModuleNotFoundError as exc:
        raise SystemExit("Could not locate the Eneo learning report module") from exc


def load_coach_module() -> CoachModule:
    try:
        return cast(CoachModule, _import_module("eneo_review_coach"))
    except ModuleNotFoundError as exc:
        raise SystemExit("Could not locate the Eneo coach export module") from exc


def load_coach_proposals_module() -> CoachProposalsModule:
    try:
        return cast(CoachProposalsModule, _import_module("eneo_review_coach_proposals"))
    except ModuleNotFoundError as exc:
        raise SystemExit("Could not locate the Eneo coach proposal module") from exc


def load_replay_module() -> ReplayModule:
    try:
        return cast(ReplayModule, _import_module("eneo_review_replay"))
    except ModuleNotFoundError as exc:
        raise SystemExit("Could not locate the Eneo replay validator module") from exc


def _nested(row: JsonObject, key: str) -> JsonObject:
    value = row.get(key, {})
    return cast(JsonObject, value) if isinstance(value, Mapping) else {}


def _coach_run_decision(value: str) -> MemoryCoachRunDecision:
    if value == "propose":
        return "propose"
    if value == "no_change":
        return "no_change"
    raise SystemExit(f"coach proposal returned unsupported decision: {value}")


def print_table(items: Sequence[JsonObject]) -> None:
    if not items:
        print("No findings.")
        return
    for item in items:
        marker = "SUPPRESSED" if item.get("suppressed") else "OPEN"
        line = item.get("line") or "?"
        print(
            f"{str(item['fingerprint'])[:12]}  {marker:10}  {str(item['severity']):8}  "
            f"{item.get('category', '-'):15} score={item.get('publication_score', '-')}  "
            f"{item['repository']}  {item['path']}:{line}  {item['title']}"
        )
        decision = _nested(item, "latest_decision")
        if decision:
            print(
                f"  decision={decision['decision']} actor={decision['actor']} "
                f"expires={decision.get('expires_at') or '-'} reason={decision['reason']}"
            )


def print_stats(stats: JsonObject) -> None:
    repo = stats.get("repository") or "(all repositories)"
    print(f"Eneo review memory - {repo}  (as of {stats['generated_at']})")
    print(f"  findings: {stats['findings_total']}  (no decision: {stats['findings_without_decision']})")
    by_severity = _nested(stats, "findings_by_severity")
    by_category = _nested(stats, "findings_by_category")
    latest_decisions = _nested(stats, "latest_decision_by_type")
    print("  by severity:  " + ", ".join(f"{k}={v}" for k, v in by_severity.items()))
    cats = ", ".join(f"{k}={v}" for k, v in by_category.items() if v) or "(none)"
    print(f"  by category:  {cats}")
    decs = ", ".join(f"{k}={v}" for k, v in latest_decisions.items() if v) or "(none)"
    print(f"  latest decision:  {decs}")
    print(
        f"  active suppressions: {stats['active_suppressions']} "
        f"(nearing expiry <={stats['active_suppressions_expiring_within_days']}d: "
        f"{stats['active_suppressions_nearing_expiry']})"
    )
    print(f"  repeats after a human decision (approx): {stats['repeats_after_decision_approx']}")


def print_runs(memory_db: MemoryDbModule, runs: Sequence[JsonObject]) -> None:
    if not runs:
        print("No review runs.")
        return
    for run in runs:
        findings = run["findings_count"] if run["findings_count"] is not None else "-"
        status = "stalled" if memory_db.run_is_stale(run) else run["status"]
        print(
            f"#{run['id']:<5} {status:8} {run['repository']}#{run['pr_number']}  "
            f"findings={findings}  started={run['started_at']}  "
            f"completed={run['completed_at'] or '-'}"
        )


def print_mark_stalled_result(result: JsonObject) -> None:
    count = result["failed_count"]
    print(
        f"Marked {count} stale running review run(s) as failed "
        f"(older than {result['older_than_minutes']}m, cutoff={result['cutoff']})."
    )
    runs = cast(Sequence[JsonObject], result.get("runs", ()))
    for run in runs:
        print(
            f"#{run['id']:<5} failed   {run['repository']}#{run['pr_number']}  "
            f"started={run['started_at']}  completed={run['completed_at']}"
        )


def print_publications(publications: Sequence[JsonObject]) -> None:
    if not publications:
        print("No review publications.")
        return
    for item in publications:
        run_id = item["review_run_id"] if item.get("review_run_id") is not None else "-"
        comment_id = item["comment_id"] if item.get("comment_id") is not None else "-"
        failure_code = item.get("failure_code") or "-"
        print(
            f"#{item['id']:<5} {str(item['delivery_status']):14} "
            f"{item['repository']}#{item['pr_number']}  run={run_id}  "
            f"comment={comment_id}  failure={failure_code}"
        )
        print(
            f"       generated={item.get('generated_at') or '-'}  "
            f"posting={item.get('posting_started_at') or '-'}  "
            f"posted={item.get('posted_at') or '-'}  "
            f"failed={item.get('publish_failed_at') or '-'}"
        )


def print_run_stats(stats: JsonObject) -> None:
    repo = stats.get("repository") or "(all repositories)"
    print(f"Eneo review runs - {repo}  (last {stats['window_days']}d, as of {stats['generated_at']})")
    print("  (best-effort telemetry recorded by the reviewer; treat counts as approximate)")
    print(f"  total: {stats['total']}")
    by_status = _nested(stats, "by_status")
    print("  by status:  " + ", ".join(f"{k}={v}" for k, v in by_status.items()))
    print(f"  stalled (running but likely crashed): {stats['stalled_running']}")
    tta = _nested(stats, "time_to_answer_seconds")
    print(f"  time to answer (s):  p50={tta['p50']}  p95={tta['p95']}")
    print(f"  avg findings / completed run:  {stats['avg_findings_per_completed_run']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Override ENEO_REVIEW_DB for this command.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create or migrate the database schema.")

    migrate_parser = sub.add_parser(
        "migrate-volume",
        help="Safely copy an initialized review-memory database into a new volume.",
    )
    migrate_parser.add_argument("--source", required=True)
    migrate_parser.add_argument("--destination", required=True)
    migrate_parser.add_argument("--owner-uid", type=int)
    migrate_parser.add_argument("--owner-gid", type=int)

    list_parser = sub.add_parser("list", help="List recent findings.")
    list_parser.add_argument("--repo", help="Limit to owner/repository.")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--open-only", action="store_true")
    list_parser.add_argument("--json", action="store_true")

    show_parser = sub.add_parser("show", help="Show one finding and its decision history.")
    show_parser.add_argument("fingerprint")

    decide_parser = sub.add_parser("decide", help="Append a human triage decision.")
    decide_parser.add_argument(
        "fingerprint",
        help="Finding fingerprint or prefix used to verify the explicit target.",
    )
    decide_parser.add_argument(
        "decision",
        help="Decision value validated by the review-memory database.",
    )
    decide_parser.add_argument("--reason", required=True)
    decide_parser.add_argument("--actor", required=True)
    decide_parser.add_argument("--expires-days", type=int)
    decide_parser.add_argument(
        "--observation-id",
        type=int,
        help="Exact finding_observations.id to attach this decision to.",
    )
    decide_parser.add_argument(
        "--repo",
        help="Repository for a PR-local finding reference target.",
    )
    decide_parser.add_argument(
        "--pr",
        type=int,
        help="Pull request number for a PR-local finding reference target.",
    )
    decide_parser.add_argument(
        "--local-reference",
        help="PR-local finding reference such as F1.",
    )
    decide_parser.add_argument(
        "--latest",
        action="store_true",
        help="Explicitly target the latest observation for the fingerprint.",
    )

    export_parser = sub.add_parser("export", help="Export findings and decisions as JSON.")
    export_parser.add_argument("--output", help="Write to a file instead of stdout.")

    stats_parser = sub.add_parser("stats", help="Summarize findings and human decisions.")
    stats_parser.add_argument("--repo", help="Limit to owner/repository.")
    stats_parser.add_argument("--expiring-within-days", type=int, default=30)
    stats_parser.add_argument("--json", action="store_true")

    runs_parser = sub.add_parser("runs", help="List recent review runs, or --stats for run metrics.")
    runs_parser.add_argument("--repo", help="Limit to owner/repository.")
    runs_parser.add_argument(
        "--pr",
        type=int,
        help="Limit --mark-stalled to one pull request. Requires --repo.",
    )
    runs_parser.add_argument("--limit", type=int, default=50)
    runs_parser.add_argument(
        "--stats", action="store_true", help="Show aggregate run metrics instead of a list."
    )
    runs_parser.add_argument(
        "--mark-stalled",
        action="store_true",
        help="Mark stale running runs as failed, then print the affected runs.",
    )
    runs_parser.add_argument(
        "--older-than-minutes",
        type=int,
        default=30,
        help="Age threshold for --mark-stalled. Default: 30.",
    )
    runs_parser.add_argument("--days", type=int, default=30, help="Window in days for --stats.")
    runs_parser.add_argument("--json", action="store_true")

    publications_parser = sub.add_parser(
        "publications",
        help="List generated, posted, stale, and failed review publications.",
    )
    publications_parser.add_argument("--repo", help="Limit to owner/repository.")
    publications_parser.add_argument(
        "--pr",
        type=int,
        help="Limit to one pull request. Requires --repo.",
    )
    publications_parser.add_argument("--limit", type=int, default=50)
    publications_parser.add_argument("--json", action="store_true")

    learning_parser = sub.add_parser(
        "learning-report",
        help="Generate a private learning-candidate report from an export JSON.",
    )
    learning_parser.add_argument(
        "--export",
        required=True,
        help="Path created by `eneo-review-memory export --output`.",
    )
    learning_parser.add_argument("--repo", help="Limit to owner/repository.")
    learning_parser.add_argument("--output", help="Write Markdown to a file instead of stdout.")

    replay_parser = sub.add_parser(
        "validate-replay",
        help="Validate typed replay fixture files.",
    )
    replay_parser.add_argument(
        "path",
        type=Path,
        help="Replay fixture file or directory containing *.json fixtures.",
    )

    coach_parser = sub.add_parser(
        "coach-export",
        help="Generate a bounded untrusted JSON bundle for the private review coach.",
    )
    coach_parser.add_argument(
        "--export",
        required=True,
        help="Path created by `eneo-review-memory export --output`.",
    )
    coach_parser.add_argument("--repo", help="Limit to owner/repository.")
    coach_parser.add_argument("--after-decision-id", type=int, default=0)
    coach_parser.add_argument("--after-feedback-id", type=int, default=0)
    coach_parser.add_argument("--include-incomplete", action="store_true")
    coach_parser.add_argument("--output", required=True, help="Write JSON to this file.")

    coach_propose_parser = sub.add_parser(
        "coach-propose",
        help="Select deterministic private coach improvement candidates from a coach export.",
    )
    coach_propose_parser.add_argument(
        "--events",
        required=True,
        help="Path created by `eneo-review-memory coach-export --output`.",
    )
    coach_propose_parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for proposal.json and SUMMARY.md.",
    )
    coach_propose_parser.add_argument("--max-candidates", type=int, default=3)
    coach_propose_parser.add_argument("--min-independent-episodes", type=int, default=2)

    coach_verify_parser = sub.add_parser(
        "coach-verify-proposal",
        help="Strictly read and verify a private coach proposal artifact.",
    )
    coach_verify_parser.add_argument(
        "--proposal",
        required=True,
        help="Path created by `eneo-review-memory coach-propose` or `coach-run`.",
    )

    coach_run_parser = sub.add_parser(
        "coach-run",
        help="Run the private coach pipeline in dry-run mode and record the result.",
    )
    coach_run_parser.add_argument(
        "--export",
        required=True,
        help="Path created by `eneo-review-memory export --output`.",
    )
    coach_run_parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for coach-export.json, proposal.json, and SUMMARY.md.",
    )
    coach_run_parser.add_argument("--repo", help="Limit to owner/repository.")
    coach_run_parser.add_argument("--after-decision-id", type=int, default=0)
    coach_run_parser.add_argument("--after-feedback-id", type=int, default=0)
    coach_run_parser.add_argument("--include-incomplete", action="store_true")
    coach_run_parser.add_argument("--max-candidates", type=int, default=3)
    coach_run_parser.add_argument("--min-independent-episodes", type=int, default=2)

    args = parser.parse_args()

    if args.command == "learning-report":
        learning = load_learning_module()
        state = learning.load_export(Path(args.export))
        report = learning.build_learning_report(state, repository=args.repo)
        content = learning.render_markdown(report)
        if args.output:
            destination = Path(args.output)
            write_private_file(destination, content)
            print(destination)
        else:
            print(content, end="")
        return 0

    if args.command == "validate-replay":
        replay = load_replay_module()
        cwd = str(Path.cwd())
        if cwd not in sys.path:
            sys.path.insert(0, cwd)
        results = replay.validate_replay_path(args.path)
        for result in results:
            print(f"Replay OK: {result.fixture_id} ({result.path})")
        print(f"Validated {len(results)} replay fixture(s).")
        return 0

    if args.command == "coach-export":
        learning = load_learning_module()
        coach = load_coach_module()
        state = learning.load_export(Path(args.export))
        payload = coach.build_coach_export(
            state,
            repository=args.repo,
            after_decision_id=args.after_decision_id,
            after_feedback_id=args.after_feedback_id,
            include_incomplete=args.include_incomplete,
        )
        destination = Path(args.output)
        write_private_file(destination, coach.dumps_coach_export(payload))
        print(destination)
        return 0

    if args.command == "coach-propose":
        proposals = load_coach_proposals_module()
        payload = proposals.load_coach_export(Path(args.events))
        bundle = proposals.build_proposal(
            payload,
            max_candidates=args.max_candidates,
            min_independent_episodes=args.min_independent_episodes,
        )
        output_dir = Path(args.output_dir)
        write_private_file(
            output_dir / "proposal.json",
            proposals.dumps_proposal_bundle(bundle),
        )
        write_private_file(output_dir / "SUMMARY.md", proposals.render_markdown(bundle))
        print(output_dir)
        return 0

    if args.command == "coach-verify-proposal":
        proposals = load_coach_proposals_module()
        bundle = proposals.load_proposal_bundle(Path(args.proposal))
        verification = proposals.verify_proposal_bundle(bundle)
        print(json.dumps(verification.to_json_obj(), sort_keys=True, indent=2))
        return 0

    if args.command == "coach-run":
        memory_db = load_memory_module()
        artifacts = build_coach_run_artifacts(
            export_path=Path(args.export),
            output_dir=Path(args.output_dir),
            repository=args.repo,
            after_decision_id=args.after_decision_id,
            after_feedback_id=args.after_feedback_id,
            include_incomplete=args.include_incomplete,
            max_candidates=args.max_candidates,
            min_independent_episodes=args.min_independent_episodes,
        )

        candidates = tuple(
            memory_db.CoachCandidateInput(
                candidate_key=candidate.candidate_key,
                target_owner=candidate.target_owner,
                suggested_route=candidate.suggested_route,
                event_type=candidate.event_type,
                independent_episode_count=candidate.independent_episode_count,
                evidence_event_ids=candidate.evidence_event_ids,
                evidence_events_total=candidate.evidence_events_total,
            )
            for candidate in artifacts.bundle.candidates
        )
        run_input = memory_db.CoachRunInput(
            repository=artifacts.bundle.repository_untrusted,
            source_event_set_id=artifacts.bundle.source_event_set_id,
            source_snapshot_id=artifacts.bundle.source_snapshot_id,
            proposal_set_id=artifacts.bundle.proposal_set_id,
            decision=_coach_run_decision(artifacts.bundle.decision),
            events_considered=artifacts.bundle.events_considered,
            artifact_dir=str(artifacts.paths.output_dir),
            candidates=candidates,
        )
        with closing(memory_db.connect_existing(args.db)) as connection:
            run = memory_db.record_coach_run(connection, run_input)
        print(
            memory_db.json_dumps(
                {
                    "run": run.to_json_obj(),
                    "artifacts": artifacts.paths.to_json_obj(),
                }
            )
        )
        return 0

    memory_db = load_memory_module()
    if args.command == "migrate-volume":
        try:
            result = memory_db.migrate_volume(
                args.source,
                args.destination,
                owner_uid=args.owner_uid,
                owner_gid=args.owner_gid,
            )
        except memory_db.ReviewMemoryError as exc:
            raise SystemExit(str(exc)) from exc
        print(memory_db.json_dumps(result))
        return 0

    opener = memory_db.connect if args.command == "init" else memory_db.connect_existing
    try:
        connection = opener(args.db)
    except memory_db.ReviewMemoryError as exc:
        raise SystemExit(str(exc)) from exc
    with closing(connection) as connection:
        if args.command == "init":
            print(f"Ready: {memory_db.database_path(args.db)}")
            return 0

        if args.command == "list":
            items = memory_db.list_findings(
                connection,
                repository=args.repo,
                limit=args.limit,
                include_suppressed=not args.open_only,
            )
            if args.json:
                print(memory_db.json_dumps(items))
            else:
                print_table(items)
            return 0

        if args.command == "show":
            try:
                fingerprint = memory_db.resolve_fingerprint(connection, args.fingerprint)
            except memory_db.ReviewMemoryError as exc:
                raise SystemExit(str(exc)) from exc
            finding = connection.execute(
                "SELECT * FROM findings WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if not finding:
                raise SystemExit("Unknown fingerprint")
            decisions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM decisions WHERE fingerprint = ? ORDER BY id", (fingerprint,)
                )
            ]
            print(
                memory_db.json_dumps(
                    {
                        "finding": dict(finding),
                        "decisions": decisions,
                        "active_suppression": memory_db.active_suppression(connection, fingerprint),
                    }
                )
            )
            return 0

        if args.command == "decide":
            try:
                result = memory_db.add_decision(
                    connection,
                    args.fingerprint,
                    args.decision,
                    args.reason,
                    args.actor,
                    expires_days=args.expires_days,
                    observation_id=args.observation_id,
                    repository=args.repo,
                    pr_number=args.pr,
                    local_reference=args.local_reference or "",
                    latest=args.latest,
                )
            except memory_db.ReviewMemoryError as exc:
                raise SystemExit(str(exc)) from exc
            print(memory_db.json_dumps(result))
            return 0

        if args.command == "export":
            content = memory_db.json_dumps(memory_db.export_state(connection)) + "\n"
            if args.output:
                destination = Path(args.output)
                write_private_file(destination, content)
                print(destination)
            else:
                print(content, end="")
            return 0

        if args.command == "stats":
            stats = memory_db.compute_stats(
                connection,
                repository=args.repo,
                expiring_within_days=args.expiring_within_days,
            )
            if args.json:
                print(memory_db.json_dumps(stats))
            else:
                print_stats(stats)
            return 0

        if args.command == "runs":
            if args.mark_stalled:
                if args.stats:
                    raise SystemExit("--mark-stalled cannot be combined with --stats")
                if args.pr is not None and not args.repo:
                    raise SystemExit("--pr requires --repo")
                try:
                    result = memory_db.mark_stale_runs_failed(
                        connection,
                        repository=args.repo,
                        pr_number=args.pr,
                        older_than_minutes=args.older_than_minutes,
                    )
                except memory_db.ReviewMemoryError as exc:
                    raise SystemExit(str(exc)) from exc
                if args.json:
                    print(memory_db.json_dumps(result))
                else:
                    print_mark_stalled_result(result)
            elif args.stats:
                run_metrics = memory_db.run_stats(connection, repository=args.repo, days=args.days)
                if args.json:
                    print(memory_db.json_dumps(run_metrics))
                else:
                    print_run_stats(run_metrics)
            else:
                runs = memory_db.list_runs(connection, repository=args.repo, limit=args.limit)
                if args.json:
                    print(memory_db.json_dumps(runs))
                else:
                    print_runs(memory_db, runs)
            return 0

        if args.command == "publications":
            if args.pr is not None and not args.repo:
                raise SystemExit("--pr requires --repo")
            publications = memory_db.list_publications(
                connection,
                repository=args.repo,
                pr_number=args.pr,
                limit=args.limit,
            )
            if args.json:
                print(memory_db.json_dumps(publications))
            else:
                print_publications(publications)
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
