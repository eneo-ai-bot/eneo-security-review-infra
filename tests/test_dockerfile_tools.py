from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from fnmatch import fnmatch
import io
import importlib
import importlib.util
from pathlib import Path
import types
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_install_module():
    spec = importlib.util.spec_from_file_location(
        "eneo_bootstrap_install", ROOT / "bootstrap" / "install.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load bootstrap installer")
    module = importlib.util.module_from_spec(spec)
    previous_yaml = sys.modules.get("yaml")
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_load = lambda _text: {}
    yaml_stub.safe_dump = lambda *_args, **_kwargs: ""
    sys.modules["yaml"] = yaml_stub
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_yaml is None:
            sys.modules.pop("yaml", None)
        else:
            sys.modules["yaml"] = previous_yaml
    return module


def _docker_copy_sources() -> list[str]:
    sources: list[str] = []
    for raw_line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("COPY "):
            continue
        parts = shlex.split(line)
        index = 1
        while index < len(parts) and parts[index].startswith("--"):
            index += 1
        sources.extend(parts[index:-1])
    return sources


def _copy_source_covers(source: str, relative_path: str) -> bool:
    if "*" in source:
        return fnmatch(relative_path, source)
    if source.endswith("/"):
        return relative_path.startswith(source)
    return relative_path == source


class DockerfileToolsTests(unittest.TestCase):
    def tearDown(self) -> None:
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
            "eneo_review_memory",
        ):
            sys.modules.pop(name, None)

    def test_container_installs_every_review_memory_runtime_module(self) -> None:
        sources = _docker_copy_sources()
        modules = [
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "tools").glob("eneo_review_*.py"))
        ]

        missing = [
            module
            for module in modules
            if not any(_copy_source_covers(source, module) for source in sources)
        ]

        self.assertEqual([], missing)

    def test_memory_cli_keeps_stable_operator_command_name(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn(
            "cp /usr/local/bin/eneo_review_memory.py /usr/local/bin/eneo-review-memory",
            dockerfile,
        )
        self.assertIn(
            "cp /usr/local/bin/eneo_review_feedback_bridge.py /usr/local/bin/eneo-review-feedback-bridge",
            dockerfile,
        )

    def test_docker_build_context_excludes_python_bytecode(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn("**/__pycache__/", dockerignore)
        self.assertIn("**/*.py[cod]", dockerignore)

    def test_installer_replaces_managed_trees_and_ignores_bytecode(self) -> None:
        install = _load_install_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            (source / "__pycache__").mkdir(parents=True)
            (source / "memory_schema.py").write_text("SCHEMA_VERSION = 8\n", encoding="utf-8")
            (source / "__pycache__" / "memory_schema.cpython-313.pyc").write_bytes(b"stale")
            (target / "__pycache__").mkdir(parents=True)
            (target / "old_module.py").write_text("SCHEMA_VERSION = 6\n", encoding="utf-8")
            (target / "__pycache__" / "old_module.cpython-313.pyc").write_bytes(b"old")

            install.copy_managed_tree(source, target)

            self.assertEqual(
                "SCHEMA_VERSION = 8\n",
                (target / "memory_schema.py").read_text(encoding="utf-8"),
            )
            self.assertFalse((target / "old_module.py").exists())
            self.assertFalse((target / "__pycache__").exists())

    def test_installed_memory_cli_imports_support_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_dir = Path(temp)
            for module in (ROOT / "tools").glob("eneo_review_*.py"):
                shutil.copy2(module, install_dir / module.name)
            shutil.copy2(
                install_dir / "eneo_review_memory.py",
                install_dir / "eneo-review-memory",
            )
            shutil.copy2(
                install_dir / "eneo_review_feedback_bridge.py",
                install_dir / "eneo-review-feedback-bridge",
            )

            completed = subprocess.run(
                [sys.executable, str(install_dir / "eneo-review-memory"), "--help"],
                check=False,
                capture_output=True,
                text=True,
            )
            bridge_completed = subprocess.run(
                [
                    sys.executable,
                    str(install_dir / "eneo-review-feedback-bridge"),
                    "--help",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={"PYTHONPATH": str(ROOT / "bootstrap" / "plugins")},
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(0, bridge_completed.returncode, bridge_completed.stderr)

    def test_memory_cli_can_discover_image_bootstrap_plugin(self) -> None:
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import eneo_review_memory

            with mock.patch.dict("os.environ", {}, clear=True):
                candidates = eneo_review_memory.memory_module_candidates()
        finally:
            sys.path.remove(str(ROOT / "tools"))

        self.assertIn(
            Path("/opt/eneo-bootstrap/plugins/eneo_review_tools"),
            candidates,
        )

    def test_memory_cli_prefers_image_plugin_and_evicts_stale_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stale = root / "stale"
            fresh = root / "fresh"
            stale.mkdir()
            fresh.mkdir()
            (stale / "memory_db.py").write_text("MARKER = 'stale'\n", encoding="utf-8")
            (fresh / "memory_db.py").write_text(
                "MARKER = 'fresh'\n"
                "class ReviewMemoryError(Exception): pass\n"
                "def connect(explicit=None): return None\n"
                "def connect_existing(explicit=None): return None\n",
                encoding="utf-8",
            )

            sys.path.insert(0, str(stale))
            try:
                stale_module = importlib.import_module("memory_db")
                self.assertEqual(stale_module.MARKER, "stale")
            finally:
                sys.path.remove(str(stale))

            sys.path.insert(0, str(ROOT / "tools"))
            try:
                import eneo_review_memory

                stderr = io.StringIO()
                with mock.patch.object(
                    eneo_review_memory,
                    "memory_module_candidates",
                    return_value=(fresh, stale),
                ):
                    with redirect_stderr(stderr):
                        loaded = eneo_review_memory.load_memory_module()
            finally:
                sys.path.remove(str(ROOT / "tools"))

        self.assertEqual(getattr(loaded, "MARKER"), "fresh")
        self.assertTrue(hasattr(loaded, "connect_existing"))
        self.assertIn(str(fresh), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
