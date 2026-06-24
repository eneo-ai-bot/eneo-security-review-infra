#!/usr/bin/env python3
"""Operator status snapshot for the Eneo AI reviewer.

Read-only and side-effect-free: it only queries GitHub (via `gh`) and the public
health endpoint. No secrets, no writes, nothing that can affect the live reviewer.

  python3 scripts/review_status.py

Shows: gateway health, recent /review triggers (who / which PR / dispatched vs
ignored), and where to drill in (per-run logs + the findings registry).
"""
from __future__ import annotations

import json
import subprocess
import urllib.request

REPO = "eneo-ai/eneo"
WORKFLOW_FILE = "ai-review-request.yml"
HEALTH_URL = "https://eneo-security.sundsvall.dev/health"


def _health() -> str:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as response:
            body = response.read(200).decode("utf-8", "replace").strip()
            return f"OK ({response.status})  {body}"
    except Exception as exc:  # noqa: BLE001 - operator tool, report any failure
        return f"UNREACHABLE: {exc}"


def _recent_runs():
    result = subprocess.run(
        ["gh", "api", f"repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs?per_page=15"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return json.loads(result.stdout or "{}").get("workflow_runs", []), None


_MARK = {"success": "REVIEW DISPATCHED", "skipped": "ignored (not exact /review / not allowed)"}


def main() -> int:
    print("=== Eneo AI Reviewer — status ===\n")
    print(f"Gateway: {_health()}")

    runs, error = _recent_runs()
    if error is not None:
        print(f"\n/review triggers: could not query GitHub Actions ({error})")
        return 1

    runs = runs or []
    dispatched = sum(1 for r in runs if r.get("conclusion") == "success")
    ignored = sum(1 for r in runs if r.get("conclusion") == "skipped")
    print(
        f"\n/review triggers (last {len(runs)}): "
        f"{dispatched} dispatched a review, {ignored} ignored\n"
    )
    for run in runs[:12]:
        when = str(run.get("created_at", ""))[:16].replace("T", " ")
        who = (run.get("triggering_actor") or {}).get("login", "?")
        outcome = run.get("conclusion") or run.get("status") or "?"
        mark = _MARK.get(outcome, outcome)
        title = str(run.get("display_title", ""))[:42]
        print(f"  {when}  {who:18.18}  {mark:42.42}  {title}")

    print("\nDrill in:")
    print(f"  why a run was ignored:   gh run view <id> --repo {REPO}")
    print( "  live gateway activity:   Dokploy -> Eneo-Security-Review -> hermes-review -> Logs")
    print( "  findings totals:         eneo-review-memory stats --repo eneo-ai/eneo   (run in the container)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
