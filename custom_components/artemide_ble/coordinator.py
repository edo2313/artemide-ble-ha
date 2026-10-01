"""DataUpdateCoordinator for the Artemide BLE integration."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import ArtemideClient, NodeState
from .const import DOMAIN, LAMP_GROUPS, LAMP_MAC, LAMP_NODE, LAMP_TYPE

_LOGGER = logging.getLogger(__name__)


class ArtemideCoordinator(DataUpdateCoordinator[dict[str, NodeState]]):
    """Holds one BLE connection into the mesh and polls each node's state.

    Any lamp in range works as the entry point: commands are "SMS LN<node>..."
    and the mesh relays them. The strongest advertising lamp is used.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ArtemideClient,
        lamps: list[dict[str, Any]],
        entry_points: list[str],
        poll_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
            config_entry=entry,
        )
        self.client = client
        self.lamps = lamps
        self.nodes = [lamp[LAMP_NODE] for lamp in lamps]
        self.entry_points = entry_points
        # Set while the options flow re-runs discovery (it needs the radio).
        self.paused = False
        # node -> monotonic time of the last command sent to it.
        self._commanded: dict[str, float] = {}

    def _ranked_entry_points(self) -> list[str]:
        def rssi(mac: str) -> int:
            info = bluetooth.async_last_service_info(self.hass, mac, connectable=True)
            return info.rssi if info is not None else -1000

        return sorted(self.entry_points, key=rssi, reverse=True)

    async def _connect(self) -> None:
        if self.client.connected:
            return
        last_error: Exception | None = None
        for mac in self._ranked_entry_points():
            device = bluetooth.async_ble_device_from_address(
                self.hass, mac, connectable=True
            )
            if device is None:
                continue
            try:
                await self.client.ensure_connected(device)
                return
            except Exception as err:  # noqa: BLE001 - try the next lamp
                _LOGGER.debug("Connect via %s failed: %s", mac, err)
                last_error = err
                await self.client.disconnect()
        raise UpdateFailed(
            "No Artemide lamp reachable "
            "(is the Bluetooth proxy online and in range?)"
            + (f": {last_error}" if last_error else "")
        )

    async def _async_update_data(self) -> dict[str, NodeState]:
        if self.paused:
            return self.data or {}
        try:
            await self._connect()
            result: dict[str, NodeState] = {}
            for node in self.nodes:
                started = time.monotonic()
                state = await self.client.read_state(node)
                if state is not None and self._commanded.get(node, 0) < started:
                    result[node] = state
                elif self.data and node in self.data:
                    # Failed read, or a command raced this read (its result
                    # predates the command): keep the current state.
                    result[node] = self.data[node]
            return result
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001 - surface as UpdateFailed
            raise UpdateFailed(f"Error polling Artemide: {err}") from err

    @property
    def groups(self) -> dict[str, list[str]]:
        """Group id -> member nodes, from the register 105 slots of each lamp."""
        groups: dict[str, list[str]] = {}
        for lamp in self.lamps:
            for group_id in lamp[LAMP_GROUPS]:
                groups.setdefault(group_id, []).append(lamp[LAMP_NODE])
        return dict(sorted(groups.items()))

    def group_types(self, group_id: str) -> list[str] | None:
        """Distinct device types in a group, or None if any member's is unknown."""
        types: list[str] = []
        for node in self.groups.get(group_id, []):
            lamp = self.lamp(node)
            device_type = lamp[LAMP_TYPE] if lamp else None
            if device_type is None:
                return None
            if device_type not in types:
                types.append(device_type)
        return types

    async def async_fill_groups(self) -> bool:
        """Read group slots (RS105) of lamps that have none; True if any changed.

        Covers lamps whose read failed during discovery or added by hand. Lamps really in no group are simply re-read at each setup.
        """
        changed = False
        missing = [lamp for lamp in self.lamps if not lamp[LAMP_GROUPS]]
        if not missing:
            return False
        try:
            await self._connect()
            for lamp in missing:
                groups = await self.client.read_groups(lamp[LAMP_NODE])
                if groups:
                    lamp[LAMP_GROUPS] = groups
                    changed = True
        except Exception as err:  # noqa: BLE001 - groups are optional
            _LOGGER.debug("Reading group slots failed: %s", err)
        return changed

    async def async_set_group_state(
        self,
        group_id: str,
        on: bool,
        red: int,
        green: int,
        blue: int,
        white: int = 0,
    ) -> None:
        """One mesh frame per device type; per-lamp fallback if types unknown."""
        await self._connect()
        types = self.group_types(group_id)
        if types:
            await self.client.set_group_state(
                group_id, types, on, red, green, blue, white
            )
        else:
            for node in self.groups.get(group_id, []):
                await self.client.set_state(node, on, red, green, blue, white)
        self._set_optimistic(
            self.groups.get(group_id, []), on, red, green, blue, white
        )

    def _set_optimistic(
        self,
        nodes: list[str],
        on: bool,
        red: int,
        green: int,
        blue: int,
        white: int = 0,
    ) -> None:
        """Show the commanded state until a poll newer than the command."""
        now = time.monotonic()
        data = dict(self.data or {})
        for node in nodes:
            self._commanded[node] = now
            old = data.get(node)
            if on:
                data[node] = NodeState(
                    on=True, red=red, green=green, blue=blue, white=white
                )
            elif old is not None:
                # Off keeps the last color, like the lamp does.
                data[node] = NodeState(
                    on=False,
                    red=old.red,
                    green=old.green,
                    blue=old.blue,
                    white=old.white,
                )
            else:
                data[node] = NodeState(on=False, red=0, green=0, blue=0)
        self.async_set_updated_data(data)

    def lamp(self, node: str) -> dict[str, Any] | None:
        return next((lamp for lamp in self.lamps if lamp[LAMP_NODE] == node), None)

    def mac_of(self, node: str) -> str | None:
        lamp = self.lamp(node)
        return lamp[LAMP_MAC] if lamp else None

    async def async_set_state(
        self,
        node: str,
        on: bool,
        red: int,
        green: int,
        blue: int,
        white: int = 0,
    ) -> None:
        """Send a command, ensuring the connection is up first."""
        await self._connect()
        await self.client.set_state(node, on, red, green, blue, white)
        self._set_optimistic([node], on, red, green, blue, white)
