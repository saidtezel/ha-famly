"""Binary sensors for Famly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FamlyConfigEntry
from .const import STATUS_SICK
from .coordinator import FamlyCoordinator
from .entity import FamlyChildEntity
from .parser import Child, ChildState

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class FamlyBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a per-child binary sensor."""

    value_fn: Callable[[ChildState], bool]


BINARY_SENSORS: tuple[FamlyBinarySensorDescription, ...] = (
    FamlyBinarySensorDescription(
        key="at_nursery",
        translation_key="at_nursery",
        device_class=BinarySensorDeviceClass.PRESENCE,
        value_fn=lambda s: s.checked_in,
    ),
    FamlyBinarySensorDescription(
        key="sleeping",
        translation_key="sleeping",
        value_fn=lambda s: s.sleeping,
    ),
    FamlyBinarySensorDescription(
        key="sick",
        translation_key="sick",
        value_fn=lambda s: s.status == STATUS_SICK,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamlyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        FamlyBinarySensor(coordinator, child, description)
        for child in coordinator.children
        for description in BINARY_SENSORS
    )


class FamlyBinarySensor(FamlyChildEntity, BinarySensorEntity):
    """A per-child binary sensor."""

    entity_description: FamlyBinarySensorDescription

    def __init__(
        self,
        coordinator: FamlyCoordinator,
        child: Child,
        description: FamlyBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, child, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        state = self.child_state
        return self.entity_description.value_fn(state) if state else None
