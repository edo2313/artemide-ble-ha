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
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant

from .client import (
    ArtemideClient,
    LampIdentity,
    device_type_from_name,
    is_artemide_name,
)
from .const import (
    CONNECT_GAP,
    IDENTIFY_ATTEMPTS,
    IDENTIFY_RETRY_DELAY,
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


@dataclass
class DiscoveryResult:
    """Lamp records plus what went wrong, for the config/options flow."""

    lamps: list[dict[str, Any]] = field(default_factory=list)
    # Advertising lamps that never reported their address: "name (MAC)".
    failed: list[str] = field(default_factory=list)
    # Lamps reporting an address already taken by another lamp.
    duplicates: list[str] = field(default_factory=list)


def _label(info: BluetoothServiceInfoBleak) -> str:
    # Artemide names already end with the MAC; don't repeat it.
    if info.name and info.name.upper().endswith(info.address.upper()):
        return info.name
    return f"{info.name or 'unknown'} ({info.address})"


async def _async_identify(
    info: BluetoothServiceInfoBleak, password: str
) -> LampIdentity | None:
    """RID on one lamp, retried: a lamp (or proxy slot) still busy with the
    previous connection often refuses the first attempt."""
    for attempt in range(1, IDENTIFY_ATTEMPTS + 1):
        client = ArtemideClient(password)
        try:
            identity = await asyncio.wait_for(
                client.identify(info.device), IDENTIFY_TIMEOUT
            )
        except Exception:  # noqa: BLE001 - retried, then reported
            _LOGGER.debug(
                "Identify attempt %s failed for %s", attempt, info.address, exc_info=True
            )
            identity = None
        finally:
            await client.disconnect()
            # Let the lamp and the proxy release the connection.
            await asyncio.sleep(CONNECT_GAP)
        if identity is not None:
            return identity
        if attempt < IDENTIFY_ATTEMPTS:
            await asyncio.sleep(IDENTIFY_RETRY_DELAY)
    return None


async def async_discover_lamps(
    hass: HomeAssistant,
    password: str,
    known: list[dict[str, Any]] | None = None,
) -> DiscoveryResult:
    """Identify every reachable lamp not already in `known`.

    The result's lamps include `known`, so a retry only visits the lamps that
    failed (or are new) and returns the complete list.
    """
    result = DiscoveryResult(lamps=[dict(lamp) for lamp in known or []])
    known_macs = {lamp[LAMP_MAC] for lamp in result.lamps if lamp[LAMP_MAC]}
    for info in discovered_lamps(hass):
        if info.address in known_macs:
            continue
        identity = await _async_identify(info, password)
        if identity is None:
            _LOGGER.warning(
                "Artemide lamp %s did not report its address", _label(info)
            )
            result.failed.append(_label(info))
            continue
        same = next(
            (lamp for lamp in result.lamps if lamp[LAMP_NODE] == identity.node), None
        )
        if same is not None:
            # Same address twice: separate networks, or a lamp not commissioned
            # in this one. Only one of them can be controlled.
            _LOGGER.warning(
                "Artemide lamp %s reports address %s, already used by %s",
                _label(info),
                identity.node,
                same[LAMP_NAME] or same[LAMP_NODE],
            )
            result.duplicates.append(
                f"{_label(info)}: address {identity.node}, "
                f"same as {same[LAMP_NAME] or 'node ' + same[LAMP_NODE]}"
            )
            continue
        _LOGGER.debug(
            "Lamp %s: node %s, groups %s", _label(info), identity.node, identity.groups
        )
        result.lamps.append(
            lamp_record(identity.node, info.address, info.name, identity.groups)
        )

    if any(lamp[LAMP_MAC] for lamp in result.lamps):
        result.lamps.extend(
            await _async_discover_mesh_only(hass, password, result.lamps)
        )
    return result


async def _async_discover_mesh_only(
    hass: HomeAssistant, password: str, lamps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Find nodes reachable only through the mesh, via the first direct lamp."""
    mac = next(lamp[LAMP_MAC] for lamp in lamps if lamp[LAMP_MAC])
    device = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
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
