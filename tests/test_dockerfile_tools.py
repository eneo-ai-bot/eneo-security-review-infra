from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from fnmatch import fnmatch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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

    def test_installed_memory_cli_imports_support_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_dir = Path(temp)
            for module in (ROOT / "tools").glob("eneo_review_*.py"):
                shutil.copy2(module, install_dir / module.name)
            shutil.copy2(
                install_dir / "eneo_review_memory.py",
                install_dir / "eneo-review-memory",
            )

            completed = subprocess.run(
                [sys.executable, str(install_dir / "eneo-review-memory"), "--help"],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
