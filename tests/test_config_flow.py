"""Config flow tests for Famly."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.famly.api import (
    FamlyAuthChallengeError,
    FamlyAuthError,
    FamlyConnectionError,
)
from custom_components.famly.const import CONF_ACCESS_TOKEN, CONF_DEVICE_ID, DOMAIN

USER_INPUT = {"email": "parent@example.com", "password": "secret", "region": "famly.co"}


async def test_user_flow_success(hass: HomeAssistant, mock_client):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Pat Parent"
    assert result["result"].unique_id == "login-1"
    data = result["data"]
    assert data["email"] == "parent@example.com"
    assert data["password"] == "secret"
    assert data[CONF_ACCESS_TOKEN] == "token-1"
    assert data[CONF_DEVICE_ID]


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (FamlyAuthError("bad"), "invalid_auth"),
        (FamlyAuthChallengeError("2fa"), "auth_challenge"),
        (FamlyConnectionError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recover(hass: HomeAssistant, mock_client, error, key):
    mock_client.login.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_no_children(hass: HomeAssistant, mock_client):
    mock_client.get_me.return_value = {"loginId": "login-1", "roles2": []}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "no_children"}


async def test_already_configured(hass: HomeAssistant, mock_client, config_entry):
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_client, config_entry):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mock_client.login.return_value = "token-2"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "new-secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["password"] == "new-secret"
    assert config_entry.data[CONF_ACCESS_TOKEN] == "token-2"


async def test_reauth_wrong_account(hass: HomeAssistant, mock_client, config_entry):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    mock_client.get_me.return_value = {
        **mock_client.get_me.return_value,
        "loginId": "someone-else",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "x"}
    )
    assert result["reason"] == "wrong_account"
