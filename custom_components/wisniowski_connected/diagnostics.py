"""Config entry diagnostics for Wisniowski Connected."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AUTH_RESPONSE_URL,
    CONF_BROKER_URL,
    CONF_EXPIRES_AT,
    CONF_INSTALLATION_ID,
    CONF_REFRESH_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_UPDATED_AT,
    CONF_TOKEN,
    CONF_USER_ID,
    DATA_COORDINATOR,
    DOMAIN,
)

_REDACT = {
    CONF_AUTH_RESPONSE_URL,
    CONF_BROKER_URL,
    CONF_INSTALLATION_ID,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    CONF_USER_ID,
    "email",
}
_TIMESTAMPS = (CONF_EXPIRES_AT, CONF_REFRESH_EXPIRES_AT, CONF_REFRESH_TOKEN_UPDATED_AT)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Describe the entry and cached gates without contacting the cloud."""
    # Titles, options, and arbitrary provider fields may contain email addresses.
    config = async_redact_data(
        {key: entry.data[key] for key in _REDACT if key in entry.data}, _REDACT
    )
    for key in _TIMESTAMPS:
        if isinstance(value := entry.data.get(key), (int, float)):
            config[key] = value

    result: dict[str, Any] = {
        "config_entry": {"state": entry.state.value, "data": config},
        "coordinator": None,
    }
    if runtime := hass.data.get(DOMAIN, {}).get(entry.entry_id):
        coordinator = runtime[DATA_COORDINATOR]
        # Channel IDs are dictionary keys. Export observations per gate, without
        # those keys, device names, addresses, or raw subscription/error payloads.
        result["coordinator"] = {
            "last_update_success": coordinator.last_update_success,
            "gates": [
                {
                    "connection_state": gate.connection_state
                    if gate.connection_state in ("CONNECTED", "DISCONNECTED")
                    else None,
                    "supports_set_direction": coordinator.client.supports_set_direction(gate),
                    "supports_set_position": coordinator.client.supports_set_position(gate),
                    "supports_step_by_step": coordinator.client.supports_step_by_step(gate),
                }
                for gate in (coordinator.data or {}).values()
            ],
        }
    return result
