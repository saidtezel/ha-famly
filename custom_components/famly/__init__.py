"""The Famly integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FamlyAuthError, FamlyClient, FamlyError
from .const import CONF_ACCESS_TOKEN, CONF_DEVICE_ID, CONF_REGION, DEFAULT_REGION, REGIONS
from .coordinator import FamlyCoordinator
from .parser import parse_children
from .photo_sync import PhotoSync

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SENSOR,
]

type FamlyConfigEntry = ConfigEntry[FamlyCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: FamlyConfigEntry) -> bool:
    """Set up Famly from a config entry."""

    @callback
    def _store_token(token: str) -> None:
        # Keep the latest session token so a restart does not need a fresh login.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_ACCESS_TOKEN: token}
        )

    client = FamlyClient(
        async_get_clientsession(hass),
        REGIONS[entry.data.get(CONF_REGION, DEFAULT_REGION)],
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        device_id=entry.data.get(CONF_DEVICE_ID),
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        on_token=_store_token,
    )

    try:
        me = await client.get_me()
    except FamlyAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except FamlyError as err:
        raise ConfigEntryNotReady(f"Could not reach Famly: {err}") from err

    children = parse_children(me)
    if not children:
        raise ConfigEntryNotReady("No children found on this Famly account")

    coordinator = FamlyCoordinator(hass, entry, client, children)
    await coordinator.async_config_entry_first_refresh()
    coordinator.photo_sync = PhotoSync(hass, entry, client)
    await coordinator.photo_sync.async_start()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FamlyConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
