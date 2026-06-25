from __future__ import annotations

from contextlib import redirect_stderr
import importlib
import io
import os
import sys
import tempfile
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
    def tearDown(self) -> None:
        for name in ("eneo_review_tools.feedback_bridge", "eneo_review_tools"):
            sys.modules.pop(name, None)

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

    def test_loader_prefers_image_plugin_and_evicts_stale_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stale_parent = root / "stale"
            fresh_parent = root / "fresh"
            for parent, marker in [(stale_parent, "stale"), (fresh_parent, "fresh")]:
                package = parent / "eneo_review_tools"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("", encoding="utf-8")
                (package / "feedback_bridge.py").write_text(
                    f"MARKER = {marker!r}\n"
                    "DEFAULT_PATH = '/webhooks/eneo-review-feedback'\n"
                    "DEFAULT_PORT = 8645\n"
                    "MAX_BODY_BYTES = 65536\n"
                    "class BridgeError(Exception): pass\n"
                    "class GitHubError(Exception): pass\n"
                    "class GitHubNotFound(Exception): pass\n"
                    "class UnauthorizedFeedback(Exception): pass\n",
                    encoding="utf-8",
                )

            sys.path.insert(0, str(stale_parent))
            try:
                stale = importlib.import_module("eneo_review_tools.feedback_bridge")
                self.assertEqual(stale.MARKER, "stale")
            finally:
                sys.path.remove(str(stale_parent))

            stderr = io.StringIO()
            with patch.object(
                entrypoint,
                "plugin_parent_candidates",
                return_value=(fresh_parent, stale_parent),
            ):
                with redirect_stderr(stderr):
                    loaded = entrypoint.load_feedback_bridge()

        self.assertEqual(getattr(loaded, "MARKER"), "fresh")
        self.assertIn(str(fresh_parent), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
