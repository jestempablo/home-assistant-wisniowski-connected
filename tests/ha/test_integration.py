"""Exercise flows and config entries through Home Assistant."""

import asyncio
import base64
import hashlib
import time
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
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


async def test_config_flow_token_outage_shows_cannot_connect(hass, aioclient_mock):
    aioclient_mock.post(api.KEYCLOAK_TOKEN_URL, status=503, text="Service unavailable")
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    auth = URL(flow["description_placeholders"]["authorization_url"]).query
    callback = URL("https://callback.example.invalid").with_query(code="test", state=auth["state"])
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"auth_response_url": str(callback)}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
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


@pytest.mark.parametrize(
    "endpoint",
    [f"{api.INITIAL_BROKER_URL}/graphql/?opname=CurrentInstallation", api.KEYCLOAK_TOKEN_URL],
    ids=["broker", "token"],
)
async def test_outage_during_setup_retries(hass, entry, aioclient_mock, endpoint):
    if endpoint == api.KEYCLOAK_TOKEN_URL:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "expires_at": 0})
    aioclient_mock.post(endpoint, status=503, text="Service unavailable")
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
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert hass.states.get(entity_id).state == "closed"


async def test_runtime_poll_auth_failure_starts_reauth(hass, entry, cloud, subscription):
    _, disconnected = subscription
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    cloud.clear_requests()
    cloud.post(f"{coordinator.client.broker_url}/graphql/?opname=DevicesGetAll", status=401)
    cloud.post(api.KEYCLOAK_TOKEN_URL, status=400, json={"error": "invalid_grant"})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert not coordinator.last_update_success
    assert disconnected.is_set()
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == "reauth"
    assert flow["context"]["entry_id"] == entry.entry_id


async def test_subscription_recovers_after_transport_failure(hass, entry, cloud, subscription):
    events, _ = subscription
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    updated = asyncio.Event()
    remove_listener = coordinator.async_add_listener(updated.set)
    try:
        events.put_nowait(ConnectionResetError("connection reset"))
        events.put_nowait({"onGateStatusChanged": {"channelId": "test-channel-id", "position": 35}})
        async with asyncio.timeout(10):
            await updated.wait()
        assert coordinator.data["test-channel-id"].position == 35
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    finally:
        remove_listener()


async def test_subscription_auth_failure_stops_listener_and_starts_reauth(hass, entry, cloud, subscription):
    events, _ = subscription
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    events.put_nowait(api.WisniowskiAuthError("invalid_grant"))
    await hass.async_block_till_done()
    listener = coordinator._subscription_task
    assert listener.done()
    assert listener.exception() is None
    assert not coordinator.last_update_success
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == "reauth"
    assert flow["context"]["entry_id"] == entry.entry_id


async def test_reauth_reloads_entry_with_new_credentials(hass, entry, cloud, subscription):
    _, disconnected = subscription
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    old_coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    form = await hass.config_entries.flow.async_configure(flow["flow_id"])
    state = URL(form["description_placeholders"]["authorization_url"]).query["state"]
    callback = URL("https://callback.example.invalid").with_query(code="new-code", state=state)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"auth_response_url": str(callback)}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_success"
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    assert coordinator is not old_coordinator
    assert disconnected.is_set()
    assert entry.data["refresh_token"] == "test-renewed-refresh"
    assert coordinator.client.data["refresh_token"] == entry.data["refresh_token"]
