"""
Buffered append-only binary drive logs.

Capture thread writes packed sample records; durable flushes happen on an
interval or buffer threshold — never one fsync per Speeduino packet.
"""
from __future__ import annotations

import struct
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

from src.config import DatabaseBatchSize, DatabaseFlushIntervalSeconds

LogMagic = b"K24LOG01"
LogVersion = 1
SampleStruct = struct.Struct("<dfffffffffBBH")
# timestamp d,
# RPM MAP TPS AFR TargetAFR Coolant IAT Spark CurrentVE = 9 x f32,
# RPMBin u8, MAPBin u8, pad u16


class SessionLogWriter:
    def __init__(self, PathTarget: Path, SessionID: int) -> None:
        self.PathTarget = PathTarget
        self.SessionID = SessionID
        self.Lock = threading.RLock()
        self.Buffer = bytearray()
        self.SampleCount = 0
        self.LastFlushAt = time.monotonic()
        self.Handle: Optional[BinaryIO] = None
        self.PathTarget.parent.mkdir(parents=True, exist_ok=True)
        self.Handle = self.PathTarget.open("wb", buffering=1024 * 256)
        Header = LogMagic + struct.pack("<HId", LogVersion, SessionID, time.time())
        self.Handle.write(Header)

    def Append(self, Sample: dict[str, Any]) -> None:
        Record = SampleStruct.pack(
            float(Sample.get("Timestamp", time.time())),
            float(Sample["RPM"]),
            float(Sample["MAP"]),
            float(Sample.get("TPS") or 0.0),
            float(Sample.get("AFR") or 0.0),
            float(Sample.get("TargetAFR") or 0.0),
            float(Sample.get("CoolantCelsius") or 0.0),
            float(Sample.get("IATCelsius") or 0.0),
            float(Sample.get("SparkAdvance") or 0.0),
            float(Sample.get("CurrentVE") or 0.0),
            int(Sample.get("RPMBin") or 0) & 0xFF,
            int(Sample.get("MAPBin") or 0) & 0xFF,
            0,
        )
        with self.Lock:
            self.Buffer.extend(Record)
            self.SampleCount += 1
            if (
                len(self.Buffer) >= DatabaseBatchSize * SampleStruct.size
                or time.monotonic() - self.LastFlushAt >= DatabaseFlushIntervalSeconds
            ):
                self.Flush()

    def Flush(self) -> None:
        with self.Lock:
            if not self.Buffer or self.Handle is None:
                self.LastFlushAt = time.monotonic()
                return
            self.Handle.write(self.Buffer)
            self.Handle.flush()
            self.Buffer.clear()
            self.LastFlushAt = time.monotonic()

    def Close(self) -> None:
        with self.Lock:
            self.Flush()
            if self.Handle is not None:
                self.Handle.close()
                self.Handle = None


def IterSessionLog(PathTarget: Path) -> Iterator[dict[str, Any]]:
    Payload = PathTarget.read_bytes()
    if len(Payload) < 22 or Payload[:8] != LogMagic:
        raise ValueError("Invalid session log magic")
    Version, SessionID, StartedAt = struct.unpack_from("<HId", Payload, 8)
    if Version != LogVersion:
        raise ValueError(f"Unsupported session log version {Version}")
    Offset = 22
    while Offset + SampleStruct.size <= len(Payload):
        Values = SampleStruct.unpack_from(Payload, Offset)
        Offset += SampleStruct.size
        yield {
            "SessionID": SessionID,
            "LogStartedAt": StartedAt,
            "Timestamp": Values[0],
            "RPM": Values[1],
            "MAP": Values[2],
            "TPS": Values[3],
            "AFR": Values[4],
            "TargetAFR": Values[5],
            "CoolantCelsius": Values[6],
            "IATCelsius": Values[7],
            "SparkAdvance": Values[8],
            "CurrentVE": Values[9],
            "RPMBin": Values[10],
            "MAPBin": Values[11],
        }
