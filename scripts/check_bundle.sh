#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

python3 -m compileall -q "$ROOT/bootstrap" "$ROOT/tools" "$ROOT/scripts" "$ROOT/tests"
if ! command -v pyright >/dev/null 2>&1; then
  printf '%s\n' "pyright is required for bundle checks. Install pyright and rerun." >&2
  exit 1
fi
pyright -p "$ROOT"
PYTHONPATH="$ROOT/bootstrap/plugins" python3 -m unittest discover -s "$ROOT/tests" -v

python3 - "$ROOT" <<'PY'
from pathlib import Path
import shutil
import subprocess
import sys

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

root = Path(sys.argv[1])
for relative in [
    "compose.yaml",
    "bootstrap/config.yaml",
    "bootstrap/plugins/eneo_review_tools/plugin.yaml",
    "examples/github/ai-review-request.yml",
]:
    path = root / relative
    if yaml is not None:
        with path.open(encoding="utf-8") as handle:
            value = yaml.safe_load(handle)
        if not isinstance(value, dict):
            raise SystemExit(f"{relative} did not parse to a YAML mapping")
    else:
        ruby = shutil.which("ruby")
        if not ruby:
            raise SystemExit("PyYAML is not installed and ruby is unavailable for YAML validation")
        subprocess.run(
            [
                ruby,
                "-ryaml",
                "-e",
                "v = YAML.safe_load(File.read(ARGV[0]), aliases: true); exit(v.is_a?(Hash) ? 0 : 1)",
                str(path),
            ],
            check=True,
        )
    print(f"YAML OK: {relative}")
PY

printf '%s\n' "Bundle checks passed."
