#!/usr/bin/env python3
"""HTTP entrypoint for the deterministic Eneo review-feedback bridge."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from types import ModuleType
from typing import Protocol, cast

REQUEST_TIMEOUT_SECONDS = 10.0
MAX_CONCURRENT_REQUESTS = 8


class FeedbackBridgeModule(Protocol):
    DEFAULT_PATH: str
    DEFAULT_PORT: int
    MAX_BODY_BYTES: int
    BridgeError: type[Exception]
    GitHubError: type[Exception]
    GitHubNotFound: type[Exception]
    UnauthorizedFeedback: type[Exception]

    def load_config(self) -> object: ...
    def ready_check(self, config: object) -> Mapping[str, object]: ...
    def verify_signature(self, body: bytes, signature: str, secret: str) -> bool: ...
    def decode_request_body(self, body: bytes) -> object: ...
    def process_feedback(
        self,
        *,
        payload: object,
        config: object,
        github: object,
    ) -> "BridgeResponseLike": ...
    def response_body(self, status: str, message: str = "") -> bytes: ...


class BridgeResponseLike(Protocol):
    def to_json(self) -> bytes: ...


class GitHubClientClass(Protocol):
    def __call__(self, token: str) -> object: ...


class ConfigLike(Protocol):
    secret: str
    token: str


def _import_module(name: str) -> ModuleType:
    return importlib.import_module(name)


def _insert_plugin_parent() -> None:
    candidates = [
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins",
        Path("/opt/eneo-bootstrap/plugins"),
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    ]
    candidates.extend(Path(entry) for entry in sys.path if entry)
    for candidate in candidates:
        if (candidate / "eneo_review_tools" / "feedback_bridge.py").exists():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("Could not locate the eneo_review_tools plugin")


def load_feedback_bridge() -> FeedbackBridgeModule:
    _insert_plugin_parent()
    return cast(FeedbackBridgeModule, _import_module("eneo_review_tools.feedback_bridge"))


class FeedbackRequestHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        server = cast(FeedbackServer, self.server)
        self.connection.settimeout(server.request_timeout_seconds)

    def do_GET(self) -> None:
        bridge, config, _ = self._state()
        if self.path == "/health":
            self._write(200, b'{"status":"ok"}')
            return
        if self.path == "/ready":
            try:
                self._write(200, _json_body(bridge.ready_check(config)))
            except Exception as exc:
                self._write(503, bridge.response_body("not_ready", str(exc)))
            return
        self._write(404, bridge.response_body("not_found"))

    def do_POST(self) -> None:
        server = cast(FeedbackServer, self.server)
        if not server.acquire_request_slot():
            bridge, _, _ = self._state()
            self._write(503, bridge.response_body("busy"))
            return
        try:
            self._do_POST()
        finally:
            server.release_request_slot()

    def _do_POST(self) -> None:
        bridge, config, github = self._state()
        if self.path != bridge.DEFAULT_PATH:
            self._write(404, bridge.response_body("not_found"))
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._write(411, bridge.response_body("missing_length"))
            return
        if length < 0:
            self._write(411, bridge.response_body("missing_length"))
            return
        if length > bridge.MAX_BODY_BYTES:
            self._write(413, bridge.response_body("payload_too_large"))
            return
        body = self.rfile.read(length)
        signature = self.headers.get("X-Hub-Signature-256", "")
        if not bridge.verify_signature(body, signature, config.secret):
            self._write(401, bridge.response_body("bad_signature"))
            return
        if self.headers.get("X-GitHub-Event", "") != "issue_comment":
            self._write(400, bridge.response_body("unsupported_event"))
            return
        try:
            response = bridge.process_feedback(
                payload=bridge.decode_request_body(body),
                config=config,
                github=github,
            )
            self._write(200, response.to_json())
        except bridge.UnauthorizedFeedback:
            self._write(200, bridge.response_body("unauthorized"))
        except bridge.GitHubNotFound as exc:
            self._write(200, bridge.response_body("not_found", str(exc)))
        except bridge.GitHubError as exc:
            self._write(502, bridge.response_body("github_error", str(exc)))
        except bridge.BridgeError as exc:
            self._write(400, bridge.response_body("bad_request", str(exc)))

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, file=sys.stderr)

    def _state(self) -> tuple[FeedbackBridgeModule, ConfigLike, object]:
        server = cast(FeedbackServer, self.server)
        return server.bridge, server.config, server.github

    def _write(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FeedbackServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        bridge: FeedbackBridgeModule,
        config: ConfigLike,
        github: object,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
    ) -> None:
        super().__init__(server_address, FeedbackRequestHandler)
        self.bridge = bridge
        self.config = config
        self.github = github
        self.request_timeout_seconds = request_timeout_seconds
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)

    def acquire_request_slot(self) -> bool:
        return self._request_slots.acquire(blocking=False)

    def release_request_slot(self) -> None:
        self._request_slots.release()


def serve(host: str, port: int, bridge: FeedbackBridgeModule | None = None) -> None:
    bridge = bridge or load_feedback_bridge()
    config = cast(ConfigLike, bridge.load_config())
    github_client_class = cast(
        GitHubClientClass,
        getattr(bridge, "GitHubApiClient"),
    )
    server = FeedbackServer(
        (host, port),
        bridge=bridge,
        config=config,
        github=github_client_class(config.token),
    )
    print(f"Eneo review feedback bridge listening on {host}:{port}", flush=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    bridge = load_feedback_bridge()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "verify-config"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=bridge.DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.command == "verify-config":
        config = bridge.load_config()
        bridge.ready_check(config)
        print("ok")
        return 0
    serve(str(args.host), int(args.port), bridge)
    return 0


def _json_body(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
