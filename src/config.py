from __future__ import annotations

from pathlib import Path

ProjectRoot = Path(__file__).resolve().parent.parent
DataDir = ProjectRoot / "data"
MapsDir = ProjectRoot / "maps"

DataDir.mkdir(parents=True, exist_ok=True)
MapsDir.mkdir(parents=True, exist_ok=True)

# Speeduino serial defaults (USB to Arduino Mega / compatible)
DefaultBaudRate = 115200
DefaultTimeoutSeconds = 1.0

# Typical Speeduino 16x16 speed-density axes for a K24-ish MAP setup
DefaultRPMBins = [
    500, 1000, 1500, 2000, 2500, 3000, 3500, 4000,
    4500, 5000, 5500, 6000, 6500, 7000, 7500, 8000,
]
DefaultMAPBins = [
    20, 30, 40, 50, 60, 70, 80, 90,
    100, 110, 120, 130, 140, 160, 180, 200,
]

# Learning agent knobs
DefaultLearnRate = 0.15
DefaultMinSamplesPerCell = 8
DefaultMaxCellDeltaPercent = 8.0
DefaultTargetAFR = 14.7
DefaultIdleRPM = 900
DefaultWarmCoolantCelsius = 70.0

# Sampling runs independently from GUI painting. The ECU link is polled as
# quickly as it can respond; the GUI renders at a capped rate to avoid lag.
PollIntervalSeconds = 0.02
UIRefreshIntervalMilliseconds = 50
DatabaseFlushIntervalSeconds = 0.50
DatabaseBatchSize = 50
ChartHistorySeconds = 20
ChartMaximumPoints = 400

AppTitle = "K24 Adaptive Tune"
AppVersion = "0.2.0"
