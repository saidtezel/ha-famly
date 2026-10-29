"""Sensors for Famly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import FamlyConfigEntry
from .const import STATUS_OPTIONS
from .coordinator import FamlyCoordinator
from .entity import FamlyAccountEntity, FamlyChildEntity
from .parser import Activity, Child, ChildState

PARALLEL_UPDATES = 0


def _times(items: list[Activity]) -> list[dict[str, Any]]:
    return [a.as_dict() for a in items]


def _last(items: list[Activity]) -> Activity | None:
    return items[-1] if items else None


@dataclass(frozen=True, kw_only=True)
class FamlyChildSensorDescription(SensorEntityDescription):
    """Describes a per-child sensor."""

    value_fn: Callable[[ChildState], StateType | datetime]
    attrs_fn: Callable[[ChildState], dict[str, Any]] | None = None


CHILD_SENSORS: tuple[FamlyChildSensorDescription, ...] = (
    FamlyChildSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUS_OPTIONS,
        value_fn=lambda s: s.status,
        attrs_fn=lambda s: {
            "group": s.group_name,
            "institution": s.institution_name,
            "pickup_time": s.pickup_time,
            "pickup_name": s.pickup_name,
            "key_workers": s.key_workers,
        },
    ),
    FamlyChildSensorDescription(
        key="room",
        translation_key="room",
        value_fn=lambda s: s.room,
    ),
    FamlyChildSensorDescription(
        key="arrived_at",
        translation_key="arrived_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.arrived_at,
    ),
    FamlyChildSensorDescription(
        key="left_at",
        translation_key="left_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.left_at,
    ),
    FamlyChildSensorDescription(
        key="last_nap_start",
        translation_key="last_nap_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.last_nap_start,
    ),
    FamlyChildSensorDescription(
        key="last_nap_end",
        translation_key="last_nap_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.last_nap_end,
    ),
    FamlyChildSensorDescription(
        key="sleep_today",
        translation_key="sleep_today",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda s: s.sleep_minutes_today,
        attrs_fn=lambda s: {"naps": _times(s.naps_today)},
    ),
    FamlyChildSensorDescription(
        key="last_nappy_change",
        translation_key="last_nappy_change",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: (n.start if (n := _last(s.nappies_today)) else None),
        attrs_fn=lambda s: (
            {"type": n.data.get("nappy_type"), "note": n.data.get("note")}
            if (n := _last(s.nappies_today))
            else {}
        ),
    ),
    FamlyChildSensorDescription(
        key="nappy_changes_today",
        translation_key="nappy_changes_today",
        value_fn=lambda s: len(s.nappies_today),
        attrs_fn=lambda s: {"changes": _times(s.nappies_today)},
    ),
    FamlyChildSensorDescription(
        key="last_meal",
        translation_key="last_meal",
        value_fn=lambda s: (m.data.get("meal") if (m := _last(s.meals_today)) else None),
        attrs_fn=lambda s: (
            {"time": m.start.isoformat() if m.start else None}
            if (m := _last(s.meals_today))
            else {}
        ),
    ),
    FamlyChildSensorDescription(
        key="meals_today",
        translation_key="meals_today",
        value_fn=lambda s: len(s.meals_today),
        attrs_fn=lambda s: {"meals": _times(s.meals_today)},
    ),
    FamlyChildSensorDescription(
        key="last_activity",
        translation_key="last_activity",
        value_fn=lambda s: (s.last_activity.title[:255] if s.last_activity else None),
        attrs_fn=lambda s: (s.last_activity.as_dict() if s.last_activity else {}),
    ),
)


@dataclass(frozen=True, kw_only=True)
class FamlyAccountSensorDescription(SensorEntityDescription):
    """Describes an account-level sensor."""

    count_key: str


ACCOUNT_SENSORS: tuple[FamlyAccountSensorDescription, ...] = (
    FamlyAccountSensorDescription(
        key="unread_messages",
        translation_key="unread_messages",
        count_key="messages",
    ),
    FamlyAccountSensorDescription(
        key="unseen_notifications",
        translation_key="unseen_notifications",
        count_key="notifications",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamlyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        FamlyChildSensor(coordinator, child, description)
        for child in coordinator.children
        for description in CHILD_SENSORS
    ]
    entities.extend(
        FamlyAccountSensor(coordinator, description) for description in ACCOUNT_SENSORS
    )
    async_add_entities(entities)


class FamlyChildSensor(FamlyChildEntity, SensorEntity):
    """A per-child sensor."""

    entity_description: FamlyChildSensorDescription

    def __init__(
        self,
        coordinator: FamlyCoordinator,
        child: Child,
        description: FamlyChildSensorDescription,
    ) -> None:
        super().__init__(coordinator, child, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        state = self.child_state
        return self.entity_description.value_fn(state) if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self.child_state
        if not state or not self.entity_description.attrs_fn:
            return None
        return self.entity_description.attrs_fn(state)


class FamlyAccountSensor(FamlyAccountEntity, SensorEntity):
    """Unread counts for the login."""

    entity_description: FamlyAccountSensorDescription

    def __init__(
        self,
        coordinator: FamlyCoordinator,
        description: FamlyAccountSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if not data:
            return None
        return data.counts.get(self.entity_description.count_key)
