"""Wisniowski Connected cloud integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WisniowskiAuthError, WisniowskiClient, WisniowskiError
from .const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN
from .coordinator import WisniowskiCoordinator

PLATFORMS = [Platform.COVER, Platform.BUTTON, Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wisniowski Connected from a config entry."""

    def _update_tokens(data: dict) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **data})

    client = WisniowskiClient(
        async_get_clientsession(hass),
        dict(entry.data),
        async_update_tokens=_update_tokens,
    )

    try:
        await client.async_initialize()
        coordinator = WisniowskiCoordinator(hass, entry, client)
        await coordinator.async_start()
    except WisniowskiAuthError as exc:
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except WisniowskiError as exc:
        raise ConfigEntryNotReady(str(exc)) from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Wisniowski Connected config entry."""

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime:
        await runtime[DATA_COORDINATOR].async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
