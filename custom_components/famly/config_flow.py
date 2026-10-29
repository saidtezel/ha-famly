"""Config flow for Famly."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    FamlyAuthChallengeError,
    FamlyAuthError,
    FamlyClient,
    FamlyConnectionError,
    FamlyError,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
    REGIONS,
)
from .parser import parse_children

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
        vol.Required(CONF_REGION, default=DEFAULT_REGION): SelectSelector(
            SelectSelectorConfig(
                options=list(REGIONS),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="region",
            )
        ),
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


async def _validate(
    hass: HomeAssistant, email: str, password: str, region: str, device_id: str
) -> dict[str, Any]:
    """Log in and return what the entry needs. Raises Famly errors."""
    client = FamlyClient(
        async_get_clientsession(hass),
        REGIONS[region],
        email,
        password,
        device_id=device_id,
    )
    token = await client.login()
    me = await client.get_me()
    login_id = me.get("loginId")
    if not login_id:
        raise FamlyError("Famly did not return a login id")
    name = ((me.get("name") or {}).get("fullName") or "").strip()
    return {
        "login_id": str(login_id),
        "title": name or email,
        "token": token,
        "children": parse_children(me),
    }


def _error_key(err: Exception) -> str:
    if isinstance(err, FamlyAuthChallengeError):
        return "auth_challenge"
    if isinstance(err, FamlyAuthError):
        return "invalid_auth"
    if isinstance(err, FamlyConnectionError):
        return "cannot_connect"
    return "unknown"


class FamlyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Famly."""

    VERSION = 1

    _error_detail: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            device_id = str(uuid.uuid4())
            try:
                info = await _validate(
                    self.hass,
                    email,
                    user_input[CONF_PASSWORD],
                    user_input[CONF_REGION],
                    device_id,
                )
            except FamlyError as err:
                errors["base"] = _error_key(err)
                self._error_detail = str(err)
                _LOGGER.warning("Famly login failed (%s): %s", errors["base"], err)
            except Exception as err:
                _LOGGER.exception("Unexpected error validating Famly login")
                errors["base"] = "unknown"
                self._error_detail = f"{type(err).__name__}: {err}"
            else:
                if not info["children"]:
                    errors["base"] = "no_children"
                else:
                    await self.async_set_unique_id(info["login_id"])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=info["title"],
                        data={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_REGION: user_input[CONF_REGION],
                            CONF_DEVICE_ID: device_id,
                            CONF_ACCESS_TOKEN: info["token"],
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA,
                {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD},
            ),
            errors=errors,
            description_placeholders={"error_detail": self._error_detail},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await _validate(
                    self.hass,
                    entry.data[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    entry.data.get(CONF_REGION, DEFAULT_REGION),
                    entry.data.get(CONF_DEVICE_ID) or str(uuid.uuid4()),
                )
            except FamlyError as err:
                errors["base"] = _error_key(err)
                self._error_detail = str(err)
                _LOGGER.warning("Famly login failed (%s): %s", errors["base"], err)
            except Exception as err:
                _LOGGER.exception("Unexpected error validating Famly login")
                errors["base"] = "unknown"
                self._error_detail = f"{type(err).__name__}: {err}"
            else:
                await self.async_set_unique_id(info["login_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_ACCESS_TOKEN: info["token"],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={
                "email": entry.data[CONF_EMAIL],
                "error_detail": self._error_detail,
            },
        )
