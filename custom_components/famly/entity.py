"""Base entities for Famly."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FamlyCoordinator
from .parser import Child, ChildState


class FamlyChildEntity(CoordinatorEntity[FamlyCoordinator]):
    """An entity that belongs to one child (one device per child)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FamlyCoordinator, child: Child, key: str) -> None:
        super().__init__(coordinator)
        self._child_id = child.child_id
        self._attr_unique_id = f"{child.child_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, child.child_id)},
            name=child.name,
            manufacturer="Famly",
            model=child.institution_name or "Child",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def child_state(self) -> ChildState | None:
        data = self.coordinator.data
        return data.children.get(self._child_id) if data else None

    @property
    def available(self) -> bool:
        return super().available and self.child_state is not None


class FamlyAccountEntity(CoordinatorEntity[FamlyCoordinator]):
    """An entity that belongs to the Famly login itself."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FamlyCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        account_id = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{account_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name=f"Famly {entry.title}",
            manufacturer="Famly",
            model="Parent account",
            entry_type=DeviceEntryType.SERVICE,
        )
