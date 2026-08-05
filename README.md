# K24 Adaptive Tune

Local, offline adaptive basemap agent for a **K24 + Speeduino (Arduino)** setup.

Connects over **USB serial** to Speeduino, captures drive data into **append-only binary logs**, and gradually refines a **CRC-protected calibration image** (ROM-style VE/spark/AFR lookup tables). Durable learn state uses **NvM-style binary blocks with CRC** — not SQLite, DuckDB, or RocksDB. Maps push as raw Speeduino page bytes.

## Storage model (ECU-faithful)

| Concern | On Speeduino | On this PC agent |
| --- | --- | --- |
| Fuel / spark maps | Static tables in RAM/Flash, O(1) cell lookup | `data/calibration/active.cal` (+ `active.h` C arrays) |
| Durable state | NvM / EEPROM blocks + CRC | `data/nvm/*.bin` CRC blocks |
| High-rate samples | RTOS / bare-metal buffers | `data/logs/session_NNNN.bin` append-only |
| Vehicle notes | N/A | `data/profiles/*.json` |

Traditional database engines are **not** used. Capture never depends on transactional DB flushes.

## What it does

1. Opens a COM port to your Speeduino Arduino (or runs in **Simulate** mode)
2. Streams realtime RPM / MAP / TPS / AFR / temps
3. Bins samples into the 16×16 VE grid
4. Learns per-cell fuel corrections from `AFR_measured / AFR_target`
5. Persists calibration, NvM learns, and binary session logs under `data/`
6. On demand: **Apply learned corrections** → **Push map → Speeduino** → optional **Burn to EEPROM**

## Desktop interface

The offline GUI includes:

- **Live Dashboard** — RPM, MAP, TPS, AFR/target, coolant, IAT, spark,
  battery voltage, packet rate, engine status, and short realtime trends
- **Tune Map** — active VE table, learned correction controls, ECU pull/push,
  and EEPROM burn
- **Vehicle Setup** — reusable profiles for any K24 vehicle/engine combination,
  drivetrain, gearing, tires, fuel system, sensors, ECU, upgrades, and notes
- **Sessions** — prior drive dates, durations, ports, and captured sample counts
- **Help** — offline setup tips, troubleshooting, fuel-learning guidance,
  K24 configuration ideas, dashboard definitions, and safety practices

Serial capture runs on a dedicated worker thread. The GUI paints only the
latest in-memory snapshot at a fixed rate. Log appends are buffered; CRC
calibration / NvM writes happen on Apply, disconnect, and shutdown.

## Requirements

- Python 3.10+
- Speeduino on Arduino Mega (or compatible) with USB serial
- Wideband AFR available to Speeduino (so realtime AFR is valid)

## Setup

```powershell
cd C:\Users\ddave\Projects\k24-adaptive-tune
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

GUI (default):

```powershell
python main.py
```

Headless simulated learning demo (no hardware):

```powershell
python main.py --cli-demo
```

## Typical workflow

1. Flash / configure Speeduino as usual (sensors, injectors, ignition, base timing)
2. Launch this app, pick the Speeduino COM port (or leave Simulate on to dry-run)
3. Drive — the agent accumulates cell learns while coolant is warm and AFR is sane
4. Click **Apply learned corrections** when enough cells have confidence
5. **Push map → Speeduino**, verify on a test drive, then **Burn to EEPROM** when happy

## Project layout

```
main.py                      entrypoint
src/config.py                baud, bins, learn knobs, data paths
src/ecu/speeduino.py         USB protocol (realtime + raw page R/W)
src/storage/calibration.py   CRC .cal lookup tables + C header export
src/storage/nvm.py           CRC cell-learn + session index blocks
src/storage/log.py           buffered append-only sample logs
src/storage/store.py         facade used by agent / UI
src/agent/learner.py         adaptive basemap agent
src/ui/app.py                offline CustomTkinter UI
data/calibration/            active.cal / active.h
data/nvm/                    cell_learns.bin / sessions.bin
data/logs/                   session_NNNN.bin
data/profiles/               vehicle setup JSON
```

## Important notes

- **Page offsets** in `src/ecu/speeduino.py` must match your Speeduino firmware / INI. If pull/push looks wrong, align page numbers and realtime byte offsets to your build.
- Learning is intentionally conservative (learn rate + per-cell clamp). It improves the map over many drives; it is not a substitute for safe initial timing and fueling.
- Keep a known-good Speeduino tune backup before burning learned maps.
- An old `data/k24_adaptive.db` is imported once into the new formats and renamed to `.db.bak`.

## Safety

Wrong fueling or spark can damage an engine. Use Simulate first, then short test sessions, and only burn after verifying AFR and knock/safety margins on your hardware.
