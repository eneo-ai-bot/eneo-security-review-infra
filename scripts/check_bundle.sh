#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

python3 -m compileall -q "$ROOT/bootstrap" "$ROOT/tools" "$ROOT/scripts" "$ROOT/tests"
PYTHONPATH="$ROOT/bootstrap/plugins" python3 -m unittest discover -s "$ROOT/tests" -v

python3 - "$ROOT" <<'PY'
from pathlib import Path
import sys
import yaml

root = Path(sys.argv[1])
for relative in [
    "compose.yaml",
    "bootstrap/config.yaml",
    "bootstrap/plugins/eneo_review_tools/plugin.yaml",
    "examples/github/ai-review-request.yml",
]:
    path = root / relative
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"{relative} did not parse to a YAML mapping")
    print(f"YAML OK: {relative}")
PY

printf '%s\n' "Bundle checks passed."
