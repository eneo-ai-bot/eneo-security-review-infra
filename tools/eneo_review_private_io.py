"""Private atomic file output helpers for operator review-memory artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private_file(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_symlink():
        raise ValueError(f"refusing to write through symlink: {destination}")
    fd, raw_temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
