"""Event entity that fires for each new entry in a child's activity log."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FamlyConfigEntry
from .const import EVENT_TYPES
from .coordinator import FamlyCoordinator
from .entity import FamlyChildEntity
from .parser import Child

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamlyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        FamlyActivityEvent(coordinator, child) for child in coordinator.children
    )


class FamlyActivityEvent(FamlyChildEntity, EventEntity):
    """Fires check_in, nap_start, nappy_change, meal, … as they are logged."""

    _attr_translation_key = "activity"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: FamlyCoordinator, child: Child) -> None:
        super().__init__(coordinator, child, "activity")
        data = coordinator.data
        self._seen_refresh = data.refresh_id if data else 0

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        if not data or data.refresh_id == self._seen_refresh:
            super()._handle_coordinator_update()
            return
        self._seen_refresh = data.refresh_id
        events = data.new_events.get(self._child_id, [])
        if not events:
            super()._handle_coordinator_update()
            return
        for event_type, activity in events:
            self._trigger_event(event_type, activity.as_dict())
            self.async_write_ha_state()
