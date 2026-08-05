"""
Adaptive basemap learning agent.

Collects Speeduino realtime samples, bins them into the VE grid, and
gradually corrects each cell from wideband AFR error vs the target AFR map.
Everything stays local in SQLite — the map improves the more you drive.
"""
from __future__ import annotations

import copy
import time
from typing import Any, Optional

from src.config import (
    DefaultMaxCellDeltaPercent,
    DefaultMinSamplesPerCell,
    DefaultWarmCoolantCelsius,
)
from src.db.store import LocalStore
from src.ecu.speeduino import RealtimeData, SpeeduinoClient


def NearestBin(Value: float, Bins: list[float]) -> int:
    BestIndex = 0
    BestDistance = abs(Value - Bins[0])
    for Index in range(1, len(Bins)):
        Distance = abs(Value - Bins[Index])
        if Distance < BestDistance:
            BestDistance = Distance
            BestIndex = Index
    return BestIndex


class AdaptiveTuneAgent:
    def __init__(
        self,
        Store: LocalStore,
        Client: Optional[SpeeduinoClient] = None,
        AutoPush: bool = False,
    ) -> None:
        self.Store = Store
        self.Client = Client
        self.AutoPush = AutoPush
        self.SessionID: Optional[int] = None
        self.Basemap = Store.GetActiveBasemap()
        self.PendingLearns = 0
        self.LastStatus = "Idle"
        self.LastSample: Optional[dict[str, Any]] = None
        self.LastRealtime: Optional[dict[str, Any]] = None
        self.CachedLearnStats = {"CellLearnCount": 0, "ConfidentCells": 0}
        self.LastStatsRefreshAt = 0.0

    def ReloadBasemap(self) -> None:
        self.Basemap = self.Store.GetActiveBasemap()

    def StartSession(self, PortName: str, Notes: str = "") -> int:
        self.SessionID = self.Store.StartSession(PortName, Notes)
        self.LastStatus = f"Session {self.SessionID} started on {PortName}"
        return self.SessionID

    def EndSession(self) -> None:
        if self.SessionID is not None:
            self.Store.Flush()
            self.Store.EndSession(self.SessionID)
            self.LastStatus = f"Session {self.SessionID} ended"
            self.SessionID = None

    def ProcessRealtime(self, Data: RealtimeData) -> Optional[dict[str, Any]]:
        """Ingest one Speeduino sample and update cell learning state."""
        self.LastRealtime = {
            "Timestamp": Data.Timestamp,
            "RPM": Data.RPM,
            "MAP": Data.MAP,
            "TPS": Data.TPS,
            "AFR": Data.AFR,
            "CoolantCelsius": Data.CoolantCelsius,
            "IATCelsius": Data.IATCelsius,
            "SparkAdvance": Data.SparkAdvance,
            "BatteryVoltage": Data.BatteryVoltage,
            "EngineStatus": Data.EngineStatus,
        }
        if Data.RPM < 400 or Data.MAP <= 0:
            self.LastStatus = "Waiting for running engine"
            return None
        if Data.CoolantCelsius < DefaultWarmCoolantCelsius:
            self.LastStatus = "Warm-up — learning paused"
            return None
        if Data.AFR <= 8.0 or Data.AFR >= 22.0:
            self.LastStatus = "AFR out of range — sample skipped"
            return None

        RPMBin = NearestBin(Data.RPM, self.Basemap["RPMBins"])
        MAPBin = NearestBin(Data.MAP, self.Basemap["MAPBins"])
        TargetAFR = float(self.Basemap["AFRTable"][MAPBin][RPMBin])
        CurrentVE = float(self.Basemap["VETable"][MAPBin][RPMBin])

        # Fuel rule of thumb: lean measured AFR → need more VE
        # VE_suggested ≈ VE * (AFR_measured / AFR_target)
        Ratio = Data.AFR / TargetAFR
        SuggestedVE = CurrentVE * Ratio

        # Soft weight: prefer steady cruise (moderate TPS) over tip-in
        Weight = 1.0
        if Data.TPS > 80:
            Weight = 0.4
        elif Data.TPS < 5:
            Weight = 0.5

        ErrorPercent = abs(Ratio - 1.0) * 100.0
        Confidence = max(0.0, min(1.0, 1.0 - (ErrorPercent / 25.0)))

        Sample = {
            "Timestamp": Data.Timestamp,
            "RPM": Data.RPM,
            "MAP": Data.MAP,
            "TPS": Data.TPS,
            "AFR": Data.AFR,
            "TargetAFR": TargetAFR,
            "CoolantCelsius": Data.CoolantCelsius,
            "IATCelsius": Data.IATCelsius,
            "SparkAdvance": Data.SparkAdvance,
            "CurrentVE": CurrentVE,
            "RPMBin": RPMBin,
            "MAPBin": MAPBin,
            "SuggestedVE": SuggestedVE,
            "Weight": Weight,
            "Confidence": Confidence,
        }
        self.LastSample = Sample

        if self.SessionID is not None:
            self.Store.InsertSample(self.SessionID, Sample)

        self.Store.UpsertCellLearn(
            BasemapID=self.Basemap["BasemapID"],
            RPMBin=RPMBin,
            MAPBin=MAPBin,
            Correction=SuggestedVE - CurrentVE,
            Weight=Weight,
            LastVE=CurrentVE,
            SuggestedVE=SuggestedVE,
            Confidence=Confidence,
        )
        self.PendingLearns += 1
        self.LastStatus = (
            f"Learn cell MAP[{MAPBin}] RPM[{RPMBin}] "
            f"AFR {Data.AFR:.1f}/{TargetAFR:.1f} → VE {CurrentVE:.1f}→{SuggestedVE:.1f}"
        )
        return Sample

    def ApplyLearnedCorrections(self) -> dict[str, Any]:
        """
        Blend confident cell suggestions into the active basemap.
        Returns a summary of how many cells moved.
        """
        Learns = self.Store.GetCellLearns(self.Basemap["BasemapID"])
        VETable = copy.deepcopy(self.Basemap["VETable"])
        LearnRate = float(self.Basemap["LearnRate"])
        MaxDelta = DefaultMaxCellDeltaPercent / 100.0
        CellsUpdated = 0
        TotalDelta = 0.0

        for Cell in Learns:
            if Cell["SampleCount"] < DefaultMinSamplesPerCell:
                continue
            if Cell["SumWeight"] <= 0:
                continue

            MAPBin = int(Cell["MAPBin"])
            RPMBin = int(Cell["RPMBin"])
            CurrentVE = float(VETable[MAPBin][RPMBin])
            # Use the weighted correction accumulated over every prior drive,
            # not merely the most recent suggestion for this cell.
            AvgSuggested = CurrentVE + (
                float(Cell["SumCorrection"]) / float(Cell["SumWeight"])
            )

            Desired = CurrentVE + (AvgSuggested - CurrentVE) * LearnRate
            ClampedDelta = max(-MaxDelta, min(MaxDelta, (Desired - CurrentVE) / max(CurrentVE, 1.0)))
            NewVE = CurrentVE * (1.0 + ClampedDelta)
            NewVE = max(20.0, min(140.0, NewVE))

            if abs(NewVE - CurrentVE) < 0.05:
                continue

            VETable[MAPBin][RPMBin] = round(NewVE, 2)
            CellsUpdated += 1
            TotalDelta += abs(NewVE - CurrentVE)

        self.Store.SaveBasemapTables(
            BasemapID=self.Basemap["BasemapID"],
            VETable=VETable,
            SparkTable=self.Basemap["SparkTable"],
            AFRTable=self.Basemap["AFRTable"],
        )
        self.ReloadBasemap()
        self.PendingLearns = 0
        Summary = {
            "CellsUpdated": CellsUpdated,
            "AverageDelta": (TotalDelta / CellsUpdated) if CellsUpdated else 0.0,
            "UpdatedAt": time.time(),
        }
        self.LastStatus = (
            f"Applied learn pass — {CellsUpdated} cells updated "
            f"(avg dVE {Summary['AverageDelta']:.2f})"
        )
        return Summary

    def PushBasemapToSpeeduino(self, Burn: bool = False) -> None:
        if not self.Client:
            raise RuntimeError("No Speeduino client attached")
        self.Client.WriteVETable(self.Basemap["VETable"])
        self.Client.WriteSparkTable(self.Basemap["SparkTable"])
        if Burn:
            self.Client.BurnToFlash()
        self.LastStatus = "Basemap pushed to Speeduino" + (" (burned)" if Burn else "")

    def PullBasemapFromSpeeduino(self) -> None:
        if not self.Client:
            raise RuntimeError("No Speeduino client attached")
        VETable = self.Client.ReadVETable()
        SparkTable = self.Client.ReadSparkTable()
        self.Store.SaveBasemapTables(
            BasemapID=self.Basemap["BasemapID"],
            VETable=VETable,
            SparkTable=SparkTable,
            AFRTable=self.Basemap["AFRTable"],
        )
        self.ReloadBasemap()
        self.LastStatus = "Basemap pulled from Speeduino"

    def GetDashboard(self) -> dict[str, Any]:
        # Database statistics are intentionally refreshed at 1 Hz. Live
        # gauges read in-memory values and can repaint much faster.
        Now = time.monotonic()
        if Now - self.LastStatsRefreshAt >= 1.0:
            Learns = self.Store.GetCellLearns(self.Basemap["BasemapID"])
            Confident = [
                Cell
                for Cell in Learns
                if Cell["SampleCount"] >= DefaultMinSamplesPerCell
            ]
            self.CachedLearnStats = {
                "CellLearnCount": len(Learns),
                "ConfidentCells": len(Confident),
            }
            self.LastStatsRefreshAt = Now
        return {
            "BasemapName": self.Basemap["Name"],
            "BasemapID": self.Basemap["BasemapID"],
            "LearnRate": self.Basemap["LearnRate"],
            "SessionID": self.SessionID,
            "PendingLearns": self.PendingLearns,
            **self.CachedLearnStats,
            "LastStatus": self.LastStatus,
            "LastSample": self.LastSample,
            "LastRealtime": self.LastRealtime,
            "VETable": self.Basemap["VETable"],
            "SparkTable": self.Basemap["SparkTable"],
            "AFRTable": self.Basemap["AFRTable"],
            "RPMBins": self.Basemap["RPMBins"],
            "MAPBins": self.Basemap["MAPBins"],
        }
