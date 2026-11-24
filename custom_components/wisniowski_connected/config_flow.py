"""Config flow for Wisniowski Connected."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WisniowskiAuthError, WisniowskiClient, WisniowskiError
from .const import (
    CLIENT_ID,
    CONF_AUTH_RESPONSE_URL,
    CONF_USER_ID,
    DOMAIN,
    KEYCLOAK_AUTH_URL,
    REDIRECT_URI,
)


class WisniowskiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Wisniowski Connected."""

    VERSION = 1

    def __init__(self) -> None:
        self._code_verifier: str | None = None
        self._state: str | None = None
        self._authorization_url: str | None = None
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle authorization-code paste step."""

        if not self._authorization_url:
            self._prepare_authorization()

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                code = self._extract_code(user_input[CONF_AUTH_RESPONSE_URL])
                client = await WisniowskiClient.async_from_code(
                    async_get_clientsession(self.hass),
                    code,
                    self._code_verifier or "",
                )
                await client.async_initialize()
                gates = await client.async_get_gates()
                if not gates:
                    errors["base"] = "no_devices"
                    self._prepare_authorization()
                    return self.async_show_form(
                        step_id="user",
                        data_schema=vol.Schema({vol.Required(CONF_AUTH_RESPONSE_URL): str}),
                        errors=errors,
                        description_placeholders={"authorization_url": self._authorization_url or ""},
                    )
                userinfo = await client.async_userinfo()
            except WisniowskiAuthError:
                errors["base"] = "invalid_auth"
                self._prepare_authorization()
            except WisniowskiError:
                errors["base"] = "cannot_connect"
                self._prepare_authorization()
            else:
                user_id = str(userinfo.get("sub") or userinfo.get("email") or "wisniowski")
                data = client.data
                data[CONF_USER_ID] = user_id
                title = str(userinfo.get("email") or "Wisniowski Connected")
                if len(gates) == 1:
                    title = next(iter(gates.values())).name

                if self._reauth_entry is not None:
                    if self._reauth_entry.unique_id and self._reauth_entry.unique_id != user_id:
                        errors["base"] = "wrong_account"
                        self._prepare_authorization()
                    else:
                        return self.async_update_reload_and_abort(
                            self._reauth_entry, data=data, title=title, reason="reauth_success"
                        )
                else:
                    await self.async_set_unique_id(user_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_AUTH_RESPONSE_URL): str}),
            errors=errors,
            description_placeholders={"authorization_url": self._authorization_url or ""},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle reauthorization when the stored refresh token stops working."""

        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_user()

    def _prepare_authorization(self) -> None:
        self._code_verifier = _random_urlsafe(64)
        self._state = _random_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._code_verifier.encode()).digest())
        code_challenge = challenge.rstrip(b"=").decode()
        self._authorization_url = (
            KEYCLOAK_AUTH_URL
            + "?"
            + urlencode(
                {
                    "client_id": CLIENT_ID,
                    "response_type": "code",
                    "scope": "openid offline_access profile email",
                    "redirect_uri": REDIRECT_URI,
                    "state": self._state,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    def _extract_code(self, response_url: str) -> str:
        parsed = urlparse(response_url)
        params = parse_qs(parsed.query or parsed.fragment)
        state = (params.get("state") or [""])[0]
        if state != self._state:
            raise WisniowskiAuthError("OAuth state mismatch")
        code = (params.get("code") or [""])[0]
        if not code:
            raise WisniowskiAuthError("Missing OAuth code")
        return code


def _random_urlsafe(length: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(length)).rstrip(b"=").decode()
