"""Fast local dashboard for Speeduino logging, learning, and setup records."""
from __future__ import annotations

import collections
import threading
import time
import tkinter as tk
from typing import Any, Optional

import customtkinter as CTk

from src.agent.learner import AdaptiveTuneAgent
from src.config import (
    AppTitle,
    AppVersion,
    ChartMaximumPoints,
    PollIntervalSeconds,
    UIRefreshIntervalMilliseconds,
)
from src.db.store import LocalStore
from src.ecu.speeduino import SpeeduinoClient
from src.ui.help_content import HelpTopics


class TrendChart(tk.Canvas):
    """Small dependency-free chart optimized for a fixed number of points."""

    def __init__(self, Parent: tk.Misc, Title: str, Colors: list[str]) -> None:
        super().__init__(
            Parent, height=170, background="#17202b", highlightthickness=0
        )
        self.Title = Title
        self.Colors = Colors

    def Draw(self, Series: list[list[float]]) -> None:
        self.delete("all")
        Width = max(self.winfo_width(), 100)
        Height = max(self.winfo_height(), 80)
        self.create_text(
            10, 10, text=self.Title, fill="#aab6c5", anchor="nw",
            font=("Segoe UI", 10, "bold"),
        )
        Values = [Value for Line in Series for Value in Line]
        if not Values:
            self.create_text(
                Width / 2, Height / 2, text="Waiting for data",
                fill="#607080", font=("Segoe UI", 10),
            )
            return
        Minimum = min(Values)
        Maximum = max(Values)
        Span = max(Maximum - Minimum, 1.0)
        PlotTop = 30
        PlotBottom = Height - 16
        self.create_line(8, PlotBottom, Width - 8, PlotBottom, fill="#344151")
        for Index, Line in enumerate(Series):
            if len(Line) < 2:
                continue
            Points: list[float] = []
            Count = len(Line)
            for PointIndex, Value in enumerate(Line):
                X = 8 + (PointIndex / max(Count - 1, 1)) * (Width - 16)
                Y = PlotBottom - ((Value - Minimum) / Span) * (PlotBottom - PlotTop)
                Points.extend((X, Y))
            self.create_line(
                *Points, fill=self.Colors[Index % len(self.Colors)],
                width=2, smooth=False,
            )
        self.create_text(
            Width - 8, 10, text=f"{Minimum:.1f} – {Maximum:.1f}",
            fill="#778596", anchor="ne", font=("Segoe UI", 9),
        )


