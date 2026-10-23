"""Coordinator for Wisniowski Connected."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WisniowskiClient, WisniowskiError, WisniowskiGate
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True)
class WisniowskiRuntime:
    """Runtime objects shared by Wisniowski entities."""

    client: WisniowskiClient
    coordinator: "WisniowskiCoordinator"


class WisniowskiCoordinator(DataUpdateCoordinator[dict[str, WisniowskiGate]]):
    """Coordinator backed by GraphQL subscriptions with slow polling fallback."""

    def __init__(self, hass: HomeAssistant, client: WisniowskiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self._subscription_task: asyncio.Task[None] | None = None
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        self._stopped = False

    async def _async_update_data(self) -> dict[str, WisniowskiGate]:
        try:
            return await self.client.async_get_gates()
        except WisniowskiError as exc:
            raise UpdateFailed(str(exc)) from exc

    async def async_start(self) -> None:
        """Start first refresh and websocket listener."""

        await self.async_config_entry_first_refresh()
        # The listener never finishes, so startup must not wait for it as a tracked task.
        self._subscription_task = self.hass.async_create_background_task(
            self._subscription_loop(), f"{DOMAIN} subscription"
        )

    async def async_stop(self) -> None:
        """Stop websocket listener."""

        self._stopped = True
        if self._subscription_task:
            self._subscription_task.cancel()
            try:
                await self._subscription_task
            except asyncio.CancelledError:
                pass
        for task in set(self._refresh_tasks):
            task.cancel()

    async def _subscription_loop(self) -> None:
        backoff = 2
        while not self._stopped:
            gate_ids = list((self.data or {}).keys())
            if not gate_ids:
                await asyncio.sleep(30)
                continue

            try:
                async for event in self.client.async_subscribe(gate_ids):
                    self._apply_subscription_event(event)
                    backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Wisniowski subscription disconnected: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _apply_subscription_event(self, event: dict[str, Any]) -> None:
        current = dict(self.data or {})
        if payload := event.get("onGateStatusChanged"):
            channel_id = str(payload.get("channelId"))
            if gate := current.get(channel_id):
                current[channel_id] = self.client.update_gate_status(gate, payload)
                self.async_set_updated_data(current)
            return

        if payload := event.get("onDeviceConnectionStateChange"):
            device_id = str(payload.get("deviceId"))
            state = str(payload.get("deviceConnectionState"))
            changed = False
            for channel_id, gate in list(current.items()):
                if gate.device_id == device_id:
                    current[channel_id] = self.client.update_gate_connection(gate, state)
                    changed = True
            if changed:
                self.async_set_updated_data(current)

    async def async_step_by_step(self, gate: WisniowskiGate) -> None:
        """Send a step-by-step command."""

        await self.client.async_gate_step_by_step(gate)
        self.async_schedule_update_ha_state_soon()

    async def async_open(self, gate: WisniowskiGate) -> None:
        """Open a gate."""

        await self.client.async_gate_set_direction(gate, 2)
        self.async_schedule_update_ha_state_soon()

    async def async_close(self, gate: WisniowskiGate) -> None:
        """Close a gate."""

        await self.client.async_gate_set_direction(gate, 3)
        self.async_schedule_update_ha_state_soon()

    async def async_stop_cover(self, gate: WisniowskiGate) -> None:
        """Stop a moving gate."""

        await self.client.async_gate_set_direction(gate, 1)
        self.async_schedule_update_ha_state_soon()

    async def async_set_position(self, gate: WisniowskiGate, position: int) -> None:
        """Set gate position."""

        await self.client.async_gate_set_position(gate, 100 - position)
        self.async_schedule_update_ha_state_soon()

    def async_schedule_update_ha_state_soon(self) -> None:
        """Schedule a delayed refresh after the cloud accepts a command."""

        async def _refresh_later() -> None:
            await asyncio.sleep(2)
            if not self._stopped:
                await self.async_request_refresh()

        task = self.hass.async_create_task(_refresh_later())
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)
