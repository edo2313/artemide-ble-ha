"""Lamp discovery for the Artemide BLE integration.

There is no gateway and no mesh enumeration opcode: the vendor app keeps the
lamp list in its (cloud-synced) network config. We rebuild it from the radio:

1. every Artemide device advertises on its own ("A..." name ending in its MAC);
2. connect to each one and ask its own mesh address with RID;
3. through one lamp, look for mesh nodes out of direct BLE range (broadcast
   read + sequential probe);
4. read each node's group slots (RS105), best effort.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant

from .client import ArtemideClient, device_type_from_name, is_artemide_name
from .const import (
    IDENTIFY_TIMEOUT,
    LAMP_GROUPS,
    LAMP_MAC,
    LAMP_NAME,
    LAMP_NODE,
    LAMP_TYPE,
    SERVICE_UUID,
)

_LOGGER = logging.getLogger(__name__)


def discovered_lamps(hass: HomeAssistant) -> list[BluetoothServiceInfoBleak]:
    """Connectable Artemide advertisements, strongest signal first."""
    infos = [
        info
        for info in bluetooth.async_discovered_service_info(hass, connectable=True)
        if is_artemide_name(info.name) or SERVICE_UUID in info.service_uuids
    ]
    return sorted(infos, key=lambda info: info.rssi, reverse=True)


def lamp_record(
    node: str,
    mac: str | None = None,
    name: str | None = None,
    groups: list[str] | None = None,
) -> dict[str, Any]:
    return {
        LAMP_NODE: node,
        LAMP_MAC: mac,
        LAMP_NAME: name,
        LAMP_TYPE: device_type_from_name(name),
        LAMP_GROUPS: groups or [],
    }


async def async_discover_lamps(
    hass: HomeAssistant, password: str
) -> list[dict[str, Any]]:
    """Identify every reachable lamp; return lamp records (may be empty)."""
    lamps: list[dict[str, Any]] = []
    for info in discovered_lamps(hass):
        client = ArtemideClient(password)
        try:
            identity = await asyncio.wait_for(
                client.identify(info.device), IDENTIFY_TIMEOUT
            )
        except Exception:  # noqa: BLE001 - one bad lamp must not stop discovery
            _LOGGER.debug("Identify failed for %s", info.address, exc_info=True)
            identity = None
        finally:
            await client.disconnect()
        if identity is None:
            _LOGGER.warning(
                "Artemide lamp %s (%s) did not report its address",
                info.name,
                info.address,
            )
            continue
        if any(lamp[LAMP_NODE] == identity.node for lamp in lamps):
            continue
        _LOGGER.debug(
            "Lamp %s: node %s, groups %s", info.name, identity.node, identity.groups
        )
        lamps.append(
            lamp_record(identity.node, info.address, info.name, identity.groups)
        )

    if lamps:
        lamps.extend(await _async_discover_mesh_only(hass, password, lamps))
    return lamps


async def _async_discover_mesh_only(
    hass: HomeAssistant, password: str, lamps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Find nodes reachable only through the mesh, via the first lamp."""
    device = bluetooth.async_ble_device_from_address(
        hass, lamps[0][LAMP_MAC], connectable=True
    )
    if device is None:
        return []
    known = [lamp[LAMP_NODE] for lamp in lamps]
    client = ArtemideClient(password)
    extra: list[dict[str, Any]] = []
    try:
        await client.ensure_connected(device)
        candidates = await client.broadcast_scan()
        _LOGGER.debug("Broadcast RF100 replies from %s", candidates)
        new = [node for node in candidates if node not in known]
        new += await client.scan_nodes(known + new)
        for node in new:
            extra.append(lamp_record(node, groups=await client.read_groups(node)))
    except Exception:  # noqa: BLE001 - mesh-only nodes are a best-effort bonus
        _LOGGER.debug("Mesh scan failed", exc_info=True)
    finally:
        await client.disconnect()
    return extra
