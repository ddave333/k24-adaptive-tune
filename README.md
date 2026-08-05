# K24 Adaptive Tune

Local, offline adaptive basemap agent for a **K24 + Speeduino (Arduino)** setup.

Connects over **USB serial** to Speeduino, logs drive data into local **SQLite `.db` files**, and gradually refines the VE basemap from wideband AFR error as you drive. No cloud, no web deploy, no KPro/Hondata export — maps push straight back to Speeduino.

## What it does

1. Opens a COM port to your Speeduino Arduino (or runs in **Simulate** mode)
2. Streams realtime RPM / MAP / TPS / AFR / temps
3. Bins samples into the 16×16 VE grid
4. Learns per-cell fuel corrections from `AFR_measured / AFR_target`
5. Stores sessions, samples, and cell confidence in `data/k24_adaptive.db`
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
latest in-memory snapshot at a fixed rate, while SQLite uses WAL mode and
batched writes so disk commits do not stall packet collection.

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

1. Flash / configure Speeduino as usual (sensors, injecters, ignition, base timing)
2. Launch this app, pick the Speeduino COM port (or leave Simulate on to dry-run)
3. Drive — the agent accumulates cell learns while coolant is warm and AFR is sane
4. Click **Apply learned corrections** when enough cells have confidence
5. **Push map → Speeduino**, verify on a test drive, then **Burn to EEPROM** when happy

## Project layout

```
main.py                 entrypoint
src/config.py           baud, bins, learn knobs
src/ecu/speeduino.py    USB protocol (realtime + page R/W)
src/db/store.py         local SQLite sessions / samples / basemaps
src/agent/learner.py    adaptive basemap agent
src/ui/app.py           offline CustomTkinter UI
data/                   local .db files (gitignored)
maps/                   optional map snapshots
```

## Important notes

- **Page offsets** in `src/ecu/speeduino.py` must match your Speeduino firmware / INI. If pull/push looks wrong, align page numbers and realtime byte offsets to your build.
- Learning is intentionally conservative (learn rate + per-cell clamp). It improves the map over many drives; it is not a substitute for safe initial timing and fueling.
- Keep a known-good Speeduino tune backup before burning learned maps.

## Safety

Wrong fueling or spark can damage an engine. Use Simulate first, then short test sessions, and only burn after verifying AFR and knock/safety margins on your hardware.
