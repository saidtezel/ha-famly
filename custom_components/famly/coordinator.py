"""Data update coordinator for Famly."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FamlyAuthError, FamlyClient, FamlyError
from .const import (
    ACTIVE_INTERVAL,
    DOMAIN,
    IDLE_INTERVAL,
    OPENING_HOURS_INTERVAL,
    OPENING_MARGIN,
    PHOTO_INTERVAL,
    UPCOMING_DAYS,
    UPCOMING_INTERVAL,
)
from .parser import (
    KIND_EVENT,
    KIND_LEAVE,
    Activity,
    Child,
    ChildState,
    build_child_state,
    diff_activities,
    is_within_opening_hours,
    parse_calendar,
)

if TYPE_CHECKING:
    from . import FamlyConfigEntry
    from .photo_sync import PhotoSync

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FamlyData:
    """Snapshot handed to entities."""

    children: dict[str, ChildState] = field(default_factory=dict)
    new_events: dict[str, list[tuple[str, Activity]]] = field(default_factory=dict)
    photos: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    upcoming: dict[str, list[Activity]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    refresh_id: int = 0


class FamlyCoordinator(DataUpdateCoordinator[FamlyData]):
    """Polls Famly for every child on the account."""

    config_entry: FamlyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: FamlyConfigEntry,
        client: FamlyClient,
        children: list[Child],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=ACTIVE_INTERVAL,
        )
        self.client = client
        self.children = children
        self._previous: dict[str, dict[str, Activity]] = {}
        self._opening_hours: dict[str, list[dict[str, Any]]] = {}
        self._opening_hours_at: dict[str, datetime] = {}
        self._photos: dict[str, dict[str, Any] | None] = {}
        self._photos_at: dict[str, datetime] = {}
        self._upcoming: dict[str, list[Activity]] = {}
        self._upcoming_at: dict[str, datetime] = {}
        self._counts: dict[str, int] = {}
        self._refresh_id = 0
        self.photo_sync: PhotoSync | None = None

    async def _async_update_data(self) -> FamlyData:
        try:
            return await self._fetch()
        except FamlyAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except FamlyError as err:
            raise UpdateFailed(f"Error talking to Famly: {err}") from err

    async def _fetch(self) -> FamlyData:
        now = dt_util.utcnow()
        data = FamlyData()
        active = False

        for child in self.children:
            cid = child.child_id
            summary = await self.client.get_child_summary(cid)
            tz_name = (summary.get("institution") or {}).get("timezone") or str(
                self.hass.config.time_zone
            )
            tz = dt_util.get_time_zone(tz_name) or dt_util.get_default_time_zone()
            local_now = now.astimezone(tz)
            today = local_now.date()

            calendar = await self.client.get_calendar(cid, today, today)
            state = build_child_state(summary, parse_calendar(calendar), today)
            if not state.name:
                state.name = child.name
                state.first_name = child.name

            data.children[cid] = state
            data.new_events[cid] = diff_activities(self._previous.get(cid), state.today)
            self._previous[cid] = {a.uid: a for a in state.today}

            institution_id = state.institution_id
            if institution_id:
                await self._refresh_opening_hours(institution_id, now)
                hours = self._opening_hours.get(institution_id, [])
                if is_within_opening_hours(hours, local_now, OPENING_MARGIN):
                    active = True
            else:
                active = True
            if state.checked_in:
                active = True

            await self._refresh_photo(cid, now)
            await self._refresh_upcoming(cid, today, now)
            data.photos[cid] = self._photos.get(cid)
            data.upcoming[cid] = self._upcoming.get(cid, [])

        try:
            self._counts = await self.client.get_notification_counts()
        except FamlyAuthError:
            raise
        except FamlyError as err:
            _LOGGER.debug("Could not refresh notification counts: %s", err)
        data.counts = dict(self._counts)

        self._refresh_id += 1
        data.refresh_id = self._refresh_id

        interval = ACTIVE_INTERVAL if active else IDLE_INTERVAL
        if self.update_interval != interval:
            _LOGGER.debug("Famly polling interval now %s", interval)
            self.update_interval = interval
        return data

    # Slower-moving data. Failures here keep the previous value rather than
    # failing the whole refresh.

    async def _refresh_opening_hours(self, institution_id: str, now: datetime) -> None:
        fetched = self._opening_hours_at.get(institution_id)
        if fetched and now - fetched < OPENING_HOURS_INTERVAL:
            return
        try:
            self._opening_hours[institution_id] = await self.client.get_opening_hours(
                institution_id
            )
            self._opening_hours_at[institution_id] = now
        except FamlyAuthError:
            raise
        except FamlyError as err:
            _LOGGER.debug("Could not fetch opening hours: %s", err)

    async def _refresh_photo(self, child_id: str, now: datetime) -> None:
        fetched = self._photos_at.get(child_id)
        if fetched and now - fetched < PHOTO_INTERVAL:
            return
        try:
            images = await self.client.get_tagged_images(child_id, limit=1)
            self._photos[child_id] = images[0] if images else None
            self._photos_at[child_id] = now
        except FamlyAuthError:
            raise
        except FamlyError as err:
            _LOGGER.debug("Could not fetch latest photo: %s", err)

    async def _refresh_upcoming(self, child_id: str, today: date, now: datetime) -> None:
        fetched = self._upcoming_at.get(child_id)
        if fetched and now - fetched < UPCOMING_INTERVAL:
            return
        try:
            payload = await self.client.get_calendar(
                child_id, today, today + timedelta(days=UPCOMING_DAYS)
            )
            self._upcoming[child_id] = [
                a for a in parse_calendar(payload) if a.kind in (KIND_EVENT, KIND_LEAVE)
            ]
            self._upcoming_at[child_id] = now
        except FamlyAuthError:
            raise
        except FamlyError as err:
            _LOGGER.debug("Could not fetch upcoming events: %s", err)
