#!/usr/bin/env python3
"""Send a signed local/test /review payload to the Hermes webhook."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Full /webhooks/eneo-review URL")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--requester", default="smoke-test")
    args = parser.parse_args()

    payload = {
        "repository": {"full_name": args.repo},
        "pull_request": {"number": args.pr},
        "requester": {"login": args.requester, "association": "OWNER"},
        "request": {"comment_id": 0},
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = "sha256=" + hmac.new(args.secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": secrets.token_hex(16),
            "X-Hub-Signature-256": signature,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(response.read().decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
