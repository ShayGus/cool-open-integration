from datetime import timedelta
import logging
from typing import List
from homeassistant.config_entries import ConfigEntry

from homeassistant.core import HomeAssistant
from .const import DOMAIN, DEFAULT_SCAN_INTERVAL
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from cool_open_client.cool_automation_client import CoolAutomationClient
from cool_open_client.unit import HVACUnit

_LOGGER = logging.getLogger(__package__)


class CoolAutomationDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Coolmaster data."""

    data: List[HVACUnit]

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: CoolAutomationClient, units: List[HVACUnit]
    ) -> None:
        """Initialize global Coolmaster data updater."""
        _LOGGER.debug("Init Cool Automation update coordinator")
        _LOGGER.debug("Initializing CoolAutomation Open Inegration.................................................")
        self._client = client
        self.hass = hass
        self.units = units

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL))

    async def _async_update_data(self):
        """Fetch data from Coolmaster."""
        try:
            data = {}
            for unit in self.units:
                await unit.refresh()
                data[unit.id] = unit
                unit.reset_update()
        except OSError as error:
            raise UpdateFailed from error
        return data

    @property
    def client(self):
        return self._client
