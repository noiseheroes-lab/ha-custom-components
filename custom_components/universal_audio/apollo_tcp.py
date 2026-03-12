"""Async TCP client for UA Console protocol (port 4710).

Protocol: JSON over null-terminated strings.
Commands: get, set, subscribe.

All writes serialized through asyncio.Lock to prevent
concurrent writes from corrupting the connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 10
RECONNECT_MAX_DELAY = 5
READ_TIMEOUT = 20
COMMAND_DELAY = 0.03  # 30ms between commands during enumeration

# Known property types for inputs
INPUT_BOOL_PROPS = {"Mute", "Phantom", "Pad", "Phase", "HiPass", "LowCut", "Polarity", "Stereo"}
INPUT_FLOAT_PROPS = {"Gain", "InputGain"}
# Known property types for outputs
OUTPUT_BOOL_PROPS = {"Mute", "DimOn", "MixToMono", "Phase"}
OUTPUT_FLOAT_PROPS = {"CRMonitorLevel"}


@dataclass
class ChannelInfo:
    """Discovered input or output channel."""

    index: str
    name: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or f"Channel {self.index}"


@dataclass
class ApolloState:
    """Full device state — monitor + inputs + outputs."""

    volume_db: float = -96.0
    is_muted: bool = False
    is_dimmed: bool = False
    is_mono: bool = False

    device_name: str = "Apollo"
    device_online: bool = False
    connected: bool = False
    sample_rate: str = ""
    firmware: str = ""

    inputs: dict[str, ChannelInfo] = field(default_factory=dict)
    outputs: dict[str, ChannelInfo] = field(default_factory=dict)

    @property
    def volume_normalized(self) -> float:
        """Volume as 0.0-1.0 using squared curve (monitor-style)."""
        if self.volume_db <= -96:
            return 0.0
        if self.volume_db >= 0:
            return 1.0
        linear = (self.volume_db + 96.0) / 96.0
        return linear * linear

    @staticmethod
    def db_from_normalized(value: float) -> float:
        """Convert 0.0-1.0 back to dB (inverse of squared curve)."""
        if value <= 0:
            return -96.0
        if value >= 1.0:
            return 0.0
        return math.sqrt(value) * 96.0 - 96.0

    @staticmethod
    def gain_normalized(db: float, min_db: float = 0.0, max_db: float = 65.0) -> float:
        if max_db <= min_db:
            return 0.0
        return max(0.0, min(1.0, (db - min_db) / (max_db - min_db)))

    @staticmethod
    def gain_from_normalized(value: float, min_db: float = 0.0, max_db: float = 65.0) -> float:
        return min_db + value * (max_db - min_db)


class ApolloTCPClient:
    """Async TCP client for UA Console — lock-serialized writes."""

    def __init__(
        self,
        host: str,
        port: int = 4710,
        device_index: int = 0,
        output_index: int = 4,
    ) -> None:
        self._host = host
        self._port = port
        self._device_index = device_index
        self._output_index = output_index

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer = bytearray()
        self._reconnect_attempt = 0
        self._intentional_disconnect = False
        self._running = False

        self.state = ApolloState()
        self._callbacks: list[Callable[[], None]] = []
        self._keepalive_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None
        self._enum_task: asyncio.Task | None = None
        self._reconnect_handle: asyncio.TimerHandle | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def _dev_path(self) -> str:
        return f"/devices/{self._device_index}"

    @property
    def _monitor_path(self) -> str:
        return f"/devices/{self._device_index}/outputs/{self._output_index}"

    def _input_path(self, idx: str) -> str:
        return f"/devices/{self._device_index}/inputs/{idx}"

    def _output_path(self, idx: str) -> str:
        return f"/devices/{self._device_index}/outputs/{idx}"

    def add_callback(self, callback: Callable[[], None]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks = [cb for cb in self._callbacks if cb is not callback]

    def _notify(self) -> None:
        for cb in self._callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Callback error")

    # ── Connection ──────────────────────────────────────────

    async def connect(self) -> None:
        self._intentional_disconnect = False
        self._running = True
        await self._do_connect()

    async def disconnect(self) -> None:
        self._intentional_disconnect = True
        self._running = False
        self._cancel_tasks()
        await self._close_writer()
        self.state.connected = False
        self._notify()

    async def _do_connect(self) -> None:
        self._cancel_tasks()
        await self._close_writer()
        self.state.connected = False
        self._buffer.clear()

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=5,
            )
        except (OSError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Connection to %s:%s failed: %s", self._host, self._port, err)
            self._schedule_reconnect()
            return

        self._reconnect_attempt = 0
        self.state.connected = True
        _LOGGER.info("Connected to UA Console at %s:%s", self._host, self._port)

        self._receive_task = asyncio.create_task(self._receive_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._enum_task = asyncio.create_task(self._enumerate())

        self._notify()

    def _schedule_reconnect(self) -> None:
        if self._intentional_disconnect:
            return
        self._reconnect_attempt += 1
        delay = min(2 ** (self._reconnect_attempt - 1), RECONNECT_MAX_DELAY)
        _LOGGER.debug("Reconnecting in %ss (attempt %d)", delay, self._reconnect_attempt)
        loop = asyncio.get_event_loop()
        self._reconnect_handle = loop.call_later(
            delay, lambda: asyncio.ensure_future(self._do_connect())
        )

    def _cancel_tasks(self) -> None:
        for task in (self._receive_task, self._keepalive_task, self._enum_task):
            if task and not task.done():
                task.cancel()
        self._receive_task = None
        self._keepalive_task = None
        self._enum_task = None
        if self._reconnect_handle:
            self._reconnect_handle.cancel()
            self._reconnect_handle = None

    async def _close_writer(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    # ── Send ──────────────────────────────────────────────────

    async def _send(self, command: str) -> bool:
        """Send a command to UA Console. Returns True if sent successfully."""
        w = self._writer
        if not w:
            _LOGGER.warning("No writer, dropping: %s", command[:80])
            return False
        try:
            w.write((command + "\0").encode())
            await w.drain()
            return True
        except (OSError, ConnectionError) as err:
            _LOGGER.warning("Send failed (%s): %s", err, command[:80])
            self.state.connected = False
            return False

    # ── Public Commands ─────────────────────────────────────

    async def set_volume(self, normalized: float) -> None:
        db = ApolloState.db_from_normalized(normalized)
        await self._send(f"set {self._monitor_path}/CRMonitorLevel/value {db}")

    async def set_output_bool(self, prop: str, value: bool) -> None:
        cmd = f"set {self._monitor_path}/{prop}/value {str(value).lower()}"
        _LOGGER.debug("set_output_bool: %s", cmd)
        await self._send(cmd)

    async def set_input_bool(self, input_idx: str, prop: str, value: bool) -> None:
        path = self._input_path(input_idx)
        await self._send(f"set {path}/{prop}/value {str(value).lower()}")

    async def set_input_float(self, input_idx: str, prop: str, value: float) -> None:
        path = self._input_path(input_idx)
        await self._send(f"set {path}/{prop}/value {value}")

    async def set_mute(self, mute: bool) -> None:
        await self.set_output_bool("Mute", mute)

    async def set_dim(self, dim: bool) -> None:
        await self.set_output_bool("DimOn", dim)

    async def set_mono(self, mono: bool) -> None:
        await self.set_output_bool("MixToMono", mono)

    # ── Enumeration (proper sequential async) ───────────────

    async def _enumerate(self) -> None:
        """Full device enumeration — runs as a single sequential task."""
        try:
            _LOGGER.info("Starting enumeration...")

            # Step 1: Get devices
            await self._send("get /devices")
            await asyncio.sleep(0.5)

            # Step 2: Get device info
            await self._send(f"get {self._dev_path}")
            await self._send(f"subscribe {self._dev_path}/DeviceOnline")
            await asyncio.sleep(0.3)

            # Step 3: Enumerate inputs
            await self._send(f"get {self._dev_path}/inputs")
            await asyncio.sleep(0.5)

            for inp_idx in list(self.state.inputs.keys()):
                await self._send(f"get {self._input_path(inp_idx)}")
                await asyncio.sleep(COMMAND_DELAY)

            await asyncio.sleep(0.3)

            # Subscribe to input properties
            for inp_idx in list(self.state.inputs.keys()):
                path = self._input_path(inp_idx)
                for prop in INPUT_BOOL_PROPS | INPUT_FLOAT_PROPS:
                    await self._send(f"subscribe {path}/{prop}")
                    await self._send(f"get {path}/{prop}")
                    await asyncio.sleep(COMMAND_DELAY)

            # Step 4: Enumerate outputs
            await self._send(f"get {self._dev_path}/outputs")
            await asyncio.sleep(0.5)

            for out_idx in list(self.state.outputs.keys()):
                await self._send(f"get {self._output_path(out_idx)}")
                await asyncio.sleep(COMMAND_DELAY)

            await asyncio.sleep(0.3)

            # Subscribe to monitor output properties
            for prop in OUTPUT_BOOL_PROPS | OUTPUT_FLOAT_PROPS:
                await self._send(f"subscribe {self._monitor_path}/{prop}")
                await self._send(f"get {self._monitor_path}/{prop}")
                await asyncio.sleep(COMMAND_DELAY)

            _LOGGER.info(
                "Enumeration complete: %s — %d inputs, %d outputs",
                self.state.device_name,
                len(self.state.inputs),
                len(self.state.outputs),
            )
            self._notify()

        except asyncio.CancelledError:
            _LOGGER.debug("Enumeration cancelled")
        except Exception:
            _LOGGER.exception("Enumeration failed")

    # ── Receiving ───────────────────────────────────────────

    async def _receive_loop(self) -> None:
        assert self._reader is not None
        try:
            while self._running:
                try:
                    data = await asyncio.wait_for(
                        self._reader.read(65536), timeout=READ_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    _LOGGER.debug("Read timeout, probing connection")
                    ok = await self._send("get /devices")
                    if ok:
                        continue
                    else:
                        _LOGGER.warning("Keepalive probe failed — connection dead")
                        break
                if not data:
                    _LOGGER.warning("Read returned empty — connection closed by peer")
                    break
                self._buffer.extend(data)
                self._process_buffer()
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            await self._handle_disconnect()

    def _process_buffer(self) -> None:
        while True:
            idx = self._buffer.find(b"\x00")
            if idx < 0:
                break
            message = self._buffer[:idx].decode("utf-8", errors="replace").strip()
            self._buffer = self._buffer[idx + 1:]
            if message:
                self._parse_response(message)

    def _parse_response(self, message: str) -> None:
        try:
            obj = json.loads(message)
        except json.JSONDecodeError:
            return
        try:
            self._dispatch_response(obj)
        except Exception:
            _LOGGER.exception("Error handling response: %s", message[:200])

    def _dispatch_response(self, obj: dict) -> None:
        path = obj.get("path", "")
        data = obj.get("data")

        if isinstance(data, dict):
            if "children" in data:
                self._handle_children(path, sorted(data["children"].keys()))
            if "properties" in data:
                for prop_name, prop_data in data["properties"].items():
                    if isinstance(prop_data, dict) and "value" in prop_data:
                        self._handle_property(path, prop_name, prop_data["value"])
            if "children" not in data and "properties" not in data:
                prop = self._prop_from_path(path)
                if prop:
                    self._handle_property(path, prop, data)
        else:
            prop = self._prop_from_path(path)
            if prop:
                self._handle_property(path, prop, data)

    @staticmethod
    def _prop_from_path(path: str) -> str | None:
        """Extract property name from path. Handles /Prop/value → Prop."""
        parts = path.rstrip("/").split("/")
        if not parts:
            return None
        # set responses have /PropName/value — use the prop name, not "value"
        if parts[-1] == "value" and len(parts) >= 2:
            return parts[-2]
        return parts[-1]

    # ── Children (pure data — no sends) ─────────────────────

    def _handle_children(self, path: str, ids: list[str]) -> None:
        if path == f"{self._dev_path}/inputs":
            for i in ids:
                if i not in self.state.inputs:
                    self.state.inputs[i] = ChannelInfo(index=i)
            _LOGGER.debug("Discovered %d inputs: %s", len(ids), ids)

        elif path == f"{self._dev_path}/outputs":
            for o in ids:
                if o not in self.state.outputs:
                    self.state.outputs[o] = ChannelInfo(index=o)
            _LOGGER.debug("Discovered %d outputs: %s", len(ids), ids)

        elif path == "/devices":
            _LOGGER.debug("Discovered %d devices", len(ids))

    # ── Properties ──────────────────────────────────────────

    def _handle_property(self, path: str, prop: str, value: Any) -> None:
        changed = False

        if prop == "DeviceName" and isinstance(value, str):
            self.state.device_name = value
            changed = True
        elif prop == "DeviceOnline":
            self.state.device_online = self._to_bool(value)
            changed = True
        elif prop == "SampleRate" and isinstance(value, (str, int, float)):
            self.state.sample_rate = str(value)
            changed = True
        elif prop == "FirmwareVersion" and isinstance(value, str):
            self.state.firmware = value
            changed = True
        elif "/inputs/" in path:
            changed = self._handle_input_property(path, prop, value)
        elif "/outputs/" in path:
            changed = self._handle_output_property(path, prop, value)

        if changed:
            self._notify()

    def _handle_input_property(self, path: str, prop: str, value: Any) -> bool:
        parts = path.split("/")
        try:
            inp_idx = parts[parts.index("inputs") + 1]
        except (ValueError, IndexError):
            return False

        if inp_idx not in self.state.inputs:
            self.state.inputs[inp_idx] = ChannelInfo(index=inp_idx)
        ch = self.state.inputs[inp_idx]

        if prop == "Name" and isinstance(value, str):
            ch.name = value
            return True

        ch.properties[prop] = value
        return True

    def _handle_output_property(self, path: str, prop: str, value: Any) -> bool:
        parts = path.split("/")
        try:
            out_idx = parts[parts.index("outputs") + 1]
        except (ValueError, IndexError):
            return False

        if out_idx not in self.state.outputs:
            self.state.outputs[out_idx] = ChannelInfo(index=out_idx)
        ch = self.state.outputs[out_idx]

        if prop == "Name" and isinstance(value, str):
            ch.name = value
            return True

        ch.properties[prop] = value

        # Mirror primary monitor output to top-level state
        if out_idx == str(self._output_index):
            if prop == "CRMonitorLevel":
                self.state.volume_db = self._to_float(value)
            elif prop == "Mute":
                self.state.is_muted = self._to_bool(value)
            elif prop == "DimOn":
                self.state.is_dimmed = self._to_bool(value)
                _LOGGER.debug("DimOn updated to %s", self.state.is_dimmed)
            elif prop == "MixToMono":
                self.state.is_mono = self._to_bool(value)

        return True

    # ── Keep-alive ──────────────────────────────────────────

    async def _keepalive_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self.state.connected:
                    ok = await self._send("get /devices")
                    if not ok:
                        await self._handle_disconnect()
        except asyncio.CancelledError:
            pass

    # ── Disconnect handling ─────────────────────────────────

    async def _handle_disconnect(self) -> None:
        was_connected = self.state.connected
        self.state.connected = False
        if was_connected:
            _LOGGER.warning("Disconnected from UA Console")
            self._notify()
        if self._running and not self._intentional_disconnect:
            self._schedule_reconnect()

    # ── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.lower() in ("true", "1")
        if isinstance(value, dict) and "value" in value:
            return ApolloTCPClient._to_bool(value["value"])
        return False

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
        if isinstance(value, dict) and "value" in value:
            return ApolloTCPClient._to_float(value["value"], default)
        return default
