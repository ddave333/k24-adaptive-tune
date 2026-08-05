"""
Speeduino USB-serial client.

Talks to an Arduino running Speeduino over the secondary/TunerStudio-style
serial protocol: realtime 'A' packets plus page read/write for VE / spark tables.

Page layout defaults match common Speeduino INI maps (16x16). Adjust
PageOffsets in config or subclass if your firmware INI differs.
"""
from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

import serial
import serial.tools.list_ports

from src.config import DefaultBaudRate, DefaultTimeoutSeconds


@dataclass
class RealtimeData:
    Timestamp: float
    RPM: float
    MAP: float
    TPS: float
    AFR: float
    CoolantCelsius: float
    IATCelsius: float
    SparkAdvance: float
    BatteryVoltage: float
    EngineStatus: int


# Speeduino 'A' command returns a fixed-length realtime buffer.
# Offsets below follow common Speeduino firmware layouts; if your build
# differs, update these constants to match your speeduino.ini.
RealtimeLength = 75
OffsetSecl = 0
OffsetStatus = 1
OffsetEngine = 2
OffsetSyncLoss = 3
OffsetMAP = 4          # word
OffsetIAT = 6
OffsetCoolant = 7
OffsetAFRTarget = 8
OffsetAFR = 10
OffsetBattery = 9
OffsetTPS = 15
OffsetRPM = 14         # byte index for low; RPM is word at 14
OffsetSpark = 23


