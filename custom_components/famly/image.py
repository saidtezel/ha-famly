"""Latest tagged photo of each child."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FamlyConfigEntry
from .coordinator import FamlyCoordinator
from .entity import FamlyChildEntity
from .parser import Child, parse_dt

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamlyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        FamlyLatestPhoto(coordinator, child) for child in coordinator.children
    )


class FamlyLatestPhoto(FamlyChildEntity, ImageEntity):
    """The most recent photo the child is tagged in.

    Famly image URLs are signed and expire, so the image bytes are cached
    in Home Assistant and only re-downloaded when a new photo appears.
    """

    _attr_translation_key = "latest_photo"

    def __init__(self, coordinator: FamlyCoordinator, child: Child) -> None:
        FamlyChildEntity.__init__(self, coordinator, child, "latest_photo")
        ImageEntity.__init__(self, coordinator.hass)
        self._image_id: str | None = None
        self._sync_from_coordinator()

    def _photo(self) -> dict | None:
        data = self.coordinator.data
        return data.photos.get(self._child_id) if data else None

    def _sync_from_coordinator(self) -> None:
        photo = self._photo()
        image_id = photo.get("imageId") if photo else None
        if image_id != self._image_id:
            self._image_id = image_id
            self._cached_image = None
            self._attr_image_last_updated = (
                parse_dt(photo.get("createdAt")) if photo else None
            )

    @property
    def image_url(self) -> str | None:
        photo = self._photo()
        if not photo:
            return None
        big = photo.get("big") or {}
        return big.get("url") or photo.get("url_big") or photo.get("url")

    @property
    def image_last_updated(self) -> datetime | None:
        return self._attr_image_last_updated

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_from_coordinator()
        super()._handle_coordinator_update()
