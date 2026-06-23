#!/usr/bin/env python3
"""Human administration for the Eneo review-memory SQLite database."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


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

    args = parser.parse_args()
    with memory_db.connect(args.db) as connection:
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
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
