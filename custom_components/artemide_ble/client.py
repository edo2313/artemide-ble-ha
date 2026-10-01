"""BLE client for Artemide lights.

Owns one bleak connection (obtained through Home Assistant's Bluetooth stack, so
it transparently routes over an ESP32 Bluetooth Proxy), performs the password
handshake, serializes commands, and encodes/decodes the Playbulb-style protocol.

Protocol reverse-engineered from the official Artemide app.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import (
    BROADCAST_WINDOW,
    COLOR_FLAG,
    NAME_PREFIX,
    NAME_TYPE_INDEX,
    NOTIFY_CHAR_UUID,
    READ_TIMEOUT,
    SCAN_MAX_COUNT,
    SCAN_MAX_GAP,
    SCAN_TIMEOUT,
    WAIT_NEXT_COMMAND,
    WAIT_READ_RC,
    WAIT_READ_RF,
    WRITE_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class NodeState:
    """Decoded state of one lamp node."""

    on: bool
    red: int
    green: int
    blue: int
    white: int = 0


@dataclass
class LampIdentity:
    """What a directly connected lamp reports about itself."""

    node: str
    groups: list[str] = field(default_factory=list)


_HEX4 = re.compile(r"[0-9A-F]{4}")
_MAC_SUFFIX = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def is_artemide_name(name: str | None) -> bool:
    """Match the vendor app filter: "A..." name ending in a MAC address."""
    return bool(
        name
        and name.startswith(NAME_PREFIX)
        and len(name) > 17
        and _MAC_SUFFIX.search(name)
    )


def device_type_from_name(name: str | None) -> str | None:
    """Device type letter (A..S) = char 7 of the advertised name."""
    if not name or len(name) <= NAME_TYPE_INDEX:
        return None
    letter = name[NAME_TYPE_INDEX].upper()
    return letter if "A" <= letter <= "Z" else None


def _mesh_reply(data: bytes | None) -> tuple[str, str] | None:
    """Split a mesh reply "XXX AAAA value\\r" into (address, value)."""
    if data is None or len(data) < 9 or data[3] != 0x20 or data[8] != 0x20:
        return None
    node = data[4:8].decode("ascii", "ignore").upper()
    value = data[9:].decode("ascii", "ignore").strip("\r\n\x00 ")
    return node, value


def _text_reply_from(node: str) -> Callable[[bytes], bool]:
    """Accept "RMS <node> <text>" replies (RF100, RS105...), not RC frames."""

    def accept(data: bytes) -> bool:
        reply = _mesh_reply(data)
        return reply is not None and reply[0] == node and len(data) < 17

    return accept


def _color_reply_from(node: str) -> Callable[[bytes], bool]:
    """Accept the binary RC reply "RMS <node> " + 4 words + CR (18 bytes)."""

    def accept(data: bytes) -> bool:
        reply = _mesh_reply(data)
        return reply is not None and reply[0] == node and len(data) >= 17

    return accept


def parse_groups(value: str) -> list[str]:
    """Register 105 = 4 group slots, one hex char each, '0' = empty."""
    slots = value.strip().upper()[:4]
    groups: list[str] = []
    for slot in slots:
        if slot in "123456789ABCDEF" and slot not in groups:
            groups.append(slot)
    return groups


def _encode_channel(value: int) -> int:
    """Encode a 0..255 channel into the 16-bit wire word."""
    return (((value * 1000) // 255) << 5) | COLOR_FLAG


def _decode_channel(hi: int, lo: int) -> int:
    """Decode a 16-bit wire word back to a 0..255 channel."""
    word = (hi << 8) | lo
    value = (word ^ COLOR_FLAG) >> 5  # 0..1000
    value = max(0, min(1000, value))
    return value * 255 // 1000


def _color_frame(
    prefix: str, red: int, green: int, blue: int, white: int = 0
) -> bytes:
    """12-char command + 4 big-endian words W,R,G,B = 20 bytes."""
    words = (
        _encode_channel(white),
        _encode_channel(red),
        _encode_channel(green),
        _encode_channel(blue),
    )
    data = bytearray(prefix.encode())
    for word in words:
        data += bytes(((word >> 8) & 0xFF, word & 0xFF))
    return bytes(data)


def build_color(node: str, red: int, green: int, blue: int, white: int = 0) -> bytes:
    """Build a WC set-color frame (W,R,G,B)."""
    return _color_frame(f"SMS LN{node}WC", red, green, blue, white)


def build_power(node: str, on: bool) -> bytes:
    """Build a WF on/off frame. Channel 100 = main."""
    return f"SMS LN{node}WF100{'1' if on else '0'}".encode()


def group_address(group_id: str) -> str:
    """Group id (one hex char) -> 4-char group address, e.g. "1" -> "1111"."""
    return group_id * 4


# Per-type on/off channel for group WF. Italian alphabet: no J/K.
_GROUP_CHANNELS = {letter: 102 + i for i, letter in enumerate("ABCDEFGHILMNOPQRS")}


def group_channel(device_type: str) -> int:
    """Group WF channel for a device type; main channel 100 for X/unknown."""
    return _GROUP_CHANNELS.get(device_type, 100)


def build_group_power(group_id: str, device_type: str, on: bool) -> bytes:
    """Build "SMS LG<gggg>WF<channel><0|1>" for one device type in the group."""
    channel = group_channel(device_type)
    return f"SMS LG{group_address(group_id)}WF{channel}{'1' if on else '0'}".encode()


def build_group_color(
    group_id: str,
    device_type: str,
    red: int,
    green: int,
    blue: int,
    white: int = 0,
) -> bytes:
    """Build "SMS LG<gggg>C<type>" + W,R,G,B words.

    Type X uses the old "WC" opcode, like the vendor app's IsOldVersion path.
    """
    opcode = "WC" if device_type == "X" else f"C{device_type.lower()}"
    return _color_frame(
        f"SMS LG{group_address(group_id)}{opcode}", red, green, blue, white
    )


class ArtemideClient:
    """Manages one BLE connection into an Artemide mesh, plus its commands."""

    def __init__(self, password: str) -> None:
        self._password = password
        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()
        self._notify_future: asyncio.Future[bytes] | None = None
        self._accept: Callable[[bytes], bool] | None = None
        # Set when the connected lamp answers a bare "ERR": the session was
        # rejected (e.g. handshake not accepted); reconnect to recover.
        self._rejected = False
        self._collected: list[bytes] | None = None

    @property
    def connected(self) -> bool:
        return (
            self._client is not None
            and self._client.is_connected
            and not self._rejected
        )

    async def ensure_connected(self, device: BLEDevice) -> None:
        """Connect (if needed), subscribe to notifications, send the password."""
        if self.connected:
            return
        if self._client is not None:
            # Rejected session (or stale client): start over with a fresh one.
            await self.disconnect()
        _LOGGER.debug("Connecting to %s", device.address)
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.address,
            self._on_disconnect,
        )
        self._client = client
        self._rejected = False
        await client.start_notify(NOTIFY_CHAR_UUID, self._notification_handler)
        await client.write_gatt_char(
            WRITE_CHAR_UUID, f"SPS {self._password}\r".encode(), response=False
        )
        await asyncio.sleep(WAIT_NEXT_COMMAND)
        _LOGGER.debug("Handshake complete for %s", device.address)

    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("Disconnected")
        self._client = None

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is not None and client.is_connected:
            await client.disconnect()

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        _LOGGER.debug("RX: %s %r", data.hex(), bytes(data))
        if bytes(data).strip() == b"ERR":
            _LOGGER.warning("Artemide lamp rejected a command (ERR); will reconnect")
            self._rejected = True
        if self._collected is not None:
            self._collected.append(bytes(data))
        if self._notify_future is None or self._notify_future.done():
            return
        if self._accept is not None and not self._accept(bytes(data)):
            # Late reply to an earlier (timed-out) read, or another node's.
            _LOGGER.debug("RX ignored: not the awaited reply")
            return
        self._notify_future.set_result(bytes(data))

    async def _write(self, data: bytes) -> None:
        if self._client is None:
            raise RuntimeError("Not connected")
        _LOGGER.debug("TX: %s", data.hex())
        await self._client.write_gatt_char(WRITE_CHAR_UUID, data, response=False)

    async def _write_and_read(
        self,
        data: bytes,
        timeout: float = READ_TIMEOUT,
        accept: Callable[[bytes], bool] | None = None,
    ) -> bytes | None:
        """Write a read command and await the next (accepted) notification."""
        loop = asyncio.get_running_loop()
        self._notify_future = loop.create_future()
        self._accept = accept
        try:
            await self._write(data)
            return await asyncio.wait_for(self._notify_future, timeout)
        except TimeoutError:
            _LOGGER.debug("Timeout waiting for response to %s", data)
            return None
        finally:
            self._notify_future = None
            self._accept = None

    async def _write_and_collect(self, data: bytes, window: float) -> list[bytes]:
        """Write a command and gather every notification for `window` seconds."""
        self._collected = []
        try:
            await self._write(data)
            await asyncio.sleep(window)
            return self._collected
        finally:
            self._collected = None

    async def send_raw(self, data: bytes, window: float) -> list[bytes]:
        """Diagnostics: send arbitrary bytes, return replies within `window`."""
        async with self._lock:
            return await self._write_and_collect(data, window)

    async def identify(self, device: BLEDevice) -> LampIdentity | None:
        """Read the connected lamp's own mesh address (RID) and group slots.

        RID is a direct command (no "SMS" prefix): it answers for
        the lamp we are connected to, which is why each lamp is visited.
        """
        await self.ensure_connected(device)
        async with self._lock:
            response = await self._write_and_read(b"RID\r")
        if response is None:
            return None
        matches = _HEX4.findall(response.decode("ascii", "ignore").upper())
        if not matches:
            _LOGGER.debug("RID: unexpected reply %r", response)
            return None
        node = matches[-1]
        await asyncio.sleep(WAIT_NEXT_COMMAND)
        return LampIdentity(node=node, groups=await self.read_groups(node))

    async def read_groups(self, node: str) -> list[str]:
        """Best effort: read group slots from register 105 (RS105).

        The vendor app only ever writes register 105 (WS105), but every other
        WSnnn register it uses has an RSnnn read, so RS105 should exist.
        """
        async with self._lock:
            response = await self._write_and_read(
                f"SMS LN{node}RS105".encode(), accept=_text_reply_from(node)
            )
        reply = _mesh_reply(response)
        if reply is None:
            _LOGGER.debug("RS105 %s: no usable reply %r", node, response)
            return []
        _LOGGER.debug("RS105 %s: %r", node, reply[1])
        return parse_groups(reply[1])

    async def broadcast_scan(self) -> list[str]:
        """Best effort: RF100 to broadcast FFFF, collect replying addresses."""
        async with self._lock:
            replies = await self._write_and_collect(
                b"SMS LNFFFFRF100", BROADCAST_WINDOW
            )
        found: list[str] = []
        for data in replies:
            reply = _mesh_reply(data)
            if reply and _HEX4.fullmatch(reply[0]) and reply[0] not in found:
                found.append(reply[0])
        return found

    async def set_state(
        self,
        node: str,
        on: bool,
        red: int,
        green: int,
        blue: int,
        white: int = 0,
    ) -> None:
        """Turn a node on/off and (when on) set its RGB color."""
        async with self._lock:
            if not on:
                await self._write(build_power(node, False))
                return
            await self._write(build_power(node, True))
            await asyncio.sleep(WAIT_NEXT_COMMAND)
            await self._write(build_color(node, red, green, blue, white))

    async def set_group_state(
        self,
        group_id: str,
        device_types: list[str],
        on: bool,
        red: int,
        green: int,
        blue: int,
        white: int = 0,
    ) -> None:
        """Switch/color a whole group with one mesh frame per device type.

        Mirrors the vendor app: it loops over the distinct device types in the
        group (Group.LstCommandType), never over the lamps.
        """
        async with self._lock:
            for index, device_type in enumerate(device_types):
                if index:
                    await asyncio.sleep(WAIT_NEXT_COMMAND)
                await self._write(build_group_power(group_id, device_type, on))
                if on:
                    await asyncio.sleep(WAIT_NEXT_COMMAND)
                    await self._write(
                        build_group_color(
                            group_id, device_type, red, green, blue, white
                        )
                    )

    async def scan_nodes(self, known: list[str] | None = None) -> list[str]:
        """Probe sequential node addresses; return new ones that answer.

        There is no BLE enumeration opcode (addresses come from the vendor app's
        network config), but they are dense hex assigned max+1, so a sequential
        RF100 probe finds mesh nodes out of direct BLE range. Any reply => exists.
        """
        known = known or []
        found: list[str] = []
        gap = 0
        async with self._lock:
            for i in range(1, SCAN_MAX_COUNT + 1):
                node = f"{i:04X}"
                if node in known:
                    gap = 0
                    continue
                response = await self._write_and_read(
                    f"SMS LN{node}RF100".encode(),
                    timeout=SCAN_TIMEOUT,
                    accept=_text_reply_from(node),
                )
                if response is not None:
                    _LOGGER.debug("Scan: node %s present", node)
                    found.append(node)
                    gap = 0
                else:
                    gap += 1
                    if (found or known) and gap >= SCAN_MAX_GAP:
                        break
                await asyncio.sleep(WAIT_NEXT_COMMAND)
        return found

    async def read_state(self, node: str) -> NodeState | None:
        """Poll a node: RF100 (on/level) then RC (color)."""
        async with self._lock:
            output = await self._write_and_read(
                f"SMS LN{node}RF100".encode(), accept=_text_reply_from(node)
            )
            await asyncio.sleep(WAIT_READ_RF)
            response = await self._write_and_read(
                f"SMS LN{node}RC".encode(), accept=_color_reply_from(node)
            )
            await asyncio.sleep(WAIT_READ_RC - WAIT_READ_RF)

        state = self._parse_color(response)
        on = self._parse_output(output)
        if state is not None and on is not None:
            # RF100 is authoritative: RC keeps the last color while off.
            state.on = on
        return state

    @staticmethod
    def _parse_output(data: bytes | None) -> bool | None:
        """RF100 reply "RMS 0001 00001\\r": 00000 = off, 00001 = on."""
        reply = _mesh_reply(data)
        if reply is None or not reply[1].isdigit():
            return None
        return int(reply[1]) > 0

    @staticmethod
    def _parse_color(data: bytes | None) -> NodeState | None:
        """Parse a binary RC response: "RMS <node> " + W,R,G,B words + CR."""
        if data is None or len(data) < 17:
            return None
        if data[3] != 0x20 or data[8] != 0x20:
            return None
        white = _decode_channel(data[9], data[10])
        red = _decode_channel(data[11], data[12])
        green = _decode_channel(data[13], data[14])
        blue = _decode_channel(data[15], data[16])
        on = (white | red | green | blue) > 0
        return NodeState(on=on, red=red, green=green, blue=blue, white=white)
