"""BLE client for Daikin Madoka energy data via UART protocol."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from dataclasses import dataclass, field

from bleak import BleakClient, BleakScanner

_LOGGER = logging.getLogger(__name__)

# BLE UART characteristics
UART_SERVICE = "2141e110-213a-11e6-b67b-9e71128cae77"
UART_NOTIFY = "2141e111-213a-11e6-b67b-9e71128cae77"
UART_WRITE = "2141e112-213a-11e6-b67b-9e71128cae77"

# UART protocol constants
MAX_CHUNK = 20
RESPONSE_TIMEOUT = 5.0

# Command IDs (from reverse-engineered Madoka Assistant APK)
CMD_ENERGY_CONSUMPTION = 0x0120  # functionId 288
CMD_ENTER_PRIVILEGED = 0x4112   # Enter privileged mode

# Energy parameter IDs within CMD_ENERGY_CONSUMPTION
PARAM_TODAY = 0x40
PARAM_YESTERDAY = 0x41
PARAM_THIS_WEEK = 0x42
PARAM_LAST_WEEK = 0x43
PARAM_THIS_YEAR = 0x44
PARAM_LAST_YEAR = 0x45

# Energy data: uint32 LE values in 0.1 kWh units
# Daily: 13 uint32 = total + 12 two-hour slots
# Weekly: 8 uint32 = total + 7 days
# Yearly: 13 uint32 = total + 12 months
ENERGY_SCALE = 0.1  # Values are in 0.1 kWh


@dataclass
class EnergyPeriod:
    """Energy consumption for a single period."""

    total: float | None = None
    consumption: list[float | None] = field(default_factory=list)


@dataclass
class EnergyData:
    """Energy consumption data with current and previous period."""

    current: EnergyPeriod = field(default_factory=EnergyPeriod)
    previous: EnergyPeriod = field(default_factory=EnergyPeriod)


@dataclass
class MadokaData:
    """All data from a Madoka device."""

    day_energy: EnergyData = field(default_factory=EnergyData)
    week_energy: EnergyData = field(default_factory=EnergyData)
    year_energy: EnergyData = field(default_factory=EnergyData)


def _parse_energy_param(raw: bytes, n_slots: int) -> EnergyPeriod:
    """Parse a single energy parameter (uint32 LE array in 0.1 kWh units).

    Format: [total, slot_0, slot_1, ..., slot_n-1] as uint32 LE values.
    Expected size: (1 + n_slots) * 4 bytes.
    """
    period = EnergyPeriod()
    if not raw or len(raw) < 4:
        return period

    n_values = len(raw) // 4
    values = [struct.unpack_from("<I", raw, i * 4)[0] for i in range(n_values)]

    period.total = round(values[0] * ENERGY_SCALE, 1)
    period.consumption = [
        round(v * ENERGY_SCALE, 1) for v in values[1 : 1 + n_slots]
    ]
    return period


class _UARTTransport:
    """Low-level UART-over-BLE transport for Madoka protocol."""

    def __init__(self, client: BleakClient) -> None:
        self._client = client
        self._event = asyncio.Event()
        self._response = bytearray()
        self._chunks: list[bytearray] = []
        self._last_cid: int | None = None

    def _handler(self, _sender: object, data: bytearray) -> None:
        if len(data) < 2:
            return
        cid = data[0]
        if self._last_cid is not None and cid <= self._last_cid:
            self._chunks.clear()
        self._last_cid = cid
        self._chunks.append(data)
        if self._chunks:
            expected = math.ceil(self._chunks[0][1] / MAX_CHUNK)
            if len(self._chunks) == expected:
                out = bytearray()
                for c in self._chunks:
                    out.extend(c[1:])
                self._chunks.clear()
                self._last_cid = None
                self._response = out
                self._event.set()

    async def start(self) -> None:
        await self._client.start_notify(UART_NOTIFY, self._handler)

    async def stop(self) -> None:
        try:
            await self._client.stop_notify(UART_NOTIFY)
        except Exception:
            pass

    async def command(
        self, cmd_id: int, params: bytes = b"\x00\x00"
    ) -> dict[int, bytearray]:
        """Send a UART command and return parsed response parameters."""
        cmd = cmd_id.to_bytes(2, "big")
        payload = bytearray([0x00, 0x00]) + bytearray(cmd) + bytearray(params)
        payload[0] = len(payload)

        # Split into 20-byte BLE chunks
        chunks = []
        idx = 0
        while True:
            chunk = payload[idx * 19 : min((idx + 1) * 19, len(payload))]
            chunks.append(bytearray(idx.to_bytes(1, "big")) + chunk)
            idx += 1
            if idx * 19 >= len(payload):
                break

        self._event.clear()
        self._response = bytearray()
        self._chunks.clear()
        self._last_cid = None

        for chunk in chunks:
            await self._client.write_gatt_char(UART_WRITE, chunk, response=False)

        try:
            await asyncio.wait_for(self._event.wait(), timeout=RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            return {}

        return self._parse_params(self._response)

    @staticmethod
    def _parse_params(data: bytearray) -> dict[int, bytearray]:
        if len(data) < 4:
            return {}
        length = data[0]
        params: dict[int, bytearray] = {}
        i = 4  # Skip length + 3-byte function ID
        while i < min(len(data), length):
            if i + 1 >= len(data):
                break
            pid = data[i]
            psize = data[i + 1]
            if psize == 0xFF:
                psize = 0
            if i + 2 + psize > len(data):
                params[pid] = data[i + 2 :]
                break
            params[pid] = data[i + 2 : i + 2 + psize]
            i += 2 + psize
        return params


class MadokaBleClient:
    """BLE client for reading Madoka energy data via UART protocol."""

    def __init__(self, address: str) -> None:
        self.address = address

    async def read_data(self) -> MadokaData:
        """Connect to device and read all energy data."""
        data = MadokaData()

        device = await BleakScanner.find_device_by_address(self.address, timeout=15)
        if not device:
            raise ConnectionError(f"Device {self.address} not found")

        async with BleakClient(device) as client:
            _LOGGER.debug("Connected to Madoka %s", self.address)

            transport = _UARTTransport(client)
            await transport.start()

            try:
                # Enter privileged mode
                await transport.command(
                    CMD_ENTER_PRIVILEGED, bytes([0xFE, 0x01, 0x01])
                )
                await asyncio.sleep(0.3)

                # Read energy consumption - request all params at once
                req_params = bytearray()
                for pid in (
                    PARAM_TODAY,
                    PARAM_YESTERDAY,
                    PARAM_THIS_WEEK,
                    PARAM_LAST_WEEK,
                    PARAM_THIS_YEAR,
                    PARAM_LAST_YEAR,
                ):
                    req_params.extend([pid, 0x00])

                result = await transport.command(CMD_ENERGY_CONSUMPTION, req_params)

                # The bulk request may not return all params with full data
                # due to BLE MTU limits. Fall back to individual requests.
                needs_individual = not result or all(
                    len(v) < 20 for v in result.values() if v
                )

                if needs_individual:
                    _LOGGER.debug("Fetching energy params individually")
                    for pid in (
                        PARAM_TODAY,
                        PARAM_YESTERDAY,
                        PARAM_THIS_WEEK,
                        PARAM_LAST_WEEK,
                        PARAM_THIS_YEAR,
                        PARAM_LAST_YEAR,
                    ):
                        resp = await transport.command(
                            CMD_ENERGY_CONSUMPTION, bytes([pid, 0x00])
                        )
                        if pid in resp and resp[pid]:
                            result[pid] = resp[pid]
                        await asyncio.sleep(0.2)

                # Parse energy data
                # Daily: 12 two-hour slots
                if PARAM_TODAY in result:
                    data.day_energy.current = _parse_energy_param(
                        result[PARAM_TODAY], 12
                    )
                if PARAM_YESTERDAY in result:
                    data.day_energy.previous = _parse_energy_param(
                        result[PARAM_YESTERDAY], 12
                    )
                # Weekly: 7 days
                if PARAM_THIS_WEEK in result:
                    data.week_energy.current = _parse_energy_param(
                        result[PARAM_THIS_WEEK], 7
                    )
                if PARAM_LAST_WEEK in result:
                    data.week_energy.previous = _parse_energy_param(
                        result[PARAM_LAST_WEEK], 7
                    )
                # Yearly: 12 months
                if PARAM_THIS_YEAR in result:
                    data.year_energy.current = _parse_energy_param(
                        result[PARAM_THIS_YEAR], 12
                    )
                if PARAM_LAST_YEAR in result:
                    data.year_energy.previous = _parse_energy_param(
                        result[PARAM_LAST_YEAR], 12
                    )

                _LOGGER.debug(
                    "Energy data: today=%.1f, yesterday=%.1f, "
                    "this_week=%.1f, last_week=%.1f, "
                    "this_year=%.1f, last_year=%.1f kWh",
                    data.day_energy.current.total or 0,
                    data.day_energy.previous.total or 0,
                    data.week_energy.current.total or 0,
                    data.week_energy.previous.total or 0,
                    data.year_energy.current.total or 0,
                    data.year_energy.previous.total or 0,
                )

            finally:
                await transport.stop()

        return data
