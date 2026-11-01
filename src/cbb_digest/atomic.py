"""Whole-file writes that a crash can never leave half-done."""

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path`, creating its directory; readers see the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename over it, so a crash never leaves a torn file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
