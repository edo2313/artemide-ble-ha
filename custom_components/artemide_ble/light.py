"""Light platform for the Artemide BLE integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import NodeState, group_address
from .const import DOMAIN, LAMP_GROUPS, LAMP_MAC, LAMP_NAME, LAMP_TYPE
from .coordinator import ArtemideCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one light entity per node and one per group."""
    coordinator: ArtemideCoordinator = entry.runtime_data
    entities: list[LightEntity] = [
        ArtemideLight(coordinator, entry, node) for node in coordinator.nodes
    ]
    entities += [
        ArtemideGroupLight(coordinator, entry, group_id, members)
        for group_id, members in coordinator.groups.items()
    ]
    async_add_entities(entities)


class _ArtemideRGBWLight(CoordinatorEntity[ArtemideCoordinator], LightEntity):
    """RGBW + brightness folded into the channel values, as the lamps expect."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_color_mode = ColorMode.RGBW
    _attr_supported_color_modes = {ColorMode.RGBW}

    def __init__(self, coordinator: ArtemideCoordinator) -> None:
        super().__init__(coordinator)
        # Local best-effort brightness/color until the first poll or command.
        self._rgbw: tuple[int, int, int, int] = (255, 255, 255, 0)
        self._brightness: int = 255
        self._on: bool = False

    def _apply_state(self, state: NodeState) -> None:
        self._on = state.on
        channels = (state.red, state.green, state.blue, state.white)
        mx = max(channels)
        if mx > 0:
            self._brightness = mx
            scale = 255 / mx
            self._rgbw = tuple(min(255, round(c * scale)) for c in channels)

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def rgbw_color(self) -> tuple[int, int, int, int]:
        return self._rgbw

    def _folded_rgbw(self) -> tuple[int, int, int, int]:
        """RGBW with brightness folded in (what the device expects)."""
        scale = self._brightness / 255
        r, g, b, w = (round(c * scale) for c in self._rgbw)
        return r, g, b, w

    async def _async_send(
        self, on: bool, red: int, green: int, blue: int, white: int
    ) -> None:
        raise NotImplementedError

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_RGBW_COLOR in kwargs:
            self._rgbw = kwargs[ATTR_RGBW_COLOR]
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]
        await self._async_send(True, *self._folded_rgbw())
        self._on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_send(False, 0, 0, 0, 0)
        self._on = False
        self.async_write_ha_state()


class ArtemideLight(_ArtemideRGBWLight):
    """A single Artemide lamp node."""

    def __init__(
        self, coordinator: ArtemideCoordinator, entry: ConfigEntry, node: str
    ) -> None:
        super().__init__(coordinator)
        self._node = node
        lamp = coordinator.lamp(node) or {}
        self._attr_unique_id = f"{entry.unique_id}_{node}"
        mac = lamp.get(LAMP_MAC)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_{node}")},
            connections={(dr.CONNECTION_BLUETOOTH, mac)} if mac else set(),
            name=f"Artemide {node}",
            manufacturer="Artemide",
            model=(lamp.get(LAMP_NAME) or "")[:3] or None,
        )
        self._attr_extra_state_attributes = {
            "mesh_node": node,
            "advertised_name": lamp.get(LAMP_NAME),
            "device_type": lamp.get(LAMP_TYPE),
            "groups": lamp.get(LAMP_GROUPS, []),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        state = self.coordinator.data.get(self._node) if self.coordinator.data else None
        if state is not None:
            self._apply_state(state)
        super()._handle_coordinator_update()

    async def _async_send(
        self, on: bool, red: int, green: int, blue: int, white: int
    ) -> None:
        # Optimistic state from the coordinator; the next poll confirms it.
        await self.coordinator.async_set_state(
            self._node, on, red, green, blue, white
        )


class ArtemideGroupLight(_ArtemideRGBWLight):
    """An Artemide group: one "SMS LG<gggg>" frame switches all members at once.

    On if any member is on; color from the first member that is on.
    """

    def __init__(
        self,
        coordinator: ArtemideCoordinator,
        entry: ConfigEntry,
        group_id: str,
        members: list[str],
    ) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        self._members = members
        self._attr_unique_id = f"{entry.unique_id}_group_{group_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_group_{group_id}")},
            name=f"Artemide group {group_id}",
            manufacturer="Artemide",
            model="Group",
        )
        self._attr_extra_state_attributes = {
            "group_id": group_id,
            "group_address": group_address(group_id),
            "members": members,
            "device_types": coordinator.group_types(group_id),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data or {}
        states = [data[node] for node in self._members if node in data]
        if states:
            lit = [state for state in states if state.on]
            self._apply_state(lit[0] if lit else states[0])
            self._on = bool(lit)
        super()._handle_coordinator_update()

    async def _async_send(
        self, on: bool, red: int, green: int, blue: int, white: int
    ) -> None:
        # Optimistic member states from the coordinator; next poll confirms.
        await self.coordinator.async_set_group_state(
            self._group_id, on, red, green, blue, white
        )
