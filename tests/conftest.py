"""Fixtures for Famly tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.famly.const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_REGION,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-10-28T13:00:00+00:00"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.device_id = "device-1"
    client.login = AsyncMock(return_value="token-1")
    client.get_me = AsyncMock(return_value=load("me.json"))
    client.get_child_summary = AsyncMock(return_value=load("summary.json"))
    client.get_calendar = AsyncMock(return_value=load("calendar.json"))
    client.get_tagged_images = AsyncMock(return_value=load("images.json"))
    client.get_opening_hours = AsyncMock(return_value=load("openinghours.json"))
    client.get_notification_counts = AsyncMock(
        return_value={"messages": 2, "notifications": 5}
    )
    with (
        patch("custom_components.famly.FamlyClient", return_value=client),
        patch("custom_components.famly.config_flow.FamlyClient", return_value=client),
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="login-1",
        title="Pat Parent",
        data={
            "email": "parent@example.com",
            "password": "secret",
            CONF_REGION: "famly.co",
            CONF_DEVICE_ID: "device-1",
            CONF_ACCESS_TOKEN: "token-0",
        },
    )
