"""
ECU-faithful local storage facade.

Calibration = CRC-protected raw lookup tables (.cal)
Durable state = NvM-style CRC blocks (learns, sessions)
Samples = append-only binary session logs
Profiles = plain JSON (human metadata, not ECU calibration)

No SQLite / DuckDB / RocksDB.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from src.config import (
    CalibrationDir,
    DataDir,
    LogsDir,
    NvmDir,
    ProfilesDir,
)
from src.storage.calibration import CalibrationImage
from src.storage.log import SessionLogWriter
from src.storage.migrate import MigrateLegacySQLite
from src.storage.nvm import CellLearnBlock, SessionIndex, SessionRecord


class LocalStore:
    def __init__(self, RootDir: Optional[Path] = None) -> None:
        self.RootDir = RootDir or DataDir
        self.CalibrationDir = (
            self.RootDir / "calibration" if RootDir else CalibrationDir
        )
        self.NvmDir = self.RootDir / "nvm" if RootDir else NvmDir
        self.LogsDir = self.RootDir / "logs" if RootDir else LogsDir
        self.ProfilesDir = self.RootDir / "profiles" if RootDir else ProfilesDir

        for Directory in (
            self.CalibrationDir,
            self.NvmDir,
            self.LogsDir,
            self.ProfilesDir,
        ):
            Directory.mkdir(parents=True, exist_ok=True)

        self.CalPath = self.CalibrationDir / "active.cal"
        self.CalHeaderPath = self.CalibrationDir / "active.h"
        self.LearnPath = self.NvmDir / "cell_learns.bin"
        self.SessionsPath = self.NvmDir / "sessions.bin"
        # Compatibility attribute for CLI messages (was DBPath)
        self.DBPath = self.RootDir

        self.Lock = threading.RLock()
        self.Calibration = CalibrationImage()
        self.CellLearns = CellLearnBlock()
        self.SessionIndex = SessionIndex()
        self.ActiveLog: Optional[SessionLogWriter] = None
        self.LearnsDirty = False
        self.SessionsDirty = False
        self.ActiveProfileName: Optional[str] = None

        self._MigrateIfNeeded()
        self._LoadAll()

    def _MigrateIfNeeded(self) -> None:
        Legacy = self.RootDir / "k24_adaptive.db"
        MigrateLegacySQLite(
            Legacy,
            self.CalPath,
            self.LearnPath,
            self.SessionsPath,
        )

    def _LoadAll(self) -> None:
        if self.CalPath.exists():
            self.Calibration = CalibrationImage.Load(self.CalPath)
        else:
            self.Calibration.Save(self.CalPath, ExportHeader=self.CalHeaderPath)
        self.CellLearns = CellLearnBlock.Load(self.LearnPath)
        self.SessionIndex = SessionIndex.Load(self.SessionsPath)
        ActiveMarker = self.ProfilesDir / "active_profile.txt"
        if ActiveMarker.exists():
            self.ActiveProfileName = ActiveMarker.read_text(encoding="utf-8").strip() or None

    def GetActiveBasemap(self) -> dict[str, Any]:
        with self.Lock:
            return self.Calibration.ToDict()

    def SaveBasemapTables(
        self,
        BasemapID: int,
        VETable: list[list[float]],
        SparkTable: list[list[float]],
        AFRTable: list[list[float]],
    ) -> None:
        with self.Lock:
            self.Calibration.BasemapID = BasemapID
            self.Calibration.VETable = [list(Row) for Row in VETable]
            self.Calibration.SparkTable = [list(Row) for Row in SparkTable]
            self.Calibration.AFRTable = [list(Row) for Row in AFRTable]
            self.Calibration.Save(self.CalPath, ExportHeader=self.CalHeaderPath)

    def StartSession(self, PortName: str, Notes: str = "") -> int:
        with self.Lock:
            SessionID = self.SessionIndex.NextSessionID
            self.SessionIndex.NextSessionID += 1
            LogPath = self.LogsDir / f"session_{SessionID:04d}.bin"
            Record = SessionRecord(
                SessionID=SessionID,
                StartedAt=time.time(),
                EndedAt=0.0,
                PortName=PortName,
                Notes=Notes,
                SampleCount=0,
                LogPath=str(LogPath.relative_to(self.RootDir)).replace("\\", "/"),
            )
            self.SessionIndex.Sessions.append(Record)
            self.SessionsDirty = True
            if self.ActiveLog is not None:
                self.ActiveLog.Close()
            self.ActiveLog = SessionLogWriter(LogPath, SessionID)
            self._PersistSessions()
            return SessionID

    def EndSession(self, SessionID: int) -> None:
        with self.Lock:
            if self.ActiveLog is not None:
                SampleCount = self.ActiveLog.SampleCount
                self.ActiveLog.Close()
                self.ActiveLog = None
            else:
                SampleCount = 0
            Session = self.SessionIndex.Find(SessionID)
            if Session is not None:
                Session.EndedAt = time.time()
                if SampleCount:
                    Session.SampleCount = SampleCount
            self.SessionsDirty = True
            self.LearnsDirty = True
            self._PersistSessions()
            self._PersistLearns()

    def InsertSample(self, SessionID: int, Sample: dict[str, Any]) -> int:
        with self.Lock:
            if self.ActiveLog is None or self.ActiveLog.SessionID != SessionID:
                return 0
            self.ActiveLog.Append(Sample)
            Session = self.SessionIndex.Find(SessionID)
            if Session is not None:
                Session.SampleCount = self.ActiveLog.SampleCount
            return self.ActiveLog.SampleCount

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
        with self.Lock:
            self.CellLearns.Upsert(
                RPMBin=RPMBin,
                MAPBin=MAPBin,
                Correction=Correction,
                Weight=Weight,
                LastVE=LastVE,
                SuggestedVE=SuggestedVE,
                Confidence=Confidence,
                SampleCountDelta=SampleCountDelta,
            )
            self.LearnsDirty = True

    def Flush(self) -> None:
        """Persist buffered log bytes and dirty NvM blocks."""
        with self.Lock:
            if self.ActiveLog is not None:
                self.ActiveLog.Flush()
                Session = self.SessionIndex.Find(self.ActiveLog.SessionID)
                if Session is not None:
                    Session.SampleCount = self.ActiveLog.SampleCount
            if self.LearnsDirty:
                self._PersistLearns()
            if self.SessionsDirty:
                self._PersistSessions()

    def FlushIfNeeded(self) -> None:
        self.Flush()

    def _PersistLearns(self) -> None:
        self.CellLearns.Save(self.LearnPath)
        self.LearnsDirty = False

    def _PersistSessions(self) -> None:
        self.SessionIndex.Save(self.SessionsPath)
        self.SessionsDirty = False

    def GetCellLearns(self, BasemapID: int) -> list[dict[str, Any]]:
        with self.Lock:
            return self.CellLearns.AsList(BasemapID=BasemapID)

    def ListSessions(self, Limit: int = 50) -> list[dict[str, Any]]:
        with self.Lock:
            if self.ActiveLog is not None:
                Session = self.SessionIndex.Find(self.ActiveLog.SessionID)
                if Session is not None:
                    Session.SampleCount = self.ActiveLog.SampleCount
            return self.SessionIndex.AsList(Limit=Limit)

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
            Name = str(Profile.get("ProfileName", "")).strip()
            if not Name:
                raise ValueError("ProfileName is required")
            Now = time.time()
            ProfileID = Profile.get("ProfileID")
            Existing = self._ReadProfileFile(Name)
            if ProfileID is None and Existing is not None:
                ProfileID = Existing.get("ProfileID")
            if ProfileID is None:
                ProfileID = int(time.time() * 1000) % 1_000_000_000
            Payload = {Field: str(Profile.get(Field, "")).strip() for Field in Fields}
            Payload["ProfileID"] = int(ProfileID)
            Payload["CreatedAt"] = float(
                Existing["CreatedAt"] if Existing else Now
            )
            Payload["UpdatedAt"] = Now
            Payload["IsActive"] = True
            SafeName = "".join(
                Char if Char.isalnum() or Char in ("-", "_") else "_"
                for Char in Name
            ) or f"profile_{ProfileID}"
            PathTarget = self.ProfilesDir / f"{SafeName}.json"
            # Deactivate others
            for FilePath in self.ProfilesDir.glob("*.json"):
                try:
                    Data = json.loads(FilePath.read_text(encoding="utf-8"))
                    Data["IsActive"] = False
                    FilePath.write_text(
                        json.dumps(Data, indent=2), encoding="utf-8"
                    )
                except (OSError, json.JSONDecodeError):
                    continue
            PathTarget.write_text(json.dumps(Payload, indent=2), encoding="utf-8")
            (self.ProfilesDir / "active_profile.txt").write_text(
                Name, encoding="utf-8"
            )
            self.ActiveProfileName = Name
            return int(ProfileID)

    def _ReadProfileFile(self, ProfileName: str) -> Optional[dict[str, Any]]:
        for FilePath in self.ProfilesDir.glob("*.json"):
            try:
                Data = json.loads(FilePath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(Data.get("ProfileName", "")) == ProfileName:
                return Data
        return None

    def GetActiveVehicleProfile(self) -> Optional[dict[str, Any]]:
        with self.Lock:
            if self.ActiveProfileName:
                Match = self._ReadProfileFile(self.ActiveProfileName)
                if Match:
                    return Match
            for FilePath in self.ProfilesDir.glob("*.json"):
                try:
                    Data = json.loads(FilePath.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if Data.get("IsActive"):
                    return Data
            return None

    def ListVehicleProfiles(self) -> list[dict[str, Any]]:
        with self.Lock:
            Profiles: list[dict[str, Any]] = []
            for FilePath in self.ProfilesDir.glob("*.json"):
                try:
                    Data = json.loads(FilePath.read_text(encoding="utf-8"))
                    Profiles.append(Data)
                except (OSError, json.JSONDecodeError):
                    continue
            Profiles.sort(key=lambda Item: float(Item.get("UpdatedAt") or 0), reverse=True)
            return Profiles

    def Close(self) -> None:
        with self.Lock:
            if self.ActiveLog is not None:
                Session = self.SessionIndex.Find(self.ActiveLog.SessionID)
                if Session is not None and Session.EndedAt <= 0:
                    Session.EndedAt = time.time()
                    Session.SampleCount = self.ActiveLog.SampleCount
                self.ActiveLog.Close()
                self.ActiveLog = None
                self.SessionsDirty = True
            self.Flush()
            self.Calibration.Save(self.CalPath, ExportHeader=self.CalHeaderPath)
