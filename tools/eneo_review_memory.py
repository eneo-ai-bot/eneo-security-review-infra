#!/usr/bin/env python3
"""Human administration for the Eneo review-memory SQLite database."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import closing
from pathlib import Path

from eneo_review_private_io import write_private_file


def load_memory_module():
    candidates = [
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins" / "eneo_review_tools",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools",
    ]
    for candidate in candidates:
        if (candidate / "memory_db.py").exists():
            sys.path.insert(0, str(candidate))
            import memory_db  # type: ignore

            return memory_db
    raise SystemExit("Could not locate the eneo_review_tools plugin")


memory_db = load_memory_module()


def load_learning_module():
    try:
        import eneo_review_learning
    except ModuleNotFoundError as exc:
        raise SystemExit("Could not locate the Eneo learning report module") from exc

    return eneo_review_learning


def load_coach_module():
    try:
        import eneo_review_coach
    except ModuleNotFoundError as exc:
        raise SystemExit("Could not locate the Eneo coach export module") from exc

    return eneo_review_coach


def load_replay_module():
    try:
        import eneo_review_replay
    except ModuleNotFoundError as exc:
        raise SystemExit("Could not locate the Eneo replay validator module") from exc

    return eneo_review_replay


def print_table(items):
    if not items:
        print("No findings.")
        return
    for item in items:
        marker = "SUPPRESSED" if item.get("suppressed") else "OPEN"
        line = item.get("line") or "?"
        print(
            f"{item['fingerprint'][:12]}  {marker:10}  {item['severity']:8}  "
            f"{item.get('category', '-'):15} score={item.get('publication_score', '-')}  "
            f"{item['repository']}  {item['path']}:{line}  {item['title']}"
        )
        decision = item.get("latest_decision")
        if decision:
            print(
                f"  decision={decision['decision']} actor={decision['actor']} "
                f"expires={decision.get('expires_at') or '-'} reason={decision['reason']}"
            )


def print_stats(stats):
    repo = stats.get("repository") or "(all repositories)"
    print(f"Eneo review memory - {repo}  (as of {stats['generated_at']})")
    print(f"  findings: {stats['findings_total']}  (no decision: {stats['findings_without_decision']})")
    print("  by severity:  " + ", ".join(f"{k}={v}" for k, v in stats["findings_by_severity"].items()))
    cats = ", ".join(f"{k}={v}" for k, v in stats["findings_by_category"].items() if v) or "(none)"
    print(f"  by category:  {cats}")
    decs = ", ".join(f"{k}={v}" for k, v in stats["latest_decision_by_type"].items() if v) or "(none)"
    print(f"  latest decision:  {decs}")
    print(
        f"  active suppressions: {stats['active_suppressions']} "
        f"(nearing expiry <={stats['active_suppressions_expiring_within_days']}d: "
        f"{stats['active_suppressions_nearing_expiry']})"
    )
    print(f"  repeats after a human decision (approx): {stats['repeats_after_decision_approx']}")


def print_runs(runs):
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


def print_run_stats(stats):
    repo = stats.get("repository") or "(all repositories)"
    print(f"Eneo review runs - {repo}  (last {stats['window_days']}d, as of {stats['generated_at']})")
    print("  (best-effort telemetry recorded by the reviewer; treat counts as approximate)")
    print(f"  total: {stats['total']}")
    print("  by status:  " + ", ".join(f"{k}={v}" for k, v in stats["by_status"].items()))
    print(f"  stalled (running but likely crashed): {stats['stalled_running']}")
    tta = stats["time_to_answer_seconds"]
    print(f"  time to answer (s):  p50={tta['p50']}  p95={tta['p95']}")
    print(f"  avg findings / completed run:  {stats['avg_findings_per_completed_run']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Override ENEO_REVIEW_DB for this command.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create or migrate the database schema.")

    list_parser = sub.add_parser("list", help="List recent findings.")
    list_parser.add_argument("--repo", help="Limit to owner/repository.")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--open-only", action="store_true")
    list_parser.add_argument("--json", action="store_true")

    show_parser = sub.add_parser("show", help="Show one finding and its decision history.")
    show_parser.add_argument("fingerprint")

    decide_parser = sub.add_parser("decide", help="Append a human triage decision.")
    decide_parser.add_argument("fingerprint")
    decide_parser.add_argument(
        "decision",
        choices=sorted(memory_db.DECISIONS),
    )
    decide_parser.add_argument("--reason", required=True)
    decide_parser.add_argument("--actor", required=True)
    decide_parser.add_argument("--expires-days", type=int)

    export_parser = sub.add_parser("export", help="Export findings and decisions as JSON.")
    export_parser.add_argument("--output", help="Write to a file instead of stdout.")

    stats_parser = sub.add_parser("stats", help="Summarize findings and human decisions.")
    stats_parser.add_argument("--repo", help="Limit to owner/repository.")
    stats_parser.add_argument("--expiring-within-days", type=int, default=30)
    stats_parser.add_argument("--json", action="store_true")

    runs_parser = sub.add_parser("runs", help="List recent review runs, or --stats for run metrics.")
    runs_parser.add_argument("--repo", help="Limit to owner/repository.")
    runs_parser.add_argument("--limit", type=int, default=50)
    runs_parser.add_argument(
        "--stats", action="store_true", help="Show aggregate run metrics instead of a list."
    )
    runs_parser.add_argument("--days", type=int, default=30, help="Window in days for --stats.")
    runs_parser.add_argument("--json", action="store_true")

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
        help="Replay fixture file or directory containing *.yaml fixtures.",
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

    with closing(memory_db.connect(args.db)) as connection:
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
            result = memory_db.add_decision(
                connection,
                args.fingerprint,
                args.decision,
                args.reason,
                args.actor,
                expires_days=args.expires_days,
            )
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
            if args.stats:
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
                    print_runs(runs)
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
