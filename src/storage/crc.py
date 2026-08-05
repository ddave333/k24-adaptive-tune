"""CRC32 helpers and atomic NvM-style file replace."""
from __future__ import annotations

import binascii
import os
import tempfile
from pathlib import Path


def CRC32(Data: bytes) -> int:
    return binascii.crc32(Data) & 0xFFFFFFFF


def AtomicWriteBytes(PathTarget: Path, Payload: bytes) -> None:
    """Write to a temp file in the same directory, then replace atomically."""
    PathTarget.parent.mkdir(parents=True, exist_ok=True)
    FD, TempName = tempfile.mkstemp(
        prefix=f".{PathTarget.name}.",
        suffix=".tmp",
        dir=str(PathTarget.parent),
    )
    try:
        with os.fdopen(FD, "wb") as Handle:
            Handle.write(Payload)
            Handle.flush()
            os.fsync(Handle.fileno())
        os.replace(TempName, PathTarget)
    except Exception:
        try:
            os.unlink(TempName)
        except OSError:
            pass
        raise
