from cool_open_client.unit import HVACUnit
from homeassistant.helpers.entity import DeviceInfo


from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN
from .coordinator import CoolAutomationDataUpdateCoordinator


class CoolAutomationBaseEntity(CoordinatorEntity[CoolAutomationDataUpdateCoordinator]):
    """Represents a Cool Automation entity"""

    def __init__(
        self,
        coordinator: CoolAutomationDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        """Initiate Unit Entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._client = coordinator.client

    @property
    def unit_data(self) -> HVACUnit:
        return self.coordinator.data[self._device_id]


class CoolAutomationUnitBaseEntity(CoolAutomationBaseEntity):
    """Representation of CoolAutomation controlled unit"""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CoolAutomationDataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self.__attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.unit_data.id)},
            name=self.unit_data.name,
            manufacturer="CoolAutomations",
            suggested_area=self.unit_data.name,
        )
