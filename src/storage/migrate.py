"""One-shot import from legacy SQLite k24_adaptive.db into ECU-style files."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from src.storage.calibration import CalibrationImage
from src.storage.nvm import CellLearnBlock, SessionIndex, SessionRecord


def MigrateLegacySQLite(
    LegacyDB: Path,
    CalPath: Path,
    LearnPath: Path,
    SessionsPath: Path,
) -> bool:
    """
    Import calibration + cell learns + session index from an old .db.
    Returns True if migration ran. Renames the .db to .db.bak afterward.
    """
    if not LegacyDB.exists():
        return False
    if CalPath.exists():
        # Already on the new format; leave the old DB alone (rename only).
        Backup = LegacyDB.with_suffix(".db.bak")
        if not Backup.exists():
            LegacyDB.rename(Backup)
        return False

    Connection = sqlite3.connect(str(LegacyDB))
    Connection.row_factory = sqlite3.Row
    try:
        Row = Connection.execute(
            "SELECT * FROM Basemaps WHERE IsActive = 1 LIMIT 1"
        ).fetchone()
        if Row is None:
            Row = Connection.execute(
                "SELECT * FROM Basemaps ORDER BY BasemapID DESC LIMIT 1"
            ).fetchone()
        if Row is not None:
            Image = CalibrationImage(
                Name=str(Row["Name"]),
                BasemapID=int(Row["BasemapID"]),
                CreatedAt=float(Row["CreatedAt"]),
                UpdatedAt=float(Row["UpdatedAt"]),
                LearnRate=float(Row["LearnRate"]),
                RPMBins=json.loads(Row["RPMBinsJSON"]),
                MAPBins=json.loads(Row["MAPBinsJSON"]),
                VETable=json.loads(Row["VETableJSON"]),
                SparkTable=json.loads(Row["SparkTableJSON"]),
                AFRTable=json.loads(Row["AFRTableJSON"]),
            )
            Image.Save(CalPath, ExportHeader=CalPath.with_suffix(".h"))

            Learns = CellLearnBlock()
            for LearnRow in Connection.execute(
                "SELECT * FROM CellLearns WHERE BasemapID = ?",
                (int(Row["BasemapID"]),),
            ):
                Cell = Learns.Get(int(LearnRow["MAPBin"]), int(LearnRow["RPMBin"]))
                Cell.SampleCount = int(LearnRow["SampleCount"])
                Cell.SumCorrection = float(LearnRow["SumCorrection"])
                Cell.SumWeight = float(LearnRow["SumWeight"])
                Cell.LastVE = float(LearnRow["LastVE"] or 0.0)
                Cell.SuggestedVE = float(LearnRow["SuggestedVE"] or 0.0)
                Cell.Confidence = float(LearnRow["Confidence"] or 0.0)
                Cell.UpdatedAt = float(LearnRow["UpdatedAt"] or time.time())
            Learns.Save(LearnPath)

        Index = SessionIndex()
        for SessionRow in Connection.execute(
            "SELECT * FROM Sessions ORDER BY SessionID ASC"
        ):
            SessionID = int(SessionRow["SessionID"])
            Index.Sessions.append(
                SessionRecord(
                    SessionID=SessionID,
                    StartedAt=float(SessionRow["StartedAt"]),
                    EndedAt=float(SessionRow["EndedAt"] or 0.0),
                    PortName=str(SessionRow["PortName"] or ""),
                    Notes=str(SessionRow["Notes"] or ""),
                    SampleCount=int(SessionRow["SampleCount"] or 0),
                    LogPath="",
                )
            )
            Index.NextSessionID = max(Index.NextSessionID, SessionID + 1)
        if Index.Sessions:
            Index.Save(SessionsPath)

        # Profiles left in SQLite are skipped; users re-enter setup if needed.
    finally:
        Connection.close()

    Backup = LegacyDB.with_suffix(".db.bak")
    if Backup.exists():
        Backup = LegacyDB.with_name(f"{LegacyDB.stem}.{int(time.time())}.db.bak")
    LegacyDB.rename(Backup)
    return True
