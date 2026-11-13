"""Simulated Lavva HTTP responses and push events for Home Assistant tests."""

import asyncio
import time

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.wisniowski_connected import api
from custom_components.wisniowski_connected.const import DOMAIN


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-account-id",
        title="owner@example.invalid",
        data={
            "token": "test-original-access",
            "refresh_token": "test-original-refresh",
            "expires_at": time.time() + 3600,
            "user_id": "test-account-id",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def subscription(enable_custom_integrations, monkeypatch):
    events = asyncio.Queue()
    disconnected = asyncio.Event()

    async def subscribe(client, gate_ids):
        try:
            while True:
                yield await events.get()
        finally:
            disconnected.set()

    monkeypatch.setattr(api.WisniowskiClient, "async_subscribe", subscribe)
    return events, disconnected


@pytest.fixture
def cloud(aioclient_mock):
    broker = "https://broker.example.invalid"
    aioclient_mock.post(
        api.KEYCLOAK_TOKEN_URL,
        json={
            "access_token": "test-renewed-access",
            "refresh_token": "test-renewed-refresh",
            "expires_in": 3600,
            "refresh_expires_in": 86400,
        },
    )
    aioclient_mock.get(
        api.KEYCLOAK_USERINFO_URL,
        json={"sub": "test-account-id", "email": "owner@example.invalid"},
    )
    installation = {"installationId": "test-installation-id", "brokerUrl": broker}
    aioclient_mock.post(
        f"{api.INITIAL_BROKER_URL}/graphql/?opname=CurrentInstallation",
        json={"data": {"currentInstallation": installation}},
    )
    device = {
        "deviceId": "test-device-id",
        "name": "Module",
        "macAddress": "02:00:00:00:00:01",
    }
    aioclient_mock.post(
        f"{broker}/graphql/?opname=DevicesGetAll",
        json={"data": {"allUserDevices": [{"payload": device}]}},
    )
    channel = {
        "id": "test-channel-id",
        "deviceId": "test-device-id",
        "channelType": "GATE",
        "alias": "Test gate",
        "payload": {
            "deviceConnectionState": "CONNECTED",
            "position": 100,
            "supportedGateFeatures": ["SET_DIR", "SET_POS", "STEP_BY_STEP"],
        },
    }
    aioclient_mock.post(
        f"{broker}/graphql/?opname=ChannelsGetAll",
        json={"data": {"allUserChannels": [channel]}},
    )
    return aioclient_mock
