from datetime import timedelta
import logging
from typing import List
from homeassistant.config_entries import ConfigEntry

from homeassistant.core import HomeAssistant
from .const import DOMAIN, DEFAULT_SCAN_INTERVAL
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from cool_open_client.cool_automation_client import CoolAutomationClient
from cool_open_client.unit import HVACUnit
from cool_open_client.hvac_units_factory import HVACUnitsFactory

_LOGGER = logging.getLogger(__name__)


class CoolAutomationDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Coolmaster data."""

    data: List[HVACUnit]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: CoolAutomationClient):
        """Initialize global Coolmaster data updater."""
        _LOGGER.debug("Inniting")
        self._client = client
        client.open_socket()

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL))

    async def _async_update_data(self):
        """Fetch data from Coolmaster."""
        try:
            units_factory = HVACUnitsFactory(self._client)
            units = await units_factory.generate_units_from_api()
            data = {unit.id: unit for unit in units}
        except OSError as error:
            raise UpdateFailed from error
        return data

    @property
    def client(self):
        return self._client
