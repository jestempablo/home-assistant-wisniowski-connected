"""Exercise flows and config entries through Home Assistant."""

import base64
import hashlib
import time
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from yarl import URL

from custom_components.wisniowski_connected import api
from custom_components.wisniowski_connected.const import DATA_COORDINATOR, DOMAIN


async def test_config_flow(hass, cloud):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert flow["type"] is FlowResultType.FORM
    auth = URL(flow["description_placeholders"]["authorization_url"]).query
    assert auth["code_challenge_method"] == "S256"
    callback = URL("https://callback.example.invalid").with_query(
        code="test-auth-code", state=auth["state"]
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"auth_response_url": str(callback)}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == entry.data["user_id"] == "test-account-id"
    assert entry.title == "Test gate"
    assert "auth_response_url" not in entry.data
    (exchange,) = [
        data for _, url, data, _ in cloud.mock_calls if str(url) == api.KEYCLOAK_TOKEN_URL
    ]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "test-auth-code"
    digest = hashlib.sha256(exchange["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == auth["code_challenge"]


async def test_config_flow_rejects_wrong_state(hass, cloud):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"auth_response_url": "https://callback.example.invalid/#code=test&state=wrong"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert cloud.call_count == 0
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_setup_push_and_unload(hass, entry, cloud, subscription):
    events, disconnected = subscription
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    for platform in ("cover", "button", "sensor", "binary_sensor"):
        assert hass.states.async_entity_ids(platform)
    (entity_id,) = hass.states.async_entity_ids("cover")
    assert hass.states.get(entity_id).state == "closed"

    events.put_nowait({"onGateStatusChanged": {"channelId": "test-channel-id", "position": 35}})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["current_position"] == 65

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]
    assert disconnected.is_set()
    assert all("/gate/" not in url.path for _, url, _, _ in cloud.mock_calls)


async def test_refresh_persists_rotated_tokens(hass, entry, cloud):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "expires_at": 0})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    (exchange,) = [
        data for _, url, data, _ in cloud.mock_calls if str(url) == api.KEYCLOAK_TOKEN_URL
    ]
    assert exchange["grant_type"] == "refresh_token"
    assert exchange["refresh_token"] == "test-original-refresh"
    assert entry.data["token"] == "test-renewed-access"
    assert entry.data["refresh_token"] == "test-renewed-refresh"
    assert entry.data["refresh_token_updated_at"] <= time.time() < entry.data["expires_at"]
    assert entry.data["refresh_expires_at"] > entry.data["expires_at"]
    assert entry.data["user_id"] == "test-account-id"
    headers = [h for _, url, _, h in cloud.mock_calls if url.path == "/graphql/"]
    assert headers and all(h["Authorization"] == "Bearer test-renewed-access" for h in headers)


async def test_rejected_refresh_starts_reauth(hass, entry, aioclient_mock):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "expires_at": 0})
    aioclient_mock.post(api.KEYCLOAK_TOKEN_URL, status=400, json={"error": "invalid_grant"})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.data["refresh_token"] == "test-original-refresh"
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == "reauth"
    assert flow["context"]["entry_id"] == entry.entry_id


async def test_broker_unavailable_during_setup(hass, entry, aioclient_mock):
    aioclient_mock.post(
        f"{api.INITIAL_BROKER_URL}/graphql/?opname=CurrentInstallation",
        status=503, text="Service unavailable",
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert aioclient_mock.call_count == 1


async def test_poll_recovers_after_broker_error(hass, entry, cloud):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    (entity_id,) = hass.states.async_entity_ids("cover")
    with patch.object(
        coordinator.client, "async_get_gates", side_effect=api.WisniowskiApiError("offline")
    ):
        await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert hass.states.get(entity_id).state == "unavailable"
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert hass.states.get(entity_id).state == "closed"
