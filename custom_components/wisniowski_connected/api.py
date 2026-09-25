"""Cloud client for Wisniowski Connected / Lavva."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
import json
import logging
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType

from .const import (
    CLIENT_ID,
    CONF_BROKER_URL,
    CONF_EXPIRES_AT,
    CONF_INSTALLATION_ID,
    CONF_REFRESH_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_UPDATED_AT,
    CONF_TOKEN,
    INITIAL_BROKER_URL,
    INSTALLATION_SUBTYPE,
    KEYCLOAK_TOKEN_URL,
    KEYCLOAK_USERINFO_URL,
    REDIRECT_URI,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=30)


class WisniowskiError(Exception):
    """Base Wisniowski API error."""


class WisniowskiAuthError(WisniowskiError):
    """Authentication failed."""


class WisniowskiApiError(WisniowskiError):
    """Cloud API failed."""


@dataclass(frozen=True)
class WisniowskiGate:
    """A gate channel exposed by Wisniowski Connected."""

    channel_id: str
    device_id: str
    name: str
    device_name: str | None
    model: str | None
    mac_address: str | None
    connection_state: str | None
    position: int | None
    direction: str | None
    lock_status: str | None
    gate_mode: str | None
    supported_features: tuple[str, ...]


CURRENT_INSTALLATION_QUERY = """
query CurrentInstallation {
  currentInstallation {
    installationId
    brokerUrl
  }
}
"""

DEVICES_GET_ALL_QUERY = """
query DevicesGetAll($installationId: UUID!) {
  allUserDevices(installationId: $installationId) {
    id
    payload {
      deviceId
      model
      name
      macAddress
      connectedToSsid
      currentFirmwareVersion
      channelInfos {
        channelId
        channelNumber
        channelType
      }
    }
  }
}
"""

CHANNELS_GET_ALL_QUERY = """
query ChannelsGetAll($installationId: UUID!) {
  allUserChannels(installationId: $installationId, order: { deviceId: ASC, alias: ASC }) {
    alias
    id
    deviceId
    channelType
    payload {
      __typename
      channelId
      deviceConnectionState
      deviceId
      ... on GateChannelStateResponse {
        gateKind
        position
        partialControlModeStatus
        dailyModeStatus
        lockStatus
        gateDirection: direction
        gateMode
        supportedGateFeatures
      }
    }
  }
}
"""

ON_GATE_STATUS_CHANGED = """
subscription onGateStatusChanged($channelId: UUID!) {
  onGateStatusChanged(channelId: $channelId) {
    channelId
    dailyModeStatus
    deviceId
    direction
    installationId
    lockStatus
    partialControlModeStatus
    position
    predictedTimeInMs
    targetPosition
  }
}
"""

ON_DEVICE_CONNECTION_STATE_CHANGE = """
subscription OnDeviceConnectionStateChange($installationId: UUID!) {
  onDeviceConnectionStateChange(installationId: $installationId) {
    deviceId
    deviceConnectionState
  }
}
"""


def _token_expiry(token_payload: dict[str, Any]) -> float:
    return time.time() + int(token_payload.get("expires_in", 300))


def _optional_token_expiry(token_payload: dict[str, Any], key: str) -> float | None:
    try:
        return time.time() + int(token_payload[key])
    except (KeyError, TypeError, ValueError):
        return None


def _compact_mac(mac: str | None) -> str:
    return (mac or "").replace(":", "").replace("-", "").lower()


class WisniowskiClient:
    """Minimal async Lavva client used by the Home Assistant integration."""

    def __init__(
        self,
        session: ClientSession,
        data: dict[str, Any],
        async_update_tokens: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._session = session
        self._data = dict(data)
        self._async_update_tokens = async_update_tokens
        self._token_lock = asyncio.Lock()

    @classmethod
    async def async_from_code(
        cls,
        session: ClientSession,
        code: str,
        code_verifier: str,
    ) -> "WisniowskiClient":
        """Create a client by exchanging an OAuth authorization code."""

        token_data = await cls._async_token_request(
            session,
            {
                "client_id": CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
        )
        if CONF_REFRESH_TOKEN not in token_data:
            raise WisniowskiAuthError("Token response did not include a refresh token")

        data = {
            CONF_TOKEN: token_data["access_token"],
            CONF_REFRESH_TOKEN: token_data[CONF_REFRESH_TOKEN],
            CONF_EXPIRES_AT: _token_expiry(token_data),
            CONF_BROKER_URL: INITIAL_BROKER_URL,
            CONF_REFRESH_TOKEN_UPDATED_AT: time.time(),
        }
        refresh_expires_at = _optional_token_expiry(token_data, "refresh_expires_in")
        if refresh_expires_at is not None:
            data[CONF_REFRESH_EXPIRES_AT] = refresh_expires_at
        return cls(session, data)

    @staticmethod
    async def _async_token_request(session: ClientSession, data: dict[str, Any]) -> dict[str, Any]:
        try:
            async with session.post(KEYCLOAK_TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    description = payload.get("error_description") or payload.get("error") or response.reason
                    raise WisniowskiAuthError(str(description))
                return payload
        except (ClientError, asyncio.TimeoutError) as exc:
            raise WisniowskiAuthError(str(exc)) from exc

    @property
    def data(self) -> dict[str, Any]:
        """Return current token/config data."""

        return dict(self._data)

    @property
    def installation_id(self) -> str | None:
        """Return selected installation id."""

        return self._data.get(CONF_INSTALLATION_ID)

    @property
    def broker_url(self) -> str:
        """Return the active Lavva broker base URL."""

        return str(self._data.get(CONF_BROKER_URL) or INITIAL_BROKER_URL).rstrip("/")

    async def async_userinfo(self) -> dict[str, Any]:
        """Fetch OIDC user info."""

        headers = {"Authorization": f"Bearer {await self.async_access_token()}"}
        try:
            async with self._session.get(KEYCLOAK_USERINFO_URL, headers=headers, timeout=REQUEST_TIMEOUT) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise WisniowskiAuthError(str(payload))
                return payload
        except (ClientError, asyncio.TimeoutError) as exc:
            raise WisniowskiAuthError(str(exc)) from exc

    async def async_access_token(self) -> str:
        """Return a valid access token, refreshing and storing rotated tokens."""

        if self._has_valid_access_token():
            return str(self._data[CONF_TOKEN])

        async with self._token_lock:
            if self._has_valid_access_token():
                return str(self._data[CONF_TOKEN])

            refresh_token = self._data.get(CONF_REFRESH_TOKEN)
            if not refresh_token:
                raise WisniowskiAuthError("Missing refresh token")

            token_data = await self._async_token_request(
                self._session,
                {
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            updated = dict(self._data)
            updated[CONF_TOKEN] = token_data["access_token"]
            updated[CONF_EXPIRES_AT] = _token_expiry(token_data)
            refresh_expires_at = _optional_token_expiry(token_data, "refresh_expires_in")
            if refresh_expires_at is not None:
                updated[CONF_REFRESH_EXPIRES_AT] = refresh_expires_at
            if new_refresh_token := token_data.get(CONF_REFRESH_TOKEN):
                if new_refresh_token != refresh_token or not updated.get(CONF_REFRESH_TOKEN_UPDATED_AT):
                    updated[CONF_REFRESH_TOKEN_UPDATED_AT] = time.time()
                updated[CONF_REFRESH_TOKEN] = new_refresh_token
            self._data = updated
            if self._async_update_tokens:
                self._async_update_tokens(self.data)
        return str(self._data[CONF_TOKEN])

    def _has_valid_access_token(self) -> bool:
        """Return whether the cached access token can be reused."""

        try:
            return bool(self._data.get(CONF_TOKEN)) and float(self._data.get(CONF_EXPIRES_AT, 0)) > time.time() + 60
        except (TypeError, ValueError):
            return False

    async def async_initialize(self) -> None:
        """Load installation metadata and switch to the selected broker URL."""

        payload = await self.async_graphql(
            "CurrentInstallation",
            CURRENT_INSTALLATION_QUERY,
            {},
            broker_url=INITIAL_BROKER_URL,
        )
        installation = payload.get("currentInstallation")
        if not installation:
            raise WisniowskiApiError("No current installation returned by Lavva")

        broker_url = str(installation.get("brokerUrl") or INITIAL_BROKER_URL).rstrip("/")
        updated = dict(self._data)
        updated[CONF_INSTALLATION_ID] = str(installation["installationId"])
        updated[CONF_BROKER_URL] = broker_url.replace("/graphql", "").rstrip("/")
        self._data = updated
        if self._async_update_tokens:
            self._async_update_tokens(self.data)

    async def async_graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, Any],
        broker_url: str | None = None,
    ) -> dict[str, Any]:
        """Run a GraphQL HTTP operation."""

        base_url = (broker_url or self.broker_url).rstrip("/")
        url = f"{base_url}/graphql/?opname={operation_name}"
        body = {
            "operationName": operation_name,
            "query": query,
            "variables": variables,
        }

        # Retry authentication once, after releasing the previous response.
        for attempt in range(2):
            if attempt:
                await self._force_refresh()
            headers = {
                "Authorization": f"Bearer {await self.async_access_token()}",
                "APOLLO-QUERY-NAME": operation_name,
                "Content-Type": "application/json",
                "X-Installation-Subtype": INSTALLATION_SUBTYPE,
            }
            try:
                async with self._session.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT) as response:
                    if response.status == 401:
                        continue
                    raw = await response.text()
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        # Proxies may return HTML during outages. Report an API
                        # error without refreshing a potentially valid token.
                        raise WisniowskiApiError(
                            f"non-JSON response HTTP {response.status}: {raw[:200]!r}"
                        ) from None
                    if response.status >= 400:
                        raise WisniowskiApiError(str(payload))
                    if not isinstance(payload, dict):
                        raise WisniowskiApiError(f"Invalid GraphQL response HTTP {response.status}: expected an object")
                    if errors := payload.get("errors"):
                        raise WisniowskiApiError(str(errors))
                    return payload.get("data") or {}
            except (ClientError, asyncio.TimeoutError) as exc:
                raise WisniowskiApiError(str(exc)) from exc

        raise WisniowskiAuthError("GraphQL request returned HTTP 401 after token refresh")

    async def _force_refresh(self) -> None:
        self._data[CONF_EXPIRES_AT] = 0
        await self.async_access_token()

    async def async_get_gates(self) -> dict[str, WisniowskiGate]:
        """Fetch all configured gate channels."""

        if not self.installation_id:
            await self.async_initialize()

        installation_id = str(self.installation_id)
        devices_payload = await self.async_graphql(
            "DevicesGetAll",
            DEVICES_GET_ALL_QUERY,
            {"installationId": installation_id},
        )
        channels_payload = await self.async_graphql(
            "ChannelsGetAll",
            CHANNELS_GET_ALL_QUERY,
            {"installationId": installation_id},
        )

        devices: dict[str, dict[str, Any]] = {}
        for item in devices_payload.get("allUserDevices") or []:
            payload = item.get("payload") or {}
            device_id = str(payload.get("deviceId") or item.get("id") or "")
            if device_id:
                devices[device_id] = payload

        gates: dict[str, WisniowskiGate] = {}
        for channel in channels_payload.get("allUserChannels") or []:
            if channel.get("channelType") != "GATE":
                continue
            payload = channel.get("payload") or {}
            channel_id = str(channel.get("id") or payload.get("channelId") or "")
            device_id = str(channel.get("deviceId") or payload.get("deviceId") or "")
            if not channel_id or not device_id:
                continue
            device = devices.get(device_id, {})
            gates[channel_id] = WisniowskiGate(
                channel_id=channel_id,
                device_id=device_id,
                name=str(channel.get("alias") or device.get("name") or "Gate"),
                device_name=device.get("name"),
                model=device.get("model"),
                mac_address=device.get("macAddress"),
                connection_state=payload.get("deviceConnectionState"),
                position=_coerce_int(payload.get("position")),
                direction=payload.get("gateDirection"),
                lock_status=payload.get("lockStatus"),
                gate_mode=payload.get("gateMode"),
                supported_features=tuple(payload.get("supportedGateFeatures") or ()),
            )
        return gates

    async def async_gate_step_by_step(self, gate: WisniowskiGate) -> None:
        """Send the step-by-step gate command."""

        await self._async_rest_post(
            "/gate/stepByStep",
            {"deviceId": gate.device_id, "channelId": gate.channel_id},
        )

    async def async_gate_set_direction(self, gate: WisniowskiGate, direction: int) -> None:
        """Set gate direction. 1 stop, 2 open, 3 close."""

        await self._async_rest_post(
            "/gate/setDirection",
            {"deviceId": gate.device_id, "channelId": gate.channel_id, "direction": direction},
        )

    async def async_gate_set_position(self, gate: WisniowskiGate, position: int) -> None:
        """Set gate target position."""

        await self._async_rest_post(
            "/gate/setPosition",
            {"deviceId": gate.device_id, "channelId": gate.channel_id, "position": position},
        )

    async def _async_rest_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {await self.async_access_token()}",
            "Content-Type": "application/json",
            "installationId": str(self.installation_id or ""),
        }
        try:
            async with self._session.post(
                f"{self.broker_url}{path}",
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                data = await response.json(content_type=None)
                if response.status == 401:
                    await self._force_refresh()
                    return await self._async_rest_post(path, payload)
                if response.status >= 400:
                    raise WisniowskiApiError(str(data))
                return data
        except (ClientError, asyncio.TimeoutError) as exc:
            raise WisniowskiApiError(str(exc)) from exc

    async def async_subscribe(self, gate_ids: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Yield GraphQL subscription updates."""

        if not self.installation_id:
            await self.async_initialize()

        token = await self.async_access_token()
        ws_url = f"{self.broker_url}/graphql/".replace("https://", "wss://").replace("http://", "ws://")
        headers = {"X-Installation-Subtype": INSTALLATION_SUBTYPE}

        async with self._session.ws_connect(
            ws_url,
            headers=headers,
            protocols=("graphql-transport-ws",),
            heartbeat=30,
        ) as ws:
            await ws.send_json({"type": "connection_init", "payload": {"Authorization": f"Bearer {token}"}})
            await _wait_for_connection_ack(ws)

            await ws.send_json(
                {
                    "id": "device-connection",
                    "type": "subscribe",
                    "payload": {
                        "operationName": "OnDeviceConnectionStateChange",
                        "query": ON_DEVICE_CONNECTION_STATE_CHANGE,
                        "variables": {"installationId": self.installation_id},
                    },
                }
            )
            for channel_id in gate_ids:
                await ws.send_json(
                    {
                        "id": f"gate:{channel_id}",
                        "type": "subscribe",
                        "payload": {
                            "operationName": "onGateStatusChanged",
                            "query": ON_GATE_STATUS_CHANGED,
                            "variables": {"channelId": channel_id},
                        },
                    }
                )

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")
                    if msg_type == "next":
                        yield data.get("payload", {}).get("data", {})
                    elif msg_type == "ping":
                        await ws.send_json({"type": "pong"})
                    elif msg_type == "error":
                        raise WisniowskiApiError(str(data.get("payload")))
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    raise WisniowskiApiError("GraphQL websocket closed")

    @staticmethod
    def update_gate_status(gate: WisniowskiGate, payload: dict[str, Any]) -> WisniowskiGate:
        """Return gate with a status subscription payload applied."""

        return replace(
            gate,
            position=_coerce_int(payload.get("position")),
            direction=payload.get("direction"),
            lock_status=payload.get("lockStatus"),
        )

    @staticmethod
    def update_gate_connection(gate: WisniowskiGate, state: str) -> WisniowskiGate:
        """Return gate with a connection-state subscription payload applied."""

        return replace(gate, connection_state=state)

    @staticmethod
    def gate_unique_id(gate: WisniowskiGate) -> str:
        """Return a stable unique id for a gate."""

        if compact_mac := _compact_mac(gate.mac_address):
            return f"{compact_mac}_{gate.channel_id}"
        return f"{gate.device_id}_{gate.channel_id}"

    @staticmethod
    def supports_set_direction(gate: WisniowskiGate) -> bool:
        """Return whether the gate supports direct open/close/stop commands."""

        if gate.supported_features:
            return "SET_DIR" in gate.supported_features
        return gate.gate_mode == "ROLL_UP"

    @staticmethod
    def supports_set_position(gate: WisniowskiGate) -> bool:
        """Return whether the gate supports absolute position commands."""

        if gate.supported_features:
            return "SET_POS" in gate.supported_features
        return gate.gate_mode == "ROLL_UP"

    @staticmethod
    def supports_step_by_step(gate: WisniowskiGate) -> bool:
        """Return whether the gate supports step-by-step control."""

        if gate.supported_features:
            return "STEP_BY_STEP" in gate.supported_features
        return gate.gate_mode == "STEP_BY_STEP"


async def _wait_for_connection_ack(ws: Any) -> None:
    async with asyncio.timeout(10):
        while True:
            msg = await ws.receive()
            if msg.type != WSMsgType.TEXT:
                raise WisniowskiApiError("GraphQL websocket closed before ack")
            data = json.loads(msg.data)
            if data.get("type") == "connection_ack":
                return
            if data.get("type") == "error":
                raise WisniowskiApiError(str(data.get("payload")))


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
