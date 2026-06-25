from __future__ import annotations

from contextlib import redirect_stderr
import io
import os
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import eneo_review_feedback_bridge as entrypoint  # noqa: E402


class _FailingBridge:
    def load_config(self) -> object:
        raise SystemExit("ENEO_FEEDBACK_GH_TOKEN is required")


class FeedbackBridgeEntrypointTests(unittest.TestCase):
    def test_env_presence_summary_redacts_secret_values(self) -> None:
        environment = {
            "ENEO_FEEDBACK_WEBHOOK_SECRET": "super-secret",
            "ENEO_ALLOWED_REPOSITORIES": "eneo-ai/eneo",
            "GH_TOKEN": "legacy-secret",
        }

        with patch.dict(os.environ, environment, clear=True):
            summary = entrypoint.env_presence_summary()

        self.assertIn("ENEO_FEEDBACK_WEBHOOK_SECRET=set", summary)
        self.assertIn("ENEO_FEEDBACK_GH_TOKEN=missing", summary)
        self.assertIn("GH_TOKEN=set", summary)
        self.assertNotIn("super-secret", summary)
        self.assertNotIn("legacy-secret", summary)

    def test_load_config_failure_prints_actionable_diagnostic(self) -> None:
        stderr = io.StringIO()

        with patch.dict(os.environ, {"GH_TOKEN": "legacy-secret"}, clear=True):
            with redirect_stderr(stderr):
                with self.assertRaisesRegex(SystemExit, "1"):
                    entrypoint.load_config_or_explain(
                        cast(entrypoint.FeedbackBridgeModule, _FailingBridge())
                    )

        output = stderr.getvalue()
        self.assertIn(
            "feedback bridge configuration error: ENEO_FEEDBACK_GH_TOKEN is required",
            output,
        )
        self.assertIn("ENEO_FEEDBACK_GH_TOKEN=missing", output)
        self.assertIn("GH_TOKEN=set", output)
        self.assertNotIn("legacy-secret", output)


if __name__ == "__main__":
    unittest.main()
