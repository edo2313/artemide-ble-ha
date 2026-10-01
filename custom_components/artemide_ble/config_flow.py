"""Config flow for the Artemide BLE integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .client import is_artemide_name
from .const import (
    CONF_EXTRA_NODES,
    CONF_LAMPS,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_REDISCOVER,
    DEFAULT_PASSWORD,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    LAMP_GROUPS,
    LAMP_MAC,
    LAMP_NAME,
    LAMP_NODE,
    SERVICE_UUID,
)
from .discovery import async_discover_lamps, discovered_lamps

_LOGGER = logging.getLogger(__name__)


def _parse_nodes(raw: str) -> list[str]:
    """Split a comma/space separated node list into 4-char addresses."""
    nodes = [n.strip().upper() for n in raw.replace(",", " ").split()]
    return [n for n in nodes if n]


def _validate_nodes(raw: str) -> list[str] | None:
    """Optional list: empty is fine, every entry must be 4 characters."""
    nodes = _parse_nodes(raw)
    if any(len(n) != 4 for n in nodes):
        return None
    return nodes


def _describe(lamps: list[dict[str, Any]]) -> str:
    """One markdown line per lamp for form descriptions."""
    if not lamps:
        return "-"
    lines = []
    for lamp in lamps:
        where = lamp[LAMP_NAME] or "mesh only"
        groups = ", ".join(lamp[LAMP_GROUPS]) or "none read"
        lines.append(f"- node **{lamp[LAMP_NODE]}**: {where} (groups: {groups})")
    return "\n".join(lines)


class ArtemideConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Artemide BLE.

    One entry = one Artemide mesh network (all lamps share its password).
    """

    VERSION = 1

    def __init__(self) -> None:
        self._password: str = DEFAULT_PASSWORD
        self._lamps: list[dict[str, Any]] = []
        self._discover_task: asyncio.Task[list[dict[str, Any]]] | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """A lamp advertised: offer to set up the whole network once."""
        if not (
            is_artemide_name(discovery_info.name)
            or SERVICE_UUID in discovery_info.service_uuids
        ):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": "Artemide"}
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the lamps in range and ask for the network password."""
        await self.async_set_unique_id(DOMAIN, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            return await self.async_step_discover()

        infos = discovered_lamps(self.hass)
        if not infos:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_PASSWORD, default=self._password): str}
            ),
            description_placeholders={
                "devices": "\n".join(
                    f"- {info.name} ({info.address}, {info.rssi} dBm)"
                    for info in infos
                )
            },
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect to each lamp and read its mesh address (takes a while)."""
        if self._discover_task is None:
            self._discover_task = self.hass.async_create_task(
                async_discover_lamps(self.hass, self._password)
            )
        if not self._discover_task.done():
            return self.async_show_progress(
                step_id="discover",
                progress_action="discover",
                progress_task=self._discover_task,
            )
        try:
            self._lamps = self._discover_task.result()
        except Exception:  # noqa: BLE001 - reported as "nothing found"
            _LOGGER.exception("Artemide discovery failed")
            self._lamps = []
        self._discover_task = None
        if not self._lamps:
            return self.async_show_progress_done(next_step_id="failed")
        return self.async_show_progress_done(next_step_id="confirm")

    async def async_step_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No lamp answered: let the user retry (wrong password, out of range)."""
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(step_id="failed")

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was found; allow extra mesh-only node addresses."""
        errors: dict[str, str] = {}
        if user_input is not None:
            extra = _validate_nodes(user_input.get(CONF_EXTRA_NODES, ""))
            if extra is None:
                errors["base"] = "invalid_nodes"
            else:
                return self.async_create_entry(
                    title="Artemide",
                    data={CONF_PASSWORD: self._password, CONF_LAMPS: self._lamps},
                    options={
                        CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                        CONF_EXTRA_NODES: extra,
                    },
                )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Optional(CONF_EXTRA_NODES, default=""): str}),
            description_placeholders={"lamps": _describe(self._lamps)},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ArtemideOptionsFlow()


class ArtemideOptionsFlow(OptionsFlow):
    """Options: poll interval, extra nodes, re-run lamp discovery."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._discover_task: asyncio.Task[list[dict[str, Any]]] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            extra = _validate_nodes(user_input.get(CONF_EXTRA_NODES, ""))
            if extra is None:
                errors["base"] = "invalid_nodes"
            else:
                self._options = {
                    CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                    CONF_EXTRA_NODES: extra,
                }
                if user_input.get(CONF_REDISCOVER):
                    return await self.async_step_discover()
                return self.async_create_entry(data=self._options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=options.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=5, max=3600)),
                    vol.Optional(
                        CONF_EXTRA_NODES,
                        default=", ".join(options.get(CONF_EXTRA_NODES, [])),
                    ): str,
                    vol.Optional(CONF_REDISCOVER, default=False): bool,
                }
            ),
            description_placeholders={
                "lamps": _describe(self.config_entry.data.get(CONF_LAMPS, []))
            },
            errors=errors,
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-run discovery while the entry is unloaded from the radio."""
        if self._discover_task is None:
            self._discover_task = self.hass.async_create_task(
                self._async_rediscover()
            )
        if not self._discover_task.done():
            return self.async_show_progress(
                step_id="discover",
                progress_action="discover",
                progress_task=self._discover_task,
            )
        return self.async_show_progress_done(next_step_id="finish")

    async def _async_rediscover(self) -> list[dict[str, Any]]:
        # Free the lamp connection held by the running entry: most lamps
        # accept a single central, so identify() would fail on that one.
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is not None:
            coordinator.paused = True
            await coordinator.client.disconnect()
        try:
            return await async_discover_lamps(
                self.hass, self.config_entry.data[CONF_PASSWORD]
            )
        finally:
            if coordinator is not None:
                coordinator.paused = False

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Merge the new discovery into the stored lamps and save."""
        try:
            found = self._discover_task.result() if self._discover_task else []
        except Exception:  # noqa: BLE001 - keep the old list on failure
            _LOGGER.exception("Artemide rediscovery failed")
            found = []
        lamps = {
            lamp[LAMP_NODE]: lamp
            for lamp in self.config_entry.data.get(CONF_LAMPS, [])
        }
        for lamp in found:
            old = lamps.get(lamp[LAMP_NODE])
            if old and lamp[LAMP_MAC] is None:
                # Seen only through the mesh this time: keep the known MAC.
                lamp = {**lamp, LAMP_MAC: old[LAMP_MAC], LAMP_NAME: old[LAMP_NAME]}
            if old and not lamp[LAMP_GROUPS]:
                lamp = {**lamp, LAMP_GROUPS: old[LAMP_GROUPS]}
            lamps[lamp[LAMP_NODE]] = lamp
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_LAMPS: list(lamps.values())},
        )
        return self.async_create_entry(data=self._options)
