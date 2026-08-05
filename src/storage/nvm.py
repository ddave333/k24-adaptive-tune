"""
NvM-style durable binary blocks with CRC32 — cell learns and session index.

Buffered in RAM during capture; flushed on Apply / session end / shutdown.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.storage.calibration import TableSize
from src.storage.crc import AtomicWriteBytes, CRC32

LearnMagic = b"K24LRN01"
SessionMagic = b"K24SES01"
BlockVersion = 1
CellCount = TableSize * TableSize
PortNameWidth = 32
NotesWidth = 64
LogPathWidth = 128

# Per-cell learn record (packed)
# SampleCount u32, SumCorrection f32, SumWeight f32, LastVE f32,
# SuggestedVE f32, Confidence f32, UpdatedAt f64
CellLearnStruct = struct.Struct("<Ifffff d")
CellLearnSize = CellLearnStruct.size


@dataclass
class CellLearnState:
    SampleCount: int = 0
    SumCorrection: float = 0.0
    SumWeight: float = 0.0
    LastVE: float = 0.0
    SuggestedVE: float = 0.0
    Confidence: float = 0.0
    UpdatedAt: float = 0.0


@dataclass
class CellLearnBlock:
    """256-cell learn accumulator image (MAP major, then RPM)."""

    Cells: list[CellLearnState] = field(
        default_factory=lambda: [CellLearnState() for _ in range(CellCount)]
    )

    @staticmethod
    def CellIndex(MAPBin: int, RPMBin: int) -> int:
        return int(MAPBin) * TableSize + int(RPMBin)

    def Get(self, MAPBin: int, RPMBin: int) -> CellLearnState:
        return self.Cells[self.CellIndex(MAPBin, RPMBin)]

    def Upsert(
        self,
        RPMBin: int,
        MAPBin: int,
        Correction: float,
        Weight: float,
        LastVE: float,
        SuggestedVE: float,
        Confidence: float,
        SampleCountDelta: int = 1,
    ) -> None:
        Cell = self.Get(MAPBin, RPMBin)
        Cell.SampleCount += SampleCountDelta
        Cell.SumCorrection += Correction * Weight
        Cell.SumWeight += Weight
        Cell.LastVE = LastVE
        Cell.SuggestedVE = SuggestedVE
        Cell.Confidence = Confidence
        Cell.UpdatedAt = time.time()

    def AsList(self, BasemapID: int = 1) -> list[dict[str, Any]]:
        Result: list[dict[str, Any]] = []
        for MAPBin in range(TableSize):
            for RPMBin in range(TableSize):
                Cell = self.Get(MAPBin, RPMBin)
                if Cell.SampleCount <= 0 and Cell.SumWeight <= 0:
                    continue
                Result.append(
                    {
                        "CellLearnID": self.CellIndex(MAPBin, RPMBin) + 1,
                        "BasemapID": BasemapID,
                        "RPMBin": RPMBin,
                        "MAPBin": MAPBin,
                        "SampleCount": Cell.SampleCount,
                        "SumCorrection": Cell.SumCorrection,
                        "SumWeight": Cell.SumWeight,
                        "LastVE": Cell.LastVE,
                        "SuggestedVE": Cell.SuggestedVE,
                        "Confidence": Cell.Confidence,
                        "UpdatedAt": Cell.UpdatedAt,
                    }
                )
        return Result

    def Pack(self) -> bytes:
        Body = bytearray()
        Body.extend(LearnMagic)
        Body.extend(struct.pack("<H", BlockVersion))
        Body.extend(struct.pack("<H", CellCount))
        for Cell in self.Cells:
            Body.extend(
                CellLearnStruct.pack(
                    Cell.SampleCount,
                    Cell.SumCorrection,
                    Cell.SumWeight,
                    Cell.LastVE,
                    Cell.SuggestedVE,
                    Cell.Confidence,
                    Cell.UpdatedAt,
                )
            )
        Body.extend(struct.pack("<I", CRC32(bytes(Body))))
        return bytes(Body)

    @classmethod
    def Unpack(cls, Payload: bytes) -> "CellLearnBlock":
        if len(Payload) < 12 or Payload[:8] != LearnMagic:
            raise ValueError("Invalid cell-learn magic")
        StoredCRC = struct.unpack_from("<I", Payload, len(Payload) - 4)[0]
        Computed = CRC32(Payload[:-4])
        if StoredCRC != Computed:
            raise ValueError(
                f"Cell-learn CRC mismatch (stored={StoredCRC:08X} computed={Computed:08X})"
            )
        Version = struct.unpack_from("<H", Payload, 8)[0]
        Count = struct.unpack_from("<H", Payload, 10)[0]
        if Version != BlockVersion or Count != CellCount:
            raise ValueError("Unsupported cell-learn block layout")
        Offset = 12
        Cells: list[CellLearnState] = []
        for _ in range(CellCount):
            Values = CellLearnStruct.unpack_from(Payload, Offset)
            Offset += CellLearnSize
            Cells.append(
                CellLearnState(
                    SampleCount=int(Values[0]),
                    SumCorrection=float(Values[1]),
                    SumWeight=float(Values[2]),
                    LastVE=float(Values[3]),
                    SuggestedVE=float(Values[4]),
                    Confidence=float(Values[5]),
                    UpdatedAt=float(Values[6]),
                )
            )
        return cls(Cells=Cells)

    def Save(self, PathTarget: Path) -> None:
        AtomicWriteBytes(PathTarget, self.Pack())

    @classmethod
    def Load(cls, PathTarget: Path) -> "CellLearnBlock":
        if not PathTarget.exists():
            return cls()
        return cls.Unpack(PathTarget.read_bytes())


@dataclass
class SessionRecord:
    SessionID: int
    StartedAt: float
    EndedAt: float = 0.0
    PortName: str = ""
    Notes: str = ""
    SampleCount: int = 0
    LogPath: str = ""


SessionRecordStruct = struct.Struct(
    f"<I dd {PortNameWidth}s {NotesWidth}s I {LogPathWidth}s"
)


@dataclass
class SessionIndex:
    Sessions: list[SessionRecord] = field(default_factory=list)
    NextSessionID: int = 1

    def Pack(self) -> bytes:
        Body = bytearray()
        Body.extend(SessionMagic)
        Body.extend(struct.pack("<H", BlockVersion))
        Body.extend(struct.pack("<I", self.NextSessionID))
        Body.extend(struct.pack("<I", len(self.Sessions)))
        for Session in self.Sessions:
            Body.extend(
                SessionRecordStruct.pack(
                    Session.SessionID,
                    Session.StartedAt,
                    Session.EndedAt,
                    Session.PortName.encode("utf-8")[: PortNameWidth - 1].ljust(
                        PortNameWidth, b"\0"
                    ),
                    Session.Notes.encode("utf-8")[: NotesWidth - 1].ljust(
                        NotesWidth, b"\0"
                    ),
                    Session.SampleCount,
                    Session.LogPath.encode("utf-8")[: LogPathWidth - 1].ljust(
                        LogPathWidth, b"\0"
                    ),
                )
            )
        Body.extend(struct.pack("<I", CRC32(bytes(Body))))
        return bytes(Body)

    @classmethod
    def Unpack(cls, Payload: bytes) -> "SessionIndex":
        if len(Payload) < 18 or Payload[:8] != SessionMagic:
            raise ValueError("Invalid session-index magic")
        StoredCRC = struct.unpack_from("<I", Payload, len(Payload) - 4)[0]
        Computed = CRC32(Payload[:-4])
        if StoredCRC != Computed:
            raise ValueError(
                f"Session-index CRC mismatch (stored={StoredCRC:08X} computed={Computed:08X})"
            )
        Version = struct.unpack_from("<H", Payload, 8)[0]
        NextID = struct.unpack_from("<I", Payload, 10)[0]
        Count = struct.unpack_from("<I", Payload, 14)[0]
        if Version != BlockVersion:
            raise ValueError(f"Unsupported session-index version {Version}")
        Offset = 18
        Sessions: list[SessionRecord] = []
        for _ in range(Count):
            Values = SessionRecordStruct.unpack_from(Payload, Offset)
            Offset += SessionRecordStruct.size
            Sessions.append(
                SessionRecord(
                    SessionID=int(Values[0]),
                    StartedAt=float(Values[1]),
                    EndedAt=float(Values[2]),
                    PortName=Values[3].split(b"\0", 1)[0].decode("utf-8", "replace"),
                    Notes=Values[4].split(b"\0", 1)[0].decode("utf-8", "replace"),
                    SampleCount=int(Values[5]),
                    LogPath=Values[6].split(b"\0", 1)[0].decode("utf-8", "replace"),
                )
            )
        return cls(Sessions=Sessions, NextSessionID=NextID)

    def Save(self, PathTarget: Path) -> None:
        AtomicWriteBytes(PathTarget, self.Pack())

    @classmethod
    def Load(cls, PathTarget: Path) -> "SessionIndex":
        if not PathTarget.exists():
            return cls()
        return cls.Unpack(PathTarget.read_bytes())

    def Find(self, SessionID: int) -> Optional[SessionRecord]:
        for Session in self.Sessions:
            if Session.SessionID == SessionID:
                return Session
        return None

    def AsList(self, Limit: int = 50) -> list[dict[str, Any]]:
        Ordered = sorted(self.Sessions, key=lambda Item: Item.SessionID, reverse=True)
        Result: list[dict[str, Any]] = []
        for Session in Ordered[:Limit]:
            Result.append(
                {
                    "SessionID": Session.SessionID,
                    "StartedAt": Session.StartedAt,
                    "EndedAt": Session.EndedAt if Session.EndedAt > 0 else None,
                    "PortName": Session.PortName,
                    "Notes": Session.Notes,
                    "SampleCount": Session.SampleCount,
                    "LogPath": Session.LogPath,
                }
            )
        return Result