class AdaptiveTuneApp(CTk.CTk):
    ProfileFields = [
        ("ProfileName", "Profile name"),
        ("VehicleMake", "Vehicle make"),
        ("VehicleModel", "Vehicle model"),
        ("VehicleYear", "Year"),
        ("VehicleTrim", "Trim / chassis"),
        ("EngineCode", "K24 engine code"),
        ("EngineDisplacement", "Displacement"),
        ("CompressionRatio", "Compression ratio"),
        ("Transmission", "Transmission"),
        ("Drivetrain", "Drivetrain (FWD/RWD/AWD)"),
        ("FinalDriveRatio", "Final drive ratio"),
        ("TireSize", "Driven tire size"),
        ("VehicleWeight", "Vehicle weight"),
        ("FuelType", "Fuel type / ethanol content"),
        ("InjectorSize", "Injector size"),
        ("FuelPressure", "Base fuel pressure"),
        ("MAPSensor", "MAP sensor"),
        ("WidebandSensor", "Wideband / controller"),
        ("ECUBoard", "Speeduino board"),
        ("FirmwareVersion", "Firmware version"),
        ("RevLimit", "Rev limit"),
        ("BoostLimit", "Boost limit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        CTk.set_appearance_mode("dark")
        CTk.set_default_color_theme("dark-blue")
        self.title(f"{AppTitle}  v{AppVersion}")
        self.geometry("1380x850")
        self.minsize(1120, 720)

        self.Store = LocalStore()
        self.Client: Optional[SpeeduinoClient] = None
        self.Agent = AdaptiveTuneAgent(self.Store)
        self.Polling = False
        self.PollThread: Optional[threading.Thread] = None
        self.ProfileID: Optional[int] = None
        self.ProfileEntries: dict[str, CTk.CTkEntry] = {}
        self.SampleTimes: collections.deque[float] = collections.deque(maxlen=500)
        self.RPMHistory: collections.deque[float] = collections.deque(
            maxlen=ChartMaximumPoints
        )
        self.MAPHistory: collections.deque[float] = collections.deque(
            maxlen=ChartMaximumPoints
        )
        self.AFRHistory: collections.deque[float] = collections.deque(
            maxlen=ChartMaximumPoints
        )
        self.TargetAFRHistory: collections.deque[float] = collections.deque(
            maxlen=ChartMaximumPoints
        )
        self.MapNeedsRender = True

        self._BuildUI()
        self._RefreshPorts()
        self._LoadActiveProfile()
        self._RefreshSessions()
        self._SelectHelpTopic("Getting Started")
        self.after(UIRefreshIntervalMilliseconds, self._UITick)

    def _BuildUI(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        Sidebar = CTk.CTkFrame(self, width=245, corner_radius=0)
        Sidebar.grid(row=0, column=0, sticky="nsew")
        Sidebar.grid_propagate(False)
        CTk.CTkLabel(
            Sidebar, text="K24 Adaptive Tune",
            font=CTk.CTkFont(size=21, weight="bold"),
        ).pack(padx=16, pady=(20, 2), anchor="w")
        CTk.CTkLabel(
            Sidebar, text="Speeduino • offline • local DB",
            text_color="#8e9aaa",
        ).pack(padx=16, pady=(0, 18), anchor="w")

        CTk.CTkLabel(Sidebar, text="Speeduino serial port").pack(
            padx=16, anchor="w"
        )
        self.PortVar = tk.StringVar()
        self.PortMenu = CTk.CTkOptionMenu(
            Sidebar, variable=self.PortVar, values=["(none)"]
        )
        self.PortMenu.pack(padx=16, pady=(4, 8), fill="x")
        self.SimulateVar = tk.BooleanVar(value=True)
        CTk.CTkCheckBox(
            Sidebar, text="Simulation mode", variable=self.SimulateVar
        ).pack(padx=16, pady=(0, 10), anchor="w")
        self.ConnectButton = CTk.CTkButton(
            Sidebar, text="Connect", command=self._ToggleConnect
        )
        self.ConnectButton.pack(padx=16, pady=4, fill="x")
        CTk.CTkButton(
            Sidebar, text="Refresh ports", command=self._RefreshPorts,
            fg_color="#354252",
        ).pack(padx=16, pady=4, fill="x")
        self.ConnectionIndicator = CTk.CTkLabel(
            Sidebar, text="● OFFLINE", text_color="#7f8b99",
            font=CTk.CTkFont(weight="bold"),
        )
        self.ConnectionIndicator.pack(padx=16, pady=(14, 4), anchor="w")
        self.RateLabel = CTk.CTkLabel(
            Sidebar, text="0.0 packets/s", text_color="#8e9aaa"
        )
        self.RateLabel.pack(padx=16, anchor="w")
        self.StatusLabel = CTk.CTkLabel(
            Sidebar, text="Ready", wraplength=210, justify="left",
            text_color="#b9c3d0",
        )
        self.StatusLabel.pack(padx=16, pady=(14, 10), anchor="w")
        CTk.CTkLabel(
            Sidebar, text=f"v{AppVersion}  •  data stays local",
            text_color="#617080", font=CTk.CTkFont(size=11),
        ).pack(side="bottom", padx=16, pady=16, anchor="w")

        self.Tabs = CTk.CTkTabview(self, command=self._TabChanged)
        self.Tabs.grid(row=0, column=1, sticky="nsew", padx=12, pady=12)
        for Name in ("Live Dashboard", "Tune Map", "Vehicle Setup", "Sessions", "Help"):
            self.Tabs.add(Name)
        self._BuildLiveTab(self.Tabs.tab("Live Dashboard"))
        self._BuildTuneTab(self.Tabs.tab("Tune Map"))
        self._BuildProfileTab(self.Tabs.tab("Vehicle Setup"))
        self._BuildSessionsTab(self.Tabs.tab("Sessions"))
        self._BuildHelpTab(self.Tabs.tab("Help"))

    def _BuildLiveTab(self, Parent: CTk.CTkFrame) -> None:
        Parent.grid_columnconfigure((0, 1, 2, 3), weight=1)
        Parent.grid_rowconfigure(3, weight=1)
        self.Metrics: dict[str, CTk.CTkLabel] = {}
        Definitions = [
            ("RPM", "RPM", "0"),
            ("MAP", "MAP kPa", "0"),
            ("TPS", "TPS %", "0.0"),
            ("AFR", "AFR / target", "—"),
            ("CoolantCelsius", "Coolant °C", "—"),
            ("IATCelsius", "Intake °C", "—"),
            ("SparkAdvance", "Spark °BTDC", "—"),
            ("BatteryVoltage", "Battery V", "—"),
        ]
        for Index, (Key, Title, Default) in enumerate(Definitions):
            self.Metrics[Key] = self._MetricCard(
                Parent, Index // 4, Index % 4, Title, Default
            )
        Info = CTk.CTkFrame(Parent)
        Info.grid(row=2, column=0, columnspan=4, sticky="ew", padx=4, pady=8)
        Info.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.InfoBasemap = CTk.CTkLabel(Info, text="Basemap: —")
        self.InfoCells = CTk.CTkLabel(Info, text="Learned cells: —")
        self.InfoSession = CTk.CTkLabel(Info, text="Session: —")
        self.InfoEngine = CTk.CTkLabel(Info, text="Engine status: —")
        for Index, Label in enumerate(
            (self.InfoBasemap, self.InfoCells, self.InfoSession, self.InfoEngine)
        ):
            Label.grid(row=0, column=Index, padx=12, pady=10, sticky="w")
        Charts = CTk.CTkFrame(Parent)
        Charts.grid(row=3, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)
        Charts.grid_columnconfigure((0, 1), weight=1)
        Charts.grid_rowconfigure(0, weight=1)
        self.LoadChart = TrendChart(Charts, "RPM (blue) / MAP (orange)", ["#43a5ff", "#f6a33b"])
        self.LoadChart.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        self.AFRChart = TrendChart(Charts, "AFR measured (green) / target (gray)", ["#45d483", "#9aa3ad"])
        self.AFRChart.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)

    def _BuildTuneTab(self, Parent: CTk.CTkFrame) -> None:
        Parent.grid_columnconfigure(0, weight=1)
        Parent.grid_rowconfigure(1, weight=1)
        Controls = CTk.CTkFrame(Parent)
        Controls.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        Buttons = [
            ("Apply learned corrections", self._ApplyLearn, "#1f6aa5"),
            ("Pull from Speeduino", self._PullMap, "#354252"),
            ("Push RAM only", self._PushMap, "#91611f"),
            ("Push + burn EEPROM", self._BurnMap, "#8b3030"),
        ]
        for Text, Command, Color in Buttons:
            CTk.CTkButton(
                Controls, text=Text, command=Command, fg_color=Color
            ).pack(side="left", padx=5, pady=8)
        self.MapInfoLabel = CTk.CTkLabel(
            Controls, text="Review every change before writing the ECU.",
            text_color="#aab4c0",
        )
        self.MapInfoLabel.pack(side="right", padx=12)
        self.VEText = CTk.CTkTextbox(
            Parent, font=CTk.CTkFont(family="Consolas", size=12)
        )
        self.VEText.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

    def _BuildProfileTab(self, Parent: CTk.CTkFrame) -> None:
        Parent.grid_columnconfigure(0, weight=1)
        Parent.grid_rowconfigure(1, weight=1)
        Header = CTk.CTkFrame(Parent)
        Header.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        self.ProfileVar = tk.StringVar(value="New profile")
        self.ProfileMenu = CTk.CTkOptionMenu(
            Header, variable=self.ProfileVar, values=["New profile"],
            command=self._ProfileSelected,
        )
        self.ProfileMenu.pack(side="left", padx=8, pady=8)
        CTk.CTkButton(
            Header, text="New profile", command=self._NewProfile,
            fg_color="#354252",
        ).pack(side="left", padx=4)
        CTk.CTkButton(
            Header, text="Save vehicle setup", command=self._SaveProfile
        ).pack(side="left", padx=4)
        self.ProfileStatus = CTk.CTkLabel(
            Header, text="Document the exact combination used for each tune.",
            text_color="#9da9b8",
        )
        self.ProfileStatus.pack(side="right", padx=12)
        Form = CTk.CTkScrollableFrame(Parent)
        Form.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        Form.grid_columnconfigure((0, 1), weight=1)
        for Index, (Key, LabelText) in enumerate(self.ProfileFields):
            Container = CTk.CTkFrame(Form, fg_color="transparent")
            Container.grid(
                row=Index // 2, column=Index % 2, sticky="ew", padx=8, pady=4
            )
            Container.grid_columnconfigure(0, weight=1)
            CTk.CTkLabel(Container, text=LabelText).grid(
                row=0, column=0, sticky="w"
            )
            Entry = CTk.CTkEntry(Container)
            Entry.grid(row=1, column=0, sticky="ew", pady=(2, 0))
            self.ProfileEntries[Key] = Entry
        NotesRow = (len(self.ProfileFields) + 1) // 2
        CTk.CTkLabel(Form, text="Upgrades completed / parts list").grid(
            row=NotesRow, column=0, sticky="w", padx=8, pady=(10, 2)
        )
        CTk.CTkLabel(Form, text="Build notes / known issues / goals").grid(
            row=NotesRow, column=1, sticky="w", padx=8, pady=(10, 2)
        )
        self.UpgradesText = CTk.CTkTextbox(Form, height=150)
        self.UpgradesText.grid(
            row=NotesRow + 1, column=0, sticky="nsew", padx=8, pady=(0, 10)
        )
        self.NotesText = CTk.CTkTextbox(Form, height=150)
        self.NotesText.grid(
            row=NotesRow + 1, column=1, sticky="nsew", padx=8, pady=(0, 10)
        )

    def _BuildSessionsTab(self, Parent: CTk.CTkFrame) -> None:
        Parent.grid_columnconfigure(0, weight=1)
        Parent.grid_rowconfigure(1, weight=1)
        CTk.CTkButton(
            Parent, text="Refresh session history", command=self._RefreshSessions
        ).grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.SessionsText = CTk.CTkTextbox(
            Parent, font=CTk.CTkFont(family="Consolas", size=12)
        )
        self.SessionsText.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

    def _BuildHelpTab(self, Parent: CTk.CTkFrame) -> None:
        Parent.grid_columnconfigure(0, weight=1)
        Parent.grid_rowconfigure(1, weight=1)
        Header = CTk.CTkFrame(Parent)
        Header.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        CTk.CTkLabel(
            Header, text="Offline Help Center",
            font=CTk.CTkFont(size=18, weight="bold"),
        ).pack(side="left", padx=12, pady=10)
        self.HelpTopicVar = tk.StringVar(value="Getting Started")
        CTk.CTkOptionMenu(
            Header, variable=self.HelpTopicVar, values=list(HelpTopics),
            command=self._SelectHelpTopic, width=250,
        ).pack(side="right", padx=12, pady=8)
        self.HelpText = CTk.CTkTextbox(
            Parent, wrap="word", font=CTk.CTkFont(size=14)
        )
        self.HelpText.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

    def _MetricCard(
        self, Parent: CTk.CTkFrame, Row: int, Column: int,
        Title: str, Default: str,
    ) -> CTk.CTkLabel:
        Card = CTk.CTkFrame(Parent)
        Card.grid(row=Row, column=Column, sticky="ew", padx=4, pady=4)
        CTk.CTkLabel(Card, text=Title, text_color="#8f9baa").pack(
            padx=12, pady=(8, 0), anchor="w"
        )
        Label = CTk.CTkLabel(
            Card, text=Default, font=CTk.CTkFont(size=25, weight="bold")
        )
        Label.pack(padx=12, pady=(0, 8), anchor="w")
        return Label

    def _RefreshPorts(self) -> None:
        Ports = SpeeduinoClient.ListPorts() or ["(none)"]
        self.PortMenu.configure(values=Ports)
        if self.PortVar.get() not in Ports:
            self.PortVar.set(Ports[0])

    def _ToggleConnect(self) -> None:
        self._Disconnect() if self.Polling else self._Connect()

    def _Connect(self) -> None:
        Simulate = bool(self.SimulateVar.get())
        PortName = self.PortVar.get()
        if not Simulate and PortName == "(none)":
            self.StatusLabel.configure(text="Choose a COM port or use simulation.")
            return
        try:
            self.Client = SpeeduinoClient(
                PortName="SIM" if Simulate else PortName, Simulate=Simulate
            )
            self.Client.Connect()
            self.Agent.Client = self.Client
            self.Agent.StartSession("SIM" if Simulate else PortName)
            self.Polling = True
            self.ConnectButton.configure(text="Disconnect", fg_color="#8b3030")
            self.ConnectionIndicator.configure(
                text="● CONNECTED", text_color="#42d17b"
            )
            self.PollThread = threading.Thread(
                target=self._PollLoop, name="SpeeduinoPoll", daemon=True
            )
            self.PollThread.start()
        except Exception as Error:
            self.StatusLabel.configure(text=f"Connection failed: {Error}")
            self.Client = None

    def _Disconnect(self) -> None:
        self.Polling = False
        if self.PollThread and self.PollThread.is_alive():
            self.PollThread.join(timeout=1.5)
        self.Agent.EndSession()
        if self.Client:
            self.Client.Disconnect()
        self.Client = None
        self.Agent.Client = None
        self.ConnectButton.configure(text="Connect", fg_color="#1f6aa5")
        self.ConnectionIndicator.configure(
            text="● OFFLINE", text_color="#7f8b99"
        )
        self._RefreshSessions()

    def _PollLoop(self) -> None:
        while self.Polling and self.Client:
            StartedAt = time.perf_counter()
            try:
                Data = self.Client.ReadRealtime()
                Sample = self.Agent.ProcessRealtime(Data)
                self.SampleTimes.append(time.monotonic())
                self.RPMHistory.append(Data.RPM)
                self.MAPHistory.append(Data.MAP)
                self.AFRHistory.append(Data.AFR)
                TargetAFR = (
                    float(Sample["TargetAFR"]) if Sample
                    else (self.TargetAFRHistory[-1] if self.TargetAFRHistory else 14.7)
                )
                self.TargetAFRHistory.append(TargetAFR)
            except Exception as Error:
                self.Agent.LastStatus = f"Serial read error: {Error}"
                time.sleep(0.20)
            Elapsed = time.perf_counter() - StartedAt
            Delay = PollIntervalSeconds - Elapsed
            if Delay > 0:
                time.sleep(Delay)

    def _UITick(self) -> None:
        if not self.winfo_exists():
            return
        Dashboard = self.Agent.GetDashboard()
        Realtime = Dashboard.get("LastRealtime") or {}
        Sample = Dashboard.get("LastSample") or {}
        if Realtime:
            self.Metrics["RPM"].configure(text=f"{Realtime['RPM']:.0f}")
            self.Metrics["MAP"].configure(text=f"{Realtime['MAP']:.0f}")
            self.Metrics["TPS"].configure(text=f"{Realtime['TPS']:.1f}")
            Target = Sample.get("TargetAFR")
            AFRText = f"{Realtime['AFR']:.2f}"
            if Target:
                AFRText += f" / {Target:.2f}"
            self.Metrics["AFR"].configure(text=AFRText)
            self.Metrics["CoolantCelsius"].configure(
                text=f"{Realtime['CoolantCelsius']:.1f}"
            )
            self.Metrics["IATCelsius"].configure(
                text=f"{Realtime['IATCelsius']:.1f}"
            )
            self.Metrics["SparkAdvance"].configure(
                text=f"{Realtime['SparkAdvance']:.1f}"
            )
            self.Metrics["BatteryVoltage"].configure(
                text=f"{Realtime['BatteryVoltage']:.1f}"
            )
            self.InfoEngine.configure(
                text=f"Engine status: {Realtime['EngineStatus']}"
            )
        Now = time.monotonic()
        while self.SampleTimes and self.SampleTimes[0] < Now - 1.0:
            self.SampleTimes.popleft()
        self.RateLabel.configure(text=f"{len(self.SampleTimes):.1f} packets/s")
        self.InfoBasemap.configure(text=f"Basemap: {Dashboard['BasemapName']}")
        self.InfoCells.configure(
            text=f"Learned cells: {Dashboard['ConfidentCells']} / "
            f"{Dashboard['CellLearnCount']}"
        )
        SessionText = Dashboard["SessionID"] or "—"
        self.InfoSession.configure(text=f"Session: {SessionText}")
        self.StatusLabel.configure(text=Dashboard["LastStatus"])
        self.LoadChart.Draw([list(self.RPMHistory), list(self.MAPHistory)])
        self.AFRChart.Draw(
            [list(self.AFRHistory), list(self.TargetAFRHistory)]
        )
        if self.MapNeedsRender and self.Tabs.get() == "Tune Map":
            self._RenderVETable(Dashboard)
            self.MapNeedsRender = False
        self.after(UIRefreshIntervalMilliseconds, self._UITick)

    def _RenderVETable(self, Dashboard: dict[str, Any]) -> None:
        RPMBins = Dashboard["RPMBins"]
        MAPBins = Dashboard["MAPBins"]
        VETable = Dashboard["VETable"]
        Lines = ["MAP\\RPM " + " ".join(f"{int(RPM):>6}" for RPM in RPMBins)]
        for RowIndex, Row in enumerate(VETable):
            Lines.append(
                f"{int(MAPBins[RowIndex]):>7} "
                + " ".join(f"{Value:6.1f}" for Value in Row)
            )
        self.VEText.delete("1.0", "end")
        self.VEText.insert("1.0", "\n".join(Lines))

    def _TabChanged(self) -> None:
        Selected = self.Tabs.get()
        if Selected == "Tune Map":
            self.MapNeedsRender = True
        elif Selected == "Sessions":
            self._RefreshSessions()

    def _ApplyLearn(self) -> None:
        Summary = self.Agent.ApplyLearnedCorrections()
        self.MapNeedsRender = True
        self.MapInfoLabel.configure(
            text=f"{Summary['CellsUpdated']} cells changed; "
            f"average dVE {Summary['AverageDelta']:.2f}"
        )

    def _PushMap(self) -> None:
        self._RunECUAction(lambda: self.Agent.PushBasemapToSpeeduino(False))

    def _PullMap(self) -> None:
        self._RunECUAction(self.Agent.PullBasemapFromSpeeduino)
        self.MapNeedsRender = True

    def _BurnMap(self) -> None:
        self._RunECUAction(lambda: self.Agent.PushBasemapToSpeeduino(True))

    def _RunECUAction(self, Action: Any) -> None:
        try:
            Action()
            self.MapInfoLabel.configure(text=self.Agent.LastStatus)
        except Exception as Error:
            self.MapInfoLabel.configure(text=f"ECU action failed: {Error}")

    def _SaveProfile(self) -> None:
        Profile = {
            Key: Entry.get() for Key, Entry in self.ProfileEntries.items()
        }
        Profile["ProfileID"] = self.ProfileID
        Profile["Upgrades"] = self.UpgradesText.get("1.0", "end").strip()
        Profile["Notes"] = self.NotesText.get("1.0", "end").strip()
        if not Profile["ProfileName"]:
            self.ProfileStatus.configure(text="Profile name is required.")
            return
        self.ProfileID = self.Store.SaveVehicleProfile(Profile)
        self.ProfileStatus.configure(text=f"Saved profile #{self.ProfileID}.")
        self._RefreshProfileMenu()

    def _LoadActiveProfile(self) -> None:
        Profile = self.Store.GetActiveVehicleProfile()
        if Profile:
            self._LoadProfile(Profile)
        self._RefreshProfileMenu()

    def _RefreshProfileMenu(self) -> None:
        Profiles = self.Store.ListVehicleProfiles()
        Names = [str(Profile["ProfileName"]) for Profile in Profiles]
        self.ProfileMenu.configure(values=["New profile", *Names])
        if self.ProfileID:
            Match = next(
                (Profile for Profile in Profiles
                 if int(Profile["ProfileID"]) == self.ProfileID),
                None,
            )
            if Match:
                self.ProfileVar.set(str(Match["ProfileName"]))

    def _ProfileSelected(self, Name: str) -> None:
        if Name == "New profile":
            self._NewProfile()
            return
        Profile = next(
            (
                Item for Item in self.Store.ListVehicleProfiles()
                if str(Item["ProfileName"]) == Name
            ),
            None,
        )
        if Profile:
            self._LoadProfile(Profile)

    def _LoadProfile(self, Profile: dict[str, Any]) -> None:
        self.ProfileID = int(Profile["ProfileID"])
        for Key, Entry in self.ProfileEntries.items():
            Entry.delete(0, "end")
            Entry.insert(0, str(Profile.get(Key) or ""))
        self.UpgradesText.delete("1.0", "end")
        self.UpgradesText.insert("1.0", str(Profile.get("Upgrades") or ""))
        self.NotesText.delete("1.0", "end")
        self.NotesText.insert("1.0", str(Profile.get("Notes") or ""))
        self.ProfileVar.set(str(Profile["ProfileName"]))

    def _NewProfile(self) -> None:
        self.ProfileID = None
        for Entry in self.ProfileEntries.values():
            Entry.delete(0, "end")
        self.UpgradesText.delete("1.0", "end")
        self.NotesText.delete("1.0", "end")
        self.ProfileVar.set("New profile")
        self.ProfileStatus.configure(text="Enter the new vehicle and engine setup.")

    def _RefreshSessions(self) -> None:
        Sessions = self.Store.ListSessions(100)
        Lines = [
            "ID     STARTED              DURATION   PORT      SAMPLES   NOTES",
            "—" * 78,
        ]
        for Session in Sessions:
            StartedAt = float(Session["StartedAt"])
            EndedAt = Session.get("EndedAt")
            Duration = (
                f"{float(EndedAt) - StartedAt:7.0f}s" if EndedAt else " running"
            )
            DateText = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(StartedAt)
            )
            Lines.append(
                f"{Session['SessionID']:>4}   {DateText}   {Duration:>8}   "
                f"{str(Session.get('PortName') or ''):<8}   "
                f"{Session['SampleCount']:>7}   {Session.get('Notes') or ''}"
            )
        self.SessionsText.delete("1.0", "end")
        self.SessionsText.insert("1.0", "\n".join(Lines))

    def _SelectHelpTopic(self, Topic: str) -> None:
        self.HelpText.delete("1.0", "end")
        self.HelpText.insert("1.0", HelpTopics.get(Topic, "Topic not found.").strip())

    def OnClose(self) -> None:
        self.Polling = False
        if self.PollThread and self.PollThread.is_alive():
            self.PollThread.join(timeout=1.5)
        self.Agent.EndSession()
        if self.Client:
            self.Client.Disconnect()
        self.Store.Close()
        self.destroy()


def RunApp() -> None:
    App = AdaptiveTuneApp()
    App.protocol("WM_DELETE_WINDOW", App.OnClose)
    App.mainloop()
