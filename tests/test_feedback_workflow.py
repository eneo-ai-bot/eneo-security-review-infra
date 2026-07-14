from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def workflow_source() -> str:
    return (ROOT / "examples/github/ai-review-request.yml").read_text(encoding="utf-8")


def dispatch_script(workflow: str) -> str:
    marker = "        run: |\n"
    start = workflow.index(marker) + len(marker)
    lines: list[str] = []
    for line in workflow[start:].splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


class TimeoutResponse:
    status = 200

    def __enter__(self) -> TimeoutResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        del size
        raise TimeoutError


class TimeoutOpener:
    def open(self, request: object, *, timeout: int) -> TimeoutResponse:
        del request, timeout
        return TimeoutResponse()


class FeedbackWorkflowTests(unittest.TestCase):
    def test_checked_in_dispatch_script_is_valid_python(self) -> None:
        workflow = workflow_source()

        self.assertIn("Contract: eneo-ai-review-trigger/v3.1", workflow)
        compile(dispatch_script(workflow), "ai-review-request.yml", "exec")

    def test_workflow_routes_exact_review_and_feedback_commands(self) -> None:
        workflow = workflow_source()

        self.assertIn('r"^[@/]review(?:\\s+(?P<rest>\\S.*))?$"', workflow)
        self.assertIn('return "feedback" if match.group("rest") else "review"', workflow)
        self.assertIn('delivery_id = f"{event[\'comment\'][\'id\']}:feedback"', workflow)
        self.assertIn('event["comment"]["user"].get("type") == "Bot"', workflow)
        self.assertIn(
            'ignore("comment does not match the exact Eneo review command grammar")',
            workflow,
        )
        self.assertNotIn("trigger!r", workflow)
        self.assertNotIn("re.IGNORECASE", workflow)

    def test_dispatch_is_bounded_and_does_not_follow_redirects(self) -> None:
        workflow = workflow_source()

        self.assertIn("timeout-minutes: 5", workflow)
        self.assertIn("NoRedirectHandler", workflow)
        self.assertIn("opener.open(request, timeout=60)", workflow)
        self.assertIn('parsed_url.scheme != "https"', workflow)
        self.assertNotIn("timeout=900", workflow)
        self.assertNotIn("response.status} {text}", workflow)
        self.assertNotIn("error.code} {detail}", workflow)
        self.assertNotIn("error.read(", workflow)

    def test_response_read_timeout_uses_sanitized_reachability_error(self) -> None:
        event = {
            "comment": {
                "id": 123,
                "body": "/review",
                "author_association": "OWNER",
                "user": {"login": "alice", "type": "User"},
            },
            "repository": {"full_name": "eneo/platform"},
            "issue": {"number": 17},
        }
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            output_path = Path(directory) / "output"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            environment = {
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_OUTPUT": str(output_path),
                "ALLOWED_USERS": "alice",
                "HERMES_REVIEW_URL": "https://review.example.invalid/hook",
                "HERMES_WEBHOOK_SECRET": "test-secret",
            }
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch(
                    "urllib.request.build_opener", return_value=TimeoutOpener()
                ),
                self.assertRaises(SystemExit) as raised,
            ):
                exec(
                    compile(
                        dispatch_script(workflow_source()),
                        "ai-review-request.yml",
                        "exec",
                    ),
                    {"__name__": "__main__"},
                )

        self.assertEqual(
            str(raised.exception), "Eneo review webhook could not be reached"
        )

    def test_secrets_are_scoped_to_dispatch_and_acknowledgement_is_non_blocking(self) -> None:
        workflow = workflow_source()
        dispatch_start = workflow.index("- name: Authorize requester")
        reaction_start = workflow.index("- name: Acknowledge receipt")
        dispatch = workflow[dispatch_start:reaction_start]
        reaction = workflow[reaction_start:]

        for variable in (
            "HERMES_REVIEW_URL",
            "HERMES_WEBHOOK_SECRET",
            "HERMES_REVIEW_FEEDBACK_URL",
            "HERMES_REVIEW_FEEDBACK_SECRET",
            "ALLOWED_USERS",
        ):
            self.assertIn(variable, dispatch)
            self.assertNotIn(variable, reaction)

        self.assertIn("issues: write", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertIn('output.write(f"mode={mode}\\n")', dispatch)
        self.assertIn("steps.dispatch.outputs.dispatched == 'true'", reaction)
        self.assertIn("steps.dispatch.outputs.mode == 'review'", reaction)
        self.assertNotIn("continue-on-error: true", reaction)
        self.assertIn("if ! gh api --silent", reaction)
        self.assertIn("::warning::Review request was accepted", reaction)
        self.assertIn(">/dev/null 2>&1", reaction)


if __name__ == "__main__":
    unittest.main()
