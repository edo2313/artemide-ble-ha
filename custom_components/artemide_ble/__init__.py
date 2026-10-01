"""The Artemide BLE integration."""

from __future__ import annotations

from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .client import ArtemideClient
from .const import (
    CONF_EXTRA_NODES,
    CONF_LAMPS,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    DEFAULT_PASSWORD,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    LAMP_MAC,
    LAMP_NODE,
)
from .coordinator import ArtemideCoordinator
from .discovery import lamp_record

PLATFORMS = [Platform.LIGHT]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SEND_COMMANDS = "send_commands"
ATTR_COMMANDS = "commands"
ATTR_WINDOW = "window"
SEND_COMMANDS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ADDRESS): cv.string,
        vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): cv.string,
        vol.Required(ATTR_COMMANDS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_WINDOW, default=1.5): vol.All(
            vol.Coerce(float), vol.Range(min=0.2, max=10)
        ),
    }
)

ArtemideConfigEntry = ConfigEntry[ArtemideCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the raw-command action (works without a config entry)."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMANDS,
        partial(_async_send_commands, hass),
        schema=SEND_COMMANDS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def _async_send_commands(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Connect to one lamp, send raw commands, return every reply.

    Diagnostic tool for protocol probing through the Bluetooth proxy, e.g.
    ["RID\\r", "SMS LN0001RS105", "SMS LNFFFFRF100"]. "\\r" is unescaped;
    "hex:<bytes>" sends a binary frame (color commands).
    """
    mac: str = call.data[CONF_ADDRESS].upper()
    device = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
    if device is None:
        raise HomeAssistantError(f"Lamp {mac} not seen by any connectable adapter")

    # A lamp takes one central: pause the running entry and drop its link.
    coordinators = [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    for coordinator in coordinators:
        coordinator.paused = True
        await coordinator.client.disconnect()

    client = ArtemideClient(call.data[CONF_PASSWORD])
    results: list[dict[str, Any]] = []
    try:
        await client.ensure_connected(device)
        for command in call.data[ATTR_COMMANDS]:
            if command.startswith("hex:"):
                raw = bytes.fromhex(command[4:])
            else:
                raw = command.replace("\\r", "\r").encode()
            replies = await client.send_raw(raw, call.data[ATTR_WINDOW])
            results.append(
                {
                    "command": command,
                    "replies": [
                        {"ascii": data.decode("ascii", "replace"), "hex": data.hex()}
                        for data in replies
                    ],
                }
            )
    except Exception as err:
        raise HomeAssistantError(f"Artemide command failed: {err}") from err
    finally:
        await client.disconnect()
        for coordinator in coordinators:
            coordinator.paused = False
    return {"address": mac, "results": results}


async def async_setup_entry(hass: HomeAssistant, entry: ArtemideConfigEntry) -> bool:
    """Set up Artemide from a config entry."""
    password: str = entry.data[CONF_PASSWORD]
    poll_interval: int = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

    # Copies: coordinator fills groups in place, entry.data must stay pristine.
    lamps = [dict(lamp) for lamp in entry.data[CONF_LAMPS]]
    known = {lamp[LAMP_NODE] for lamp in lamps}
    lamps += [
        lamp_record(node)
        for node in entry.options.get(CONF_EXTRA_NODES, [])
        if node not in known
    ]
    entry_points = [lamp[LAMP_MAC] for lamp in lamps if lamp[LAMP_MAC]]

    client = ArtemideClient(password)
    coordinator = ArtemideCoordinator(
        hass, entry, client, lamps, entry_points, poll_interval
    )

    await coordinator.async_config_entry_first_refresh()
    if not coordinator.last_update_success:
        raise ConfigEntryNotReady("Could not reach any Artemide lamp")

    entry.runtime_data = coordinator

    # Before the update listener is attached, so persisting does not reload.
    if await coordinator.async_fill_groups():
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_LAMPS: [
                    lamp for lamp in coordinator.lamps if lamp[LAMP_NODE] in known
                ],
            },
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ArtemideConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.disconnect()
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ArtemideConfigEntry
) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
