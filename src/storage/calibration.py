"""
ROM-style calibration image: axes + VE/Spark/AFR lookup tables with CRC32.

Matches the ECU mental model — O(1) indexed cells, not a database rowset.
Speeduino still receives these tables as raw page bytes over serial.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.config import (
    DefaultLearnRate,
    DefaultMAPBins,
    DefaultRPMBins,
)
from src.storage.crc import AtomicWriteBytes, CRC32

CalMagic = b"K24CAL01"
CalVersion = 1
TableSize = 16
NameWidth = 64

# Header + name + times + learn + axes + three float32 tables + CRC
# Layout is fixed so firmware-minded readers can mmap the image.


def DefaultVETable(Rows: int = TableSize, Cols: int = TableSize) -> list[list[float]]:
    Table: list[list[float]] = []
    for Row in range(Rows):
        Line: list[float] = []
        MAPFactor = 0.55 + (Row / max(Rows - 1, 1)) * 0.45
        for Col in range(Cols):
            RPMFactor = 0.75 + (Col / max(Cols - 1, 1)) * 0.35
            Peak = 1.0 - abs((Col / max(Cols - 1, 1)) - 0.55) * 0.25
            Value = 100.0 * MAPFactor * RPMFactor * Peak
            Line.append(round(min(max(Value, 35.0), 110.0), 1))
        Table.append(Line)
    return Table


def DefaultSparkTable(Rows: int = TableSize, Cols: int = TableSize) -> list[list[float]]:
    Table: list[list[float]] = []
    for Row in range(Rows):
        Line: list[float] = []
        LoadPull = (Row / max(Rows - 1, 1)) * 12.0
        for Col in range(Cols):
            Base = 10.0 + (Col / max(Cols - 1, 1)) * 28.0
            Advance = Base - LoadPull
            Line.append(round(min(max(Advance, 5.0), 42.0), 1))
        Table.append(Line)
    return Table


def DefaultAFRTable(Rows: int = TableSize, Cols: int = TableSize) -> list[list[float]]:
    Table: list[list[float]] = []
    for Row in range(Rows):
        Line: list[float] = []
        Load = Row / max(Rows - 1, 1)
        for Col in range(Cols):
            RPM = Col / max(Cols - 1, 1)
            if Load > 0.75 or RPM > 0.85:
                Target = 12.8
            elif Load > 0.55:
                Target = 13.5
            elif Load < 0.25 and RPM < 0.35:
                Target = 14.2
            else:
                Target = 14.7
            Line.append(Target)
        Table.append(Line)
    return Table


def NearestBin(Value: float, Bins: list[float]) -> int:
    BestIndex = 0
    BestDistance = abs(Value - Bins[0])
    for Index in range(1, len(Bins)):
        Distance = abs(Value - Bins[Index])
        if Distance < BestDistance:
            BestDistance = Distance
            BestIndex = Index
    return BestIndex


def Flatten(Table: list[list[float]]) -> list[float]:
    Flat: list[float] = []
    for Row in Table:
        Flat.extend(float(Value) for Value in Row)
    return Flat


def Unflatten(Flat: list[float], Rows: int = TableSize, Cols: int = TableSize) -> list[list[float]]:
    return [list(Flat[Row * Cols : (Row + 1) * Cols]) for Row in range(Rows)]


@dataclass
class CalibrationImage:
    Name: str = "K24 Seed Basemap"
    BasemapID: int = 1
    CreatedAt: float = field(default_factory=time.time)
    UpdatedAt: float = field(default_factory=time.time)
    LearnRate: float = DefaultLearnRate
    RPMBins: list[float] = field(default_factory=lambda: list(DefaultRPMBins))
    MAPBins: list[float] = field(default_factory=lambda: list(DefaultMAPBins))
    VETable: list[list[float]] = field(default_factory=DefaultVETable)
    SparkTable: list[list[float]] = field(default_factory=DefaultSparkTable)
    AFRTable: list[list[float]] = field(default_factory=DefaultAFRTable)

    def ToDict(self) -> dict:
        return {
            "BasemapID": self.BasemapID,
            "Name": self.Name,
            "CreatedAt": self.CreatedAt,
            "UpdatedAt": self.UpdatedAt,
            "RPMBins": list(self.RPMBins),
            "MAPBins": list(self.MAPBins),
            "VETable": [list(Row) for Row in self.VETable],
            "SparkTable": [list(Row) for Row in self.SparkTable],
            "AFRTable": [list(Row) for Row in self.AFRTable],
            "LearnRate": self.LearnRate,
            "IsActive": True,
        }

    def LookupVE(self, RPM: float, MAP: float) -> tuple[int, int, float]:
        RPMBin = NearestBin(RPM, self.RPMBins)
        MAPBin = NearestBin(MAP, self.MAPBins)
        return RPMBin, MAPBin, float(self.VETable[MAPBin][RPMBin])

    def LookupAFR(self, RPM: float, MAP: float) -> float:
        RPMBin = NearestBin(RPM, self.RPMBins)
        MAPBin = NearestBin(MAP, self.MAPBins)
        return float(self.AFRTable[MAPBin][RPMBin])

    def Pack(self) -> bytes:
        NameBytes = self.Name.encode("utf-8")[: NameWidth - 1].ljust(NameWidth, b"\0")
        Body = bytearray()
        Body.extend(CalMagic)
        Body.extend(struct.pack("<H", CalVersion))
        Body.extend(struct.pack("<I", self.BasemapID))
        Body.extend(NameBytes)
        Body.extend(struct.pack("<ddf", self.CreatedAt, self.UpdatedAt, self.LearnRate))
        Body.extend(struct.pack("<16H", *[int(Value) for Value in self.RPMBins]))
        Body.extend(struct.pack("<16H", *[int(Value) for Value in self.MAPBins]))
        Body.extend(struct.pack(f"<{TableSize * TableSize}f", *Flatten(self.VETable)))
        Body.extend(struct.pack(f"<{TableSize * TableSize}f", *Flatten(self.SparkTable)))
        Body.extend(struct.pack(f"<{TableSize * TableSize}f", *Flatten(self.AFRTable)))
        Checksum = CRC32(bytes(Body))
        Body.extend(struct.pack("<I", Checksum))
        return bytes(Body)

    @classmethod
    def Unpack(cls, Payload: bytes) -> "CalibrationImage":
        if len(Payload) < 16 or Payload[:8] != CalMagic:
            raise ValueError("Invalid calibration magic")
        StoredCRC = struct.unpack_from("<I", Payload, len(Payload) - 4)[0]
        Computed = CRC32(Payload[:-4])
        if StoredCRC != Computed:
            raise ValueError(
                f"Calibration CRC mismatch (stored={StoredCRC:08X} computed={Computed:08X})"
            )
        Offset = 8
        Version = struct.unpack_from("<H", Payload, Offset)[0]
        Offset += 2
        if Version != CalVersion:
            raise ValueError(f"Unsupported calibration version {Version}")
        BasemapID = struct.unpack_from("<I", Payload, Offset)[0]
        Offset += 4
        Name = Payload[Offset : Offset + NameWidth].split(b"\0", 1)[0].decode("utf-8", "replace")
        Offset += NameWidth
        CreatedAt, UpdatedAt, LearnRate = struct.unpack_from("<ddf", Payload, Offset)
        Offset += 20
        RPMBins = list(struct.unpack_from("<16H", Payload, Offset))
        Offset += 32
        MAPBins = list(struct.unpack_from("<16H", Payload, Offset))
        Offset += 32
        CellCount = TableSize * TableSize
        VEFlat = list(struct.unpack_from(f"<{CellCount}f", Payload, Offset))
        Offset += CellCount * 4
        SparkFlat = list(struct.unpack_from(f"<{CellCount}f", Payload, Offset))
        Offset += CellCount * 4
        AFRFlat = list(struct.unpack_from(f"<{CellCount}f", Payload, Offset))
        return cls(
            Name=Name,
            BasemapID=BasemapID,
            CreatedAt=CreatedAt,
            UpdatedAt=UpdatedAt,
            LearnRate=LearnRate,
            RPMBins=[float(Value) for Value in RPMBins],
            MAPBins=[float(Value) for Value in MAPBins],
            VETable=Unflatten(VEFlat),
            SparkTable=Unflatten(SparkFlat),
            AFRTable=Unflatten(AFRFlat),
        )

    def Save(self, PathTarget: Path, ExportHeader: Optional[Path] = None) -> None:
        self.UpdatedAt = time.time()
        AtomicWriteBytes(PathTarget, self.Pack())
        if ExportHeader is not None:
            AtomicWriteBytes(ExportHeader, self.ExportCHeader().encode("utf-8"))

    @classmethod
    def Load(cls, PathTarget: Path) -> "CalibrationImage":
        return cls.Unpack(PathTarget.read_bytes())

    def ExportCHeader(self) -> str:
        """Emit static C arrays mirroring Speeduino/ROM table layout."""
        Lines = [
            "/* Auto-generated K24 Adaptive Tune calibration — ROM-style lookup tables */",
            "#pragma once",
            "#include <stdint.h>",
            "",
            f"#define K24_CAL_NAME \"{self.Name.replace(chr(34), '')}\"",
            f"#define K24_LEARN_RATE {self.LearnRate:.6f}f",
            "",
            "static const uint16_t RPMBins[16] = {",
            "  " + ", ".join(str(int(Value)) for Value in self.RPMBins) + ",",
            "};",
            "",
            "static const uint16_t MAPBins[16] = {",
            "  " + ", ".join(str(int(Value)) for Value in self.MAPBins) + ",",
            "};",
            "",
            "static const uint8_t VETable[16][16] = {",
        ]
        for Row in self.VETable:
            Cells = ", ".join(str(int(min(max(round(Value), 0), 255))) for Value in Row)
            Lines.append(f"  {{{Cells}}},")
        Lines.append("};")
        Lines.append("")
        Lines.append("static const uint8_t SparkTable[16][16] = {")
        for Row in self.SparkTable:
            Cells = ", ".join(str(int(min(max(round(Value), 0), 60))) for Value in Row)
            Lines.append(f"  {{{Cells}}},")
        Lines.append("};")
        Lines.append("")
        Lines.append("/* AFR targets stored as AFR * 10 */")
        Lines.append("static const uint8_t AFRTable[16][16] = {")
        for Row in self.AFRTable:
            Cells = ", ".join(str(int(min(max(round(Value * 10), 0), 255))) for Value in Row)
            Lines.append(f"  {{{Cells}}},")
        Lines.append("};")
        Lines.append("")
        return "\n".join(Lines)

    def VEPageBytes(self) -> bytes:
        Flat = [
            int(min(max(round(Value), 0), 255))
            for Row in self.VETable
            for Value in Row
        ]
        return bytes(Flat)

    def SparkPageBytes(self) -> bytes:
        Flat = [
            int(min(max(round(Value), 0), 60))
            for Row in self.SparkTable
            for Value in Row
        ]
        return bytes(Flat)
