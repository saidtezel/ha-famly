"""Setup and entity tests for Famly."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.famly.api import FamlyAuthError, FamlyConnectionError

from .conftest import NOW, load


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.freeze_time(NOW)
async def test_setup_creates_entities(hass: HomeAssistant, mock_client, config_entry):
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("binary_sensor.robin_at_nursery").state == "on"
    assert hass.states.get("binary_sensor.robin_sleeping").state == "on"
    assert hass.states.get("binary_sensor.robin_sick").state == "off"

    status = hass.states.get("sensor.robin_status")
    assert status.state == "at_nursery"
    assert status.attributes["key_workers"] == ["Kim Keyworker"]

    assert hass.states.get("sensor.robin_room").state == "Garden"
    assert hass.states.get("sensor.robin_arrived").state == "2026-10-28T07:30:00+00:00"
    assert hass.states.get("sensor.robin_picked_up").state == "unknown"
    assert hass.states.get("sensor.robin_nappy_changes_today").state == "1"
    assert hass.states.get("sensor.robin_last_nappy_change").attributes["type"] == "WET"
    assert hass.states.get("sensor.robin_last_meal").state == "Lunch"
    assert hass.states.get("sensor.robin_meals_today").state == "1"
    # 12:10 -> 13:00 still asleep = 50 minutes
    assert float(hass.states.get("sensor.robin_sleep_today").state) == pytest.approx(50)
    assert hass.states.get("sensor.robin_last_nap_start").state == "2026-10-28T12:10:00+00:00"

    assert hass.states.get("sensor.famly_pat_parent_unread_messages").state == "2"
    assert hass.states.get("sensor.famly_pat_parent_unseen_notifications").state == "5"

    photo = hass.states.get("image.robin_latest_photo")
    assert photo is not None
    assert photo.state == "2026-10-28T13:00:00+00:00"

    cal = hass.states.get("calendar.robin_nursery")
    assert cal.state == "off"
    assert cal.attributes["message"] == "Autumn stay and play"

    event = hass.states.get("event.robin_activity")
    assert event.state == "unknown"  # nothing fired on first poll


@pytest.mark.freeze_time(NOW)
async def test_new_activity_fires_event(
    hass: HomeAssistant, mock_client, config_entry, freezer: FrozenDateTimeFactory
):
    await _setup(hass, config_entry)

    calendar = load("calendar.json")
    events = calendar[0]["days"][0]["events"]
    nap = next(e for e in events if e["originator"]["id"] == "n-1")
    nap["to"] = "2026-10-28T13:01:00+00:00"
    events.append(
        {
            "originator": {"type": "Famly.Daycare:Action", "id": "a-2"},
            "title": "Robin: Nappy Change - Dry",
            "subtitle": "Robin",
            "from": "2026-10-28T13:02:00+00:00",
            "to": None,
            "embed": {
                "type": "childAction",
                "actionType": "DIAPERCHANGE",
                "diaperingType": "DRY",
                "occuredOn": "2026-10-28T13:02:00+00:00",
            },
        }
    )
    mock_client.get_calendar.return_value = calendar

    fired: list[str] = []

    def _record(event):
        new = event.data["new_state"]
        old = event.data["old_state"]
        if (
            new
            and new.entity_id == "event.robin_activity"
            and new.attributes.get("event_type")
            and (old is None or old.attributes != new.attributes)
        ):
            fired.append(new.attributes["event_type"])

    hass.bus.async_listen("state_changed", _record)
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert fired == ["nap_end", "nappy_change"]
    state = hass.states.get("event.robin_activity")
    assert state.attributes["nappy_type"] == "DRY"
    assert hass.states.get("binary_sensor.robin_sleeping").state == "off"
    assert hass.states.get("sensor.robin_nappy_changes_today").state == "2"


@pytest.mark.freeze_time("2026-10-31T13:00:00+00:00")  # Saturday, closed
async def test_idle_polling_when_closed(hass: HomeAssistant, mock_client, config_entry):
    summary = load("summary.json")
    summary["child"]["checkedIn"] = False
    mock_client.get_child_summary.return_value = summary
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.update_interval == timedelta(minutes=30)


@pytest.mark.freeze_time(NOW)
async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_client, config_entry, freezer: FrozenDateTimeFactory
):
    await _setup(hass, config_entry)
    mock_client.get_child_summary.side_effect = FamlyAuthError("nope")
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_setup_retry_when_offline(hass: HomeAssistant, mock_client, config_entry):
    mock_client.get_me.side_effect = FamlyConnectionError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload(hass: HomeAssistant, mock_client, config_entry):
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
