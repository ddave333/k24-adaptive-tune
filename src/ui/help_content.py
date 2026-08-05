"""Offline help content displayed inside the desktop application."""

HelpTopics = {
    "Getting Started": """
1. Create and save a Vehicle Setup profile before tuning.
2. Verify base timing with a timing light and confirm every sensor reading.
3. Connect the Speeduino USB port and watch the Live Dashboard at idle.
4. Do not enable learning until coolant is warm and the wideband is calibrated.
5. Begin with low-load driving. Review corrections before pushing or burning.

The app keeps all sessions, samples, profiles, and learned cells in the local
data/k24_adaptive.db file. Simulate mode lets you test the interface safely.
""",
    "Connection Troubleshooting": """
NO COM PORT
• Use a data-capable USB cable, not a charge-only cable.
• Install the correct CH340, FTDI, or Arduino USB driver.
• Close TunerStudio before connecting; only one program can own a COM port.
• Refresh ports after reconnecting the Arduino.

TIMEOUT OR BAD VALUES
• Confirm the selected baud rate matches the Speeduino firmware.
• Match realtime offsets and page layouts in src/ecu/speeduino.py to the exact
  speeduino.ini shipped with your firmware.
• Check ECU grounds, USB noise, and charging voltage.
""",
    "Before First Start": """
Do not rely on automatic learning to make an unsafe engine startable.

• Confirm injector size, dead time, fuel pressure, and firing order.
• Calibrate TPS, CLT, IAT, MAP, and wideband sensors.
• Lock ignition timing and verify commanded timing equals crank timing.
• Set conservative cranking, after-start, and warm-up enrichment.
• Configure hard rev, overboost, coolant, and AFR protection where available.
• Keep a verified recovery map before writing learned tables.
""",
    "Fuel / VE Learning": """
The fuel learner compares measured AFR with the target AFR table:

    Suggested VE = Current VE × (Measured AFR / Target AFR)

A lean reading raises VE; a rich reading lowers VE. Samples accumulate across
drives. Corrections are weighted, require repeated observations, and are
clamped before being blended into the active table.

Best data comes from steady throttle. Acceleration enrichment, deceleration
fuel cut, cold operation, sensor faults, and wheelspin can mislead a learner.
Apply small changes, inspect the table for cliffs, then validate another drive.
""",
    "K24 Configuration Ideas": """
Document the exact engine code; K24A, K24A2, K24A4, K24A8, K24Z variants differ.

NATURALLY ASPIRATED
• Correct injector characterization matters more than oversized injectors.
• Intake manifold, throttle body, header, cams, and compression change VE.
• Cam angle strongly changes cylinder filling; tune VTC regions deliberately.

BOOSTED
• Use a MAP sensor and table axis that cover expected boost with margin.
• Verify fuel system capacity, intercooling, plugs, crankcase ventilation,
  wastegate control, and overboost protection before load tuning.
• A knock-safe ignition strategy requires reliable knock detection and expert
  validation; AFR feedback alone cannot optimize spark timing.
""",
    "Reading the Dashboard": """
RPM: engine speed.
MAP: manifold absolute pressure in kPa.
TPS: throttle position percentage.
AFR: measured air/fuel ratio and current target.
CLT / IAT: coolant and intake-air temperatures.
Spark: current commanded ignition advance.
Battery: ECU supply voltage.
Rate: successfully processed realtime packets per second.

The trend plots retain a short in-memory window only. Full accepted learning
samples are stored in SQLite and remain available after restarting the app.
""",
    "Safety and Better Results": """
• Never tune while operating the laptop yourself. Use a passenger/operator.
• Use a load-bearing dyno for high-load and ignition calibration.
• Stop for lean AFR, knock, overheating, oil-pressure loss, or fuel-pressure loss.
• Smooth neighboring cells and avoid large discontinuities.
• Change one system at a time and keep notes for every session.
• Heat soak, weather, fuel blend, altitude, and sensor age affect measurements.

This tool assists data collection and conservative fuel correction. It cannot
detect every unsafe condition and does not replace an experienced calibrator.
""",
}
