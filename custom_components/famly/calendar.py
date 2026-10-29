"""Calendar of nursery events and absences for each child."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import FamlyConfigEntry
from .api import FamlyError
from .coordinator import FamlyCoordinator
from .entity import FamlyChildEntity
from .parser import KIND_EVENT, KIND_LEAVE, Activity, Child, parse_calendar

PARALLEL_UPDATES = 1
MAX_RANGE = timedelta(days=62)
DEFAULT_EVENT_LENGTH = timedelta(minutes=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamlyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        FamlyCalendar(coordinator, child) for child in coordinator.children
    )


def _to_calendar_event(act: Activity) -> CalendarEvent | None:
    if act.kind == KIND_LEAVE and act.day:
        leave_type = (act.data.get("leave_type") or "").title() or "Absence"
        return CalendarEvent(
            start=act.day,
            end=act.day + timedelta(days=1),
            summary=act.title or leave_type,
            description=act.data.get("reason"),
            uid=act.uid,
        )
    if act.kind == KIND_EVENT and act.start:
        end = act.end if act.end and act.end > act.start else act.start + DEFAULT_EVENT_LENGTH
        return CalendarEvent(
            start=act.start,
            end=end,
            summary=act.title,
            description=act.subtitle or None,
            uid=act.uid,
        )
    return None


class FamlyCalendar(FamlyChildEntity, CalendarEntity):
    """Nursery events (stay-and-play, trips, closures) and sick/holiday days."""

    _attr_translation_key = "nursery"

    def __init__(self, coordinator: FamlyCoordinator, child: Child) -> None:
        super().__init__(coordinator, child, "calendar")

    @property
    def event(self) -> CalendarEvent | None:
        data = self.coordinator.data
        if not data:
            return None
        now = dt_util.now()
        for act in data.upcoming.get(self._child_id, []):
            event = _to_calendar_event(act)
            if event is None:
                continue
            end = event.end_datetime_local
            if end > now:
                return event
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if end_date - start_date > MAX_RANGE:
            end_date = start_date + MAX_RANGE
        try:
            payload = await self.coordinator.client.get_calendar(
                self._child_id, start_date.date(), end_date.date()
            )
        except FamlyError as err:
            raise HomeAssistantError(f"Could not load Famly calendar: {err}") from err

        events: list[CalendarEvent] = []
        for act in parse_calendar(payload):
            event = _to_calendar_event(act)
            if event and event.end_datetime_local > start_date and event.start_datetime_local < end_date:
                events.append(event)
        return events
