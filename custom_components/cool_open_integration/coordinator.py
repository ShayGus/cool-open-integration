from __future__ import annotations

from datetime import timedelta
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from cool_open_client.cool_automation_client import CoolAutomationClient
from cool_open_client.unit import HVACUnit

from .const import DOMAIN, POLL_INTERVAL

_LOGGER = logging.getLogger(__package__)


class CoolAutomationDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Coolmaster data."""

    data: dict[str, HVACUnit]

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: CoolAutomationClient, units: list[HVACUnit]
    ) -> None:
        """Initialize global Coolmaster data updater."""
        _LOGGER.debug("Init Cool Automation update coordinator")
        self._client = client
        self.hass = hass
        self.units = units

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=POLL_INTERVAL))

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
