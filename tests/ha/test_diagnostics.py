"""Check the actual JSON file downloaded from Home Assistant."""

from dataclasses import replace
import json

from homeassistant.components.diagnostics import REDACTED
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from custom_components.wisniowski_connected.api import WisniowskiApiError
from custom_components.wisniowski_connected.const import DATA_COORDINATOR, DOMAIN


async def download(hass, hass_client, entry):
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == 200
    return await response.json()


async def test_download_redacts_entry_coordinator_and_devices(hass, hass_client, entry, cloud):
    callback = "https://callback.example.invalid/#code=test-private-code&state=test-private-state"
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "auth_response_url": callback,
            "email": "account@example.invalid",
            "extra": [{"contact@example.invalid": "test-extra-secret"}],
            "refresh_token_updated_at": "timestamp@example.invalid",
        },
        options={"notify": "notify@example.invalid"},
        unique_id="identity@example.invalid",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    registry = dr.async_get(hass)
    (device,) = dr.async_entries_for_config_entry(registry, entry.entry_id)
    registry.async_update_device(device.id, name_by_user="registry@example.invalid")
    (gate,) = coordinator.data.values()
    sensitive_gate = replace(
        gate,
        channel_id="private-channel-id",
        device_id="private-device-id",
        name="Gate for alias@example.invalid",
        device_name="device@example.invalid",
        model=entry.data["token"],
        mac_address="02:00:00:00:00:99",
        connection_state="state@example.invalid",
        direction=entry.data["user_id"],
        lock_status=entry.data["installation_id"],
        gate_mode=callback,
        supported_features=(entry.data["refresh_token"], "feature@example.invalid"),
    )
    coordinator.async_set_updated_data({gate.channel_id: gate, "key@example.invalid": sensitive_gate})
    coordinator.last_exception = WisniowskiApiError("error@example.invalid")

    calls = cloud.call_count
    full_report = await download(hass, hass_client, entry)
    assert cloud.call_count == calls
    raw = json.dumps(full_report)
    private_keys = (
        "token", "refresh_token", "auth_response_url", "user_id",
        "installation_id", "email", "broker_url",
    )
    for value in (
        *(entry.data[key] for key in private_keys),
        "@example.invalid", "test-private-code", "test-private-state", "test-extra-secret",
        gate.channel_id, gate.device_id, gate.mac_address,
        sensitive_gate.channel_id, sensitive_gate.device_id, sensitive_gate.mac_address, device.id,
    ):
        assert value not in raw

    data = full_report["data"]
    config = data["config_entry"]["data"]
    assert all(config[key] == REDACTED for key in private_keys)
    assert config["expires_at"] == entry.data["expires_at"]
    assert "refresh_token_updated_at" not in config
    assert data["coordinator"] == {
        "last_update_success": True,
        "gates": [
            {
                "connection_state": "CONNECTED",
                "supports_set_direction": True,
                "supports_set_position": True,
                "supports_step_by_step": True,
            },
            {
                "connection_state": None,
                "supports_set_direction": False,
                "supports_set_position": False,
                "supports_step_by_step": False,
            },
        ],
    }


async def test_unloaded_entry_still_has_diagnostics(hass, hass_client, entry, cloud):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    report = await download(hass, hass_client, entry)
    data = report["data"]
    assert data["config_entry"]["state"] == "not_loaded"
    assert data["config_entry"]["data"]["refresh_token"] == REDACTED
    assert data["coordinator"] is None