class SpeeduinoClient:
    def __init__(
        self,
        PortName: str,
        BaudRate: int = DefaultBaudRate,
        TimeoutSeconds: float = DefaultTimeoutSeconds,
        Simulate: bool = False,
    ) -> None:
        self.PortName = PortName
        self.BaudRate = BaudRate
        self.TimeoutSeconds = TimeoutSeconds
        self.Simulate = Simulate
        self.SerialPort: Optional[serial.Serial] = None
        self.Lock = threading.Lock()
        self.Connected = False
        self._SimPhase = 0.0

    @staticmethod
    def ListPorts() -> list[str]:
        return [Port.device for Port in serial.tools.list_ports.comports()]

    def Connect(self) -> None:
        if self.Simulate:
            self.Connected = True
            return
        self.SerialPort = serial.Serial(
            port=self.PortName,
            baudrate=self.BaudRate,
            timeout=self.TimeoutSeconds,
            write_timeout=self.TimeoutSeconds,
        )
        time.sleep(2.0)  # allow Arduino USB CDC reset
        self.SerialPort.reset_input_buffer()
        self.Connected = True

    def Disconnect(self) -> None:
        self.Connected = False
        if self.SerialPort and self.SerialPort.is_open:
            self.SerialPort.close()
        self.SerialPort = None

    def ReadRealtime(self) -> RealtimeData:
        if self.Simulate:
            return self._SimulateRealtime()
        if not self.SerialPort or not self.SerialPort.is_open:
            raise RuntimeError("Speeduino serial port is not connected")

        with self.Lock:
            self.SerialPort.reset_input_buffer()
            self.SerialPort.write(b"A")
            Payload = self.SerialPort.read(RealtimeLength)
            if len(Payload) < 40:
                raise TimeoutError(
                    f"Incomplete Speeduino realtime packet ({len(Payload)} bytes)"
                )
            return self._ParseRealtime(Payload)

    def _ParseRealtime(self, Payload: bytes) -> RealtimeData:
        def U8(Index: int) -> int:
            return Payload[Index] if Index < len(Payload) else 0

        def U16(Index: int) -> int:
            if Index + 1 >= len(Payload):
                return 0
            return Payload[Index] | (Payload[Index + 1] << 8)

        RPM = float(U16(14))
        MAP = float(U16(4))
        TPS = float(U8(15))
        AFRRaw = U8(10)
        AFR = AFRRaw / 10.0 if AFRRaw > 0 else 0.0
        Coolant = float(U8(7)) - 40.0
        IAT = float(U8(6)) - 40.0
        Spark = float(U8(23))
        Battery = U8(9) / 10.0
        EngineStatus = U8(2)

        return RealtimeData(
            Timestamp=time.time(),
            RPM=RPM,
            MAP=MAP,
            TPS=TPS,
            AFR=AFR,
            CoolantCelsius=Coolant,
            IATCelsius=IAT,
            SparkAdvance=Spark,
            BatteryVoltage=Battery,
            EngineStatus=EngineStatus,
        )

    def _SimulateRealtime(self) -> RealtimeData:
        import math

        self._SimPhase += 0.08
        RPM = 1800 + 2200 * (0.5 + 0.5 * math.sin(self._SimPhase * 0.35))
        MAP = 45 + 55 * (0.5 + 0.5 * math.sin(self._SimPhase * 0.22))
        Target = 14.7 if MAP < 90 else 13.2
        # Inject a lean/rich wobble so the learner has something to do
        AFR = Target + 0.6 * math.sin(self._SimPhase * 0.9)
        return RealtimeData(
            Timestamp=time.time(),
            RPM=RPM,
            MAP=MAP,
            TPS=20 + 40 * (0.5 + 0.5 * math.sin(self._SimPhase * 0.3)),
            AFR=AFR,
            CoolantCelsius=88.0,
            IATCelsius=32.0,
            SparkAdvance=18.0 + MAP / 40.0,
            BatteryVoltage=13.8,
            EngineStatus=1,
        )

    def ReadPage(self, Page: int, Length: int) -> bytes:
        """Read a Speeduino config page (table storage)."""
        if self.Simulate:
            return bytes([50] * Length)
        if not self.SerialPort or not self.SerialPort.is_open:
            raise RuntimeError("Speeduino serial port is not connected")
        with self.Lock:
            # 'r' + page + offset(2) + length(2) — Speeduino page read
            Offset = 0
            Command = struct.pack("<cBHH", b"r", Page, Offset, Length)
            self.SerialPort.reset_input_buffer()
            self.SerialPort.write(Command)
            Payload = self.SerialPort.read(Length)
            if len(Payload) != Length:
                raise TimeoutError(
                    f"Page read short: got {len(Payload)} expected {Length}"
                )
            return Payload

    def WritePage(self, Page: int, Offset: int, Data: bytes) -> None:
        """Write bytes into a Speeduino config page."""
        if self.Simulate:
            return
        if not self.SerialPort or not self.SerialPort.is_open:
            raise RuntimeError("Speeduino serial port is not connected")
        with self.Lock:
            # 'w' + page + offset(2) + length(2) + payload
            Header = struct.pack("<cBHH", b"w", Page, Offset, len(Data))
            self.SerialPort.write(Header + Data)
            time.sleep(0.05)

    def ReadVETable(self, Page: int = 0, Cells: int = 256) -> list[list[float]]:
        Raw = self.ReadPage(Page, Cells)
        Values = [float(Byte) for Byte in Raw]
        return [Values[Row * 16 : (Row + 1) * 16] for Row in range(16)]

    def WriteVETable(self, Table: list[list[float]], Page: int = 0) -> None:
        Flat: list[int] = []
        for Row in Table:
            for Value in Row:
                Flat.append(int(min(max(round(Value), 0), 255)))
        self.WritePage(Page, 0, bytes(Flat))

    def ReadSparkTable(self, Page: int = 1, Cells: int = 256) -> list[list[float]]:
        Raw = self.ReadPage(Page, Cells)
        # Speeduino often stores spark as degrees * 2 or raw degrees depending on INI
        Values = [float(Byte) for Byte in Raw]
        return [Values[Row * 16 : (Row + 1) * 16] for Row in range(16)]

    def WriteSparkTable(self, Table: list[list[float]], Page: int = 1) -> None:
        Flat: list[int] = []
        for Row in Table:
            for Value in Row:
                Flat.append(int(min(max(round(Value), 0), 60)))
        self.WritePage(Page, 0, bytes(Flat))

    def BurnToFlash(self) -> None:
        """Ask Speeduino to burn current pages to EEPROM."""
        if self.Simulate:
            return
        if not self.SerialPort or not self.SerialPort.is_open:
            raise RuntimeError("Speeduino serial port is not connected")
        with self.Lock:
            self.SerialPort.write(b"b")
            time.sleep(0.2)
