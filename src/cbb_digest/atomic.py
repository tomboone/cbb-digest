"""Whole-file writes that a crash can never leave half-done."""

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: str | bytes) -> None:
    """Write `data` to `path`, creating its directory; readers see the old file or the new one.

    Text is encoded as UTF-8; bytes are written unchanged.
    """
    raw = data.encode() if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename over it, so a crash never leaves a torn file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
