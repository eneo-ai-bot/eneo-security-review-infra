#!/usr/bin/env python3
"""Idempotently install the Eneo review profile into HERMES_HOME."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # Hermes ships PyYAML; fail clearly on unexpected images.
    raise SystemExit("PyYAML is required in the Hermes image") from exc

SOURCE = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data")).resolve()


def deep_merge(existing: Any, managed: Any) -> Any:
    """Merge dictionaries recursively, with managed values taking precedence."""
    if isinstance(existing, dict) and isinstance(managed, dict):
        result = dict(existing)
        for key, value in managed.items():
            result[key] = deep_merge(result.get(key), value) if key in result else value
        return result
    return managed


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def copy_managed_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preserve-soul",
        action="store_true",
        help="Do not replace an existing SOUL.md.",
    )
    parser.add_argument(
        "--force-agents",
        action="store_true",
        help="Replace an existing workspace/AGENTS.md instead of preserving local edits.",
    )
    parser.add_argument(
        "--skip-plugin-enable",
        action="store_true",
        help="Copy the plugin but do not run hermes plugins enable.",
    )
    args = parser.parse_args()

    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    (HERMES_HOME / "workspace").mkdir(parents=True, exist_ok=True)
    (HERMES_HOME / "review-memory").mkdir(parents=True, exist_ok=True)

    # Merge the managed security and webhook settings without deleting model/provider
    # settings written by `hermes model`.
    config_path = HERMES_HOME / "config.yaml"
    existing = load_yaml(config_path)
    managed = load_yaml(SOURCE / "config.yaml")
    merged = deep_merge(existing, managed)
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_suffix(".yaml.before-eneo"))
    atomic_write(config_path, yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))

    soul_target = HERMES_HOME / "SOUL.md"
    if not soul_target.exists() or not args.preserve_soul:
        if soul_target.exists():
            shutil.copy2(soul_target, HERMES_HOME / "SOUL.md.before-eneo")
        shutil.copy2(SOURCE / "SOUL.md", soul_target)

    agents_target = HERMES_HOME / "workspace" / "AGENTS.md"
    if not agents_target.exists() or args.force_agents:
        if agents_target.exists():
            shutil.copy2(agents_target, agents_target.with_suffix(".md.before-eneo"))
        shutil.copy2(SOURCE / "workspace" / "AGENTS.md", agents_target)

    copy_managed_tree(
        SOURCE / "skills" / "eneo-pr-review", HERMES_HOME / "skills" / "eneo-pr-review"
    )
    copy_managed_tree(SOURCE / "skills" / "ponytail", HERMES_HOME / "skills" / "ponytail")
    copy_managed_tree(
        SOURCE / "plugins" / "eneo_review_tools",
        HERMES_HOME / "plugins" / "eneo_review_tools",
    )

    # Prevent future bundled-skill seeding. Existing bundled skills are not deleted.
    (HERMES_HOME / ".no-bundled-skills").touch(exist_ok=True)

    plugin_dir = HERMES_HOME / "plugins" / "eneo_review_tools"
    sys.path.insert(0, str(plugin_dir))
    import memory_db  # type: ignore

    with closing(memory_db.connect()):
        pass

    if not args.skip_plugin_enable:
        result = subprocess.run(
            ["hermes", "plugins", "enable", "eneo-review-tools"],
            check=False,
            text=True,
            capture_output=True,
            env={**os.environ, "HERMES_HOME": str(HERMES_HOME)},
        )
        if result.returncode != 0:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            print(
                "Plugin files were installed, but automatic enablement failed. "
                "Run: hermes plugins enable eneo-review-tools",
                file=sys.stderr,
            )
            return result.returncode

    print(f"Installed Eneo reviewer into {HERMES_HOME}")
    print("Next: run `hermes model`, choose OpenAI Codex, then restart the gateway.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
