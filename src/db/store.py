"""
Local SQLite storage for sessions, samples, and evolving basemaps.
All databases live under ./data as temporary/local .db files — no network.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from src.config import (
    DataDir,
    DatabaseBatchSize,
    DatabaseFlushIntervalSeconds,
    DefaultLearnRate,
    DefaultMAPBins,
    DefaultRPMBins,
)


SchemaSQL = """
CREATE TABLE IF NOT EXISTS Sessions (
    SessionID INTEGER PRIMARY KEY AUTOINCREMENT,
    StartedAt REAL NOT NULL,
    EndedAt REAL,
    PortName TEXT,
    Notes TEXT,
    SampleCount INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS Samples (
    SampleID INTEGER PRIMARY KEY AUTOINCREMENT,
    SessionID INTEGER NOT NULL,
    Timestamp REAL NOT NULL,
    RPM REAL NOT NULL,
    MAP REAL NOT NULL,
    TPS REAL,
    AFR REAL,
    TargetAFR REAL,
    CoolantCelsius REAL,
    IATCelsius REAL,
    SparkAdvance REAL,
    CurrentVE REAL,
    RPMBin INTEGER,
    MAPBin INTEGER,
    FOREIGN KEY (SessionID) REFERENCES Sessions(SessionID)
);

CREATE TABLE IF NOT EXISTS Basemaps (
    BasemapID INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL,
    CreatedAt REAL NOT NULL,
    UpdatedAt REAL NOT NULL,
    RPMBinsJSON TEXT NOT NULL,
    MAPBinsJSON TEXT NOT NULL,
    VETableJSON TEXT NOT NULL,
    SparkTableJSON TEXT NOT NULL,
    AFRTableJSON TEXT NOT NULL,
    LearnRate REAL NOT NULL,
    IsActive INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS CellLearns (
    CellLearnID INTEGER PRIMARY KEY AUTOINCREMENT,
    BasemapID INTEGER NOT NULL,
    RPMBin INTEGER NOT NULL,
    MAPBin INTEGER NOT NULL,
    SampleCount INTEGER NOT NULL DEFAULT 0,
    SumCorrection REAL NOT NULL DEFAULT 0,
    SumWeight REAL NOT NULL DEFAULT 0,
    LastVE REAL,
    SuggestedVE REAL,
    Confidence REAL NOT NULL DEFAULT 0,
    UpdatedAt REAL NOT NULL,
    UNIQUE (BasemapID, RPMBin, MAPBin),
    FOREIGN KEY (BasemapID) REFERENCES Basemaps(BasemapID)
);

CREATE TABLE IF NOT EXISTS VehicleProfiles (
    ProfileID INTEGER PRIMARY KEY AUTOINCREMENT,
    ProfileName TEXT NOT NULL,
    CreatedAt REAL NOT NULL,
    UpdatedAt REAL NOT NULL,
    IsActive INTEGER NOT NULL DEFAULT 0,
    VehicleMake TEXT,
    VehicleModel TEXT,
    VehicleYear TEXT,
    VehicleTrim TEXT,
    EngineCode TEXT,
    EngineDisplacement TEXT,
    CompressionRatio TEXT,
    Transmission TEXT,
    Drivetrain TEXT,
    FinalDriveRatio TEXT,
    TireSize TEXT,
    VehicleWeight TEXT,
    FuelType TEXT,
    InjectorSize TEXT,
    FuelPressure TEXT,
    MAPSensor TEXT,
    WidebandSensor TEXT,
    ECUBoard TEXT,
    FirmwareVersion TEXT,
    RevLimit TEXT,
    BoostLimit TEXT,
    Upgrades TEXT,
    Notes TEXT
);

CREATE INDEX IF NOT EXISTS IDX_Samples_SessionID ON Samples(SessionID);
CREATE INDEX IF NOT EXISTS IDX_Samples_Bins ON Samples(RPMBin, MAPBin);
CREATE INDEX IF NOT EXISTS IDX_CellLearns_BasemapID ON CellLearns(BasemapID);
CREATE INDEX IF NOT EXISTS IDX_VehicleProfiles_IsActive ON VehicleProfiles(IsActive);
"""


def DefaultVETable(Rows: int = 16, Cols: int = 16) -> list[list[float]]:
    """Conservative speed-density VE seed for a naturally aspirated K24."""
    Table: list[list[float]] = []
    for Row in range(Rows):
        Line: list[float] = []
        MAPFactor = 0.55 + (Row / max(Rows - 1, 1)) * 0.45
        for Col in range(Cols):
            RPMFactor = 0.75 + (Col / max(Cols - 1, 1)) * 0.35
            # Peak volumetric efficiency mid-upper RPM / higher MAP
            Peak = 1.0 - abs((Col / max(Cols - 1, 1)) - 0.55) * 0.25
            Value = 100.0 * MAPFactor * RPMFactor * Peak
            Line.append(round(min(max(Value, 35.0), 110.0), 1))
        Table.append(Line)
    return Table


def DefaultSparkTable(Rows: int = 16, Cols: int = 16) -> list[list[float]]:
    """Conservative ignition advance seed (degrees BTDC)."""
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


def DefaultAFRTable(Rows: int = 16, Cols: int = 16) -> list[list[float]]:
    """Stoich cruise with richer targets under load."""
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


class LocalStore:
    def __init__(self, DBPath: Optional[Path] = None) -> None:
        self.DBPath = DBPath or (DataDir / "k24_adaptive.db")
        self.DBPath.parent.mkdir(parents=True, exist_ok=True)
        self.Connection = sqlite3.connect(str(self.DBPath), check_same_thread=False)
        self.Connection.row_factory = sqlite3.Row
        self.Lock = threading.RLock()
        self.SampleBuffer: list[tuple[Any, ...]] = []
        self.CellLearnBuffer: dict[tuple[int, int, int], dict[str, Any]] = {}
        self.LastFlushAt = time.monotonic()
        self._InitSchema()
        self._EnsureActiveBasemap()

    def _InitSchema(self) -> None:
        with self.Lock:
            self.Connection.execute("PRAGMA journal_mode=WAL")
            self.Connection.execute("PRAGMA synchronous=NORMAL")
            self.Connection.execute("PRAGMA temp_store=MEMORY")
            self.Connection.execute("PRAGMA cache_size=-20000")
            self.Connection.executescript(SchemaSQL)
            self.Connection.commit()

    def _EnsureActiveBasemap(self) -> int:
        Cursor = self.Connection.execute(
            "SELECT BasemapID FROM Basemaps WHERE IsActive = 1 LIMIT 1"
        )
        Row = Cursor.fetchone()
        if Row:
            return int(Row["BasemapID"])
        return self.CreateBasemap("K24 Seed Basemap", Activate=True)

    def CreateBasemap(self, Name: str, Activate: bool = False) -> int:
        Now = time.time()
        VE = DefaultVETable()
        Spark = DefaultSparkTable()
        AFR = DefaultAFRTable()
        if Activate:
            self.Connection.execute("UPDATE Basemaps SET IsActive = 0")
        Cursor = self.Connection.execute(
            """
            INSERT INTO Basemaps (
                Name, CreatedAt, UpdatedAt, RPMBinsJSON, MAPBinsJSON,
                VETableJSON, SparkTableJSON, AFRTableJSON, LearnRate, IsActive
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                Name,
                Now,
                Now,
                json.dumps(DefaultRPMBins),
                json.dumps(DefaultMAPBins),
                json.dumps(VE),
                json.dumps(Spark),
                json.dumps(AFR),
                DefaultLearnRate,
                1 if Activate else 0,
            ),
        )
        self.Connection.commit()
        return int(Cursor.lastrowid)

    def GetActiveBasemap(self) -> dict[str, Any]:
        Row = self.Connection.execute(
            "SELECT * FROM Basemaps WHERE IsActive = 1 LIMIT 1"
        ).fetchone()
        if not Row:
            BasemapID = self._EnsureActiveBasemap()
            Row = self.Connection.execute(
                "SELECT * FROM Basemaps WHERE BasemapID = ?", (BasemapID,)
            ).fetchone()
        return self._BasemapFromRow(Row)

    def _BasemapFromRow(self, Row: sqlite3.Row) -> dict[str, Any]:
        return {
            "BasemapID": int(Row["BasemapID"]),
            "Name": Row["Name"],
            "CreatedAt": float(Row["CreatedAt"]),
            "UpdatedAt": float(Row["UpdatedAt"]),
            "RPMBins": json.loads(Row["RPMBinsJSON"]),
            "MAPBins": json.loads(Row["MAPBinsJSON"]),
            "VETable": json.loads(Row["VETableJSON"]),
            "SparkTable": json.loads(Row["SparkTableJSON"]),
            "AFRTable": json.loads(Row["AFRTableJSON"]),
            "LearnRate": float(Row["LearnRate"]),
            "IsActive": bool(Row["IsActive"]),
        }

    def SaveBasemapTables(
        self,
        BasemapID: int,
        VETable: list[list[float]],
        SparkTable: list[list[float]],
        AFRTable: list[list[float]],
    ) -> None:
        self.Connection.execute(
            """
            UPDATE Basemaps
            SET VETableJSON = ?, SparkTableJSON = ?, AFRTableJSON = ?, UpdatedAt = ?
            WHERE BasemapID = ?
            """,
            (
                json.dumps(VETable),
                json.dumps(SparkTable),
                json.dumps(AFRTable),
                time.time(),
                BasemapID,
            ),
        )
        self.Connection.commit()

    def StartSession(self, PortName: str, Notes: str = "") -> int:
        Cursor = self.Connection.execute(
            """
            INSERT INTO Sessions (StartedAt, PortName, Notes, SampleCount)
            VALUES (?, ?, ?, 0)
            """,
            (time.time(), PortName, Notes),
        )
        self.Connection.commit()
        return int(Cursor.lastrowid)

    def EndSession(self, SessionID: int) -> None:
        self.Connection.execute(
            "UPDATE Sessions SET EndedAt = ? WHERE SessionID = ?",
            (time.time(), SessionID),
        )
        self.Connection.commit()

    def InsertSample(self, SessionID: int, Sample: dict[str, Any]) -> int:
        """Queue a sample for a batched transaction instead of blocking serial I/O."""
        with self.Lock:
            self.SampleBuffer.append((
                SessionID,
                Sample.get("Timestamp", time.time()),
                Sample["RPM"],
                Sample["MAP"],
                Sample.get("TPS"),
                Sample.get("AFR"),
                Sample.get("TargetAFR"),
                Sample.get("CoolantCelsius"),
                Sample.get("IATCelsius"),
                Sample.get("SparkAdvance"),
                Sample.get("CurrentVE"),
                Sample.get("RPMBin"),
                Sample.get("MAPBin"),
            ))
            self.FlushIfNeeded()
        return 0

    def UpsertCellLearn(
        self,
        BasemapID: int,
        RPMBin: int,
        MAPBin: int,
        Correction: float,
        Weight: float,
        LastVE: float,
        SuggestedVE: float,
        Confidence: float,
        SampleCountDelta: int = 1,
    ) -> None:
        """Aggregate repeated cell updates in memory until the next batch flush."""
        with self.Lock:
            Key = (BasemapID, RPMBin, MAPBin)
            Pending = self.CellLearnBuffer.get(Key)
            if Pending is None:
                Pending = {
                    "SampleCount": 0,
                    "SumCorrection": 0.0,
                    "SumWeight": 0.0,
                }
                self.CellLearnBuffer[Key] = Pending
            Pending["SampleCount"] += SampleCountDelta
            Pending["SumCorrection"] += Correction * Weight
            Pending["SumWeight"] += Weight
            Pending["LastVE"] = LastVE
            Pending["SuggestedVE"] = SuggestedVE
            Pending["Confidence"] = Confidence
            Pending["UpdatedAt"] = time.time()
            self.FlushIfNeeded()

    def FlushIfNeeded(self) -> None:
        PendingCount = len(self.SampleBuffer)
        FlushDue = time.monotonic() - self.LastFlushAt >= DatabaseFlushIntervalSeconds
        if PendingCount >= DatabaseBatchSize or FlushDue:
            self.Flush()

    def Flush(self) -> None:
        """Persist queued samples and learns in one short WAL transaction."""
        with self.Lock:
            if not self.SampleBuffer and not self.CellLearnBuffer:
                self.LastFlushAt = time.monotonic()
                return
            Samples = self.SampleBuffer
            CellLearns = list(self.CellLearnBuffer.items())
            self.SampleBuffer = []
            self.CellLearnBuffer = {}
            try:
                self.Connection.execute("BEGIN")
                if Samples:
                    self.Connection.executemany(
                        """
                        INSERT INTO Samples (
                            SessionID, Timestamp, RPM, MAP, TPS, AFR, TargetAFR,
                            CoolantCelsius, IATCelsius, SparkAdvance, CurrentVE,
                            RPMBin, MAPBin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        Samples,
                    )
                    SessionCounts: dict[int, int] = {}
                    for Sample in Samples:
                        SessionID = int(Sample[0])
                        SessionCounts[SessionID] = SessionCounts.get(SessionID, 0) + 1
                    self.Connection.executemany(
                        "UPDATE Sessions SET SampleCount = SampleCount + ? WHERE SessionID = ?",
                        [(Count, SessionID) for SessionID, Count in SessionCounts.items()],
                    )
                if CellLearns:
                    self.Connection.executemany(
                        """
                        INSERT INTO CellLearns (
                            BasemapID, RPMBin, MAPBin, SampleCount, SumCorrection,
                            SumWeight, LastVE, SuggestedVE, Confidence, UpdatedAt
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(BasemapID, RPMBin, MAPBin) DO UPDATE SET
                            SampleCount = SampleCount + excluded.SampleCount,
                            SumCorrection = SumCorrection + excluded.SumCorrection,
                            SumWeight = SumWeight + excluded.SumWeight,
                            LastVE = excluded.LastVE,
                            SuggestedVE = excluded.SuggestedVE,
                            Confidence = excluded.Confidence,
                            UpdatedAt = excluded.UpdatedAt
                        """,
                        [
                            (
                                Key[0], Key[1], Key[2], Value["SampleCount"],
                                Value["SumCorrection"], Value["SumWeight"],
                                Value["LastVE"], Value["SuggestedVE"],
                                Value["Confidence"], Value["UpdatedAt"],
                            )
                            for Key, Value in CellLearns
                        ],
                    )
                self.Connection.commit()
            except Exception:
                self.Connection.rollback()
                self.SampleBuffer = Samples + self.SampleBuffer
                for Key, Value in CellLearns:
                    Existing = self.CellLearnBuffer.get(Key)
                    if Existing:
                        Existing["SampleCount"] += Value["SampleCount"]
                        Existing["SumCorrection"] += Value["SumCorrection"]
                        Existing["SumWeight"] += Value["SumWeight"]
                    else:
                        self.CellLearnBuffer[Key] = Value
                raise
            finally:
                self.LastFlushAt = time.monotonic()

    def GetCellLearns(self, BasemapID: int) -> list[dict[str, Any]]:
        self.Flush()
        Rows = self.Connection.execute(
            "SELECT * FROM CellLearns WHERE BasemapID = ? ORDER BY MAPBin, RPMBin",
            (BasemapID,),
        ).fetchall()
        Result: list[dict[str, Any]] = []
        for Row in Rows:
            Result.append(
                {
                    "CellLearnID": int(Row["CellLearnID"]),
                    "BasemapID": int(Row["BasemapID"]),
                    "RPMBin": int(Row["RPMBin"]),
                    "MAPBin": int(Row["MAPBin"]),
                    "SampleCount": int(Row["SampleCount"]),
                    "SumCorrection": float(Row["SumCorrection"]),
                    "SumWeight": float(Row["SumWeight"]),
                    "LastVE": Row["LastVE"],
                    "SuggestedVE": Row["SuggestedVE"],
                    "Confidence": float(Row["Confidence"]),
                    "UpdatedAt": float(Row["UpdatedAt"]),
                }
            )
        return Result

    def ListSessions(self, Limit: int = 50) -> list[dict[str, Any]]:
        self.Flush()
        Rows = self.Connection.execute(
            """
            SELECT SessionID, StartedAt, EndedAt, PortName, Notes, SampleCount
            FROM Sessions
            ORDER BY SessionID DESC
            LIMIT ?
            """,
            (Limit,),
        ).fetchall()
        return [dict(Row) for Row in Rows]

    def SaveVehicleProfile(self, Profile: dict[str, Any]) -> int:
        Fields = [
            "ProfileName", "VehicleMake", "VehicleModel", "VehicleYear",
            "VehicleTrim", "EngineCode", "EngineDisplacement",
            "CompressionRatio", "Transmission", "Drivetrain",
            "FinalDriveRatio", "TireSize", "VehicleWeight", "FuelType",
            "InjectorSize", "FuelPressure", "MAPSensor", "WidebandSensor",
            "ECUBoard", "FirmwareVersion", "RevLimit", "BoostLimit",
            "Upgrades", "Notes",
        ]
        with self.Lock:
            Now = time.time()
            ProfileID = Profile.get("ProfileID")
            self.Connection.execute("UPDATE VehicleProfiles SET IsActive = 0")
            if ProfileID:
                Assignments = ", ".join(f"{Field} = ?" for Field in Fields)
                Values = [str(Profile.get(Field, "")).strip() for Field in Fields]
                self.Connection.execute(
                    f"UPDATE VehicleProfiles SET {Assignments}, UpdatedAt = ?, IsActive = 1 "
                    "WHERE ProfileID = ?",
                    (*Values, Now, int(ProfileID)),
                )
                ResultID = int(ProfileID)
            else:
                Columns = ", ".join(Fields)
                Placeholders = ", ".join("?" for _ in Fields)
                Values = [str(Profile.get(Field, "")).strip() for Field in Fields]
                Cursor = self.Connection.execute(
                    f"INSERT INTO VehicleProfiles "
                    f"({Columns}, CreatedAt, UpdatedAt, IsActive) "
                    f"VALUES ({Placeholders}, ?, ?, 1)",
                    (*Values, Now, Now),
                )
                ResultID = int(Cursor.lastrowid)
            self.Connection.commit()
            return ResultID

    def GetActiveVehicleProfile(self) -> Optional[dict[str, Any]]:
        with self.Lock:
            Row = self.Connection.execute(
                "SELECT * FROM VehicleProfiles WHERE IsActive = 1 LIMIT 1"
            ).fetchone()
            return dict(Row) if Row else None

    def ListVehicleProfiles(self) -> list[dict[str, Any]]:
        with self.Lock:
            Rows = self.Connection.execute(
                "SELECT * FROM VehicleProfiles ORDER BY UpdatedAt DESC"
            ).fetchall()
            return [dict(Row) for Row in Rows]

    def Close(self) -> None:
        with self.Lock:
            self.Flush()
            self.Connection.close()
