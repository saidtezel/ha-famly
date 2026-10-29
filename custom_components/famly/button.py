"""Button to run the Immich photo sync on demand."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FamlyConfigEntry
from .coordinator import FamlyCoordinator
from .entity import FamlyAccountEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamlyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.photo_sync and coordinator.photo_sync.enabled:
        async_add_entities([FamlySyncPhotosButton(coordinator)])


class FamlySyncPhotosButton(FamlyAccountEntity, ButtonEntity):
    """Check the news feed and upload new photos to Immich now."""

    _attr_translation_key = "sync_photos"

    def __init__(self, coordinator: FamlyCoordinator) -> None:
        super().__init__(coordinator, "sync_photos")

    async def async_press(self) -> None:
        assert self.coordinator.photo_sync
        # Runs in the background: an import can take a while.
        self.coordinator.photo_sync.async_request_sync()
