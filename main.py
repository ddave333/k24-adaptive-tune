"""
K24 Adaptive Tune — local Speeduino learning agent.

Run from the project root:
    python main.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is importable when launched as a script
ProjectRoot = Path(__file__).resolve().parent
if str(ProjectRoot) not in sys.path:
    sys.path.insert(0, str(ProjectRoot))


def Main() -> None:
    Parser = argparse.ArgumentParser(description="K24 Adaptive Tune for Speeduino")
    Parser.add_argument(
        "--cli-demo",
        action="store_true",
        help="Run a short simulated learning pass without the GUI",
    )
    Args = Parser.parse_args()

    if Args.cli_demo:
        RunCLIDemo()
        return

    from src.ui.app import RunApp

    RunApp()


def RunCLIDemo() -> None:
    from src.agent.learner import AdaptiveTuneAgent
    from src.db.store import LocalStore
    from src.ecu.speeduino import SpeeduinoClient

    print("K24 Adaptive Tune — simulated Speeduino learning demo")
    Store = LocalStore()
    Client = SpeeduinoClient(PortName="SIM", Simulate=True)
    Client.Connect()
    Agent = AdaptiveTuneAgent(Store, Client)
    Agent.StartSession("SIM", Notes="CLI demo")

    for Index in range(120):
        Data = Client.ReadRealtime()
        Sample = Agent.ProcessRealtime(Data)
        if Sample and Index % 20 == 0:
            print(
                f"  RPM={Sample['RPM']:.0f} MAP={Sample['MAP']:.0f} "
                f"AFR={Sample['AFR']:.1f}/{Sample['TargetAFR']:.1f} "
                f"VE {Sample['CurrentVE']:.1f}->{Sample['SuggestedVE']:.1f}"
            )

    Summary = Agent.ApplyLearnedCorrections()
    Agent.EndSession()
    Client.Disconnect()
    Store.Close()
    print(
        f"Done. Updated {Summary['CellsUpdated']} cells "
        f"(avg dVE {Summary['AverageDelta']:.2f}). "
        f"DB: {Store.DBPath}"
    )


if __name__ == "__main__":
    Main()
