from __future__ import annotations

from datetime import timedelta
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from cool_open_client.cool_automation_client import CoolAutomationClient
from cool_open_client.unit import HVACUnit

from .const import DOMAIN, RECONCILE_INTERVAL_MINUTES

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

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=RECONCILE_INTERVAL_MINUTES),
        )

    async def _async_update_data(self):
        """Fetch data from Coolmaster.

        Issues a single bulk request for all units and distributes the
        resulting UnitUpdateMessage objects to the in-memory HVACUnit
        instances. Replaces the previous per-unit fan-out which caused
        excessive API traffic on large installations.
        """
        try:
            updates = await self._client.get_updated_controllable_units()
        except OSError as error:
            raise UpdateFailed from error
        except Exception as error:
            # Surface as UpdateFailed so HA keeps the last known good data
            # instead of blanking entities. Token expiry and transport
            # failures both land here (same surface as the prior code).
            raise UpdateFailed(f"Bulk unit update failed: {error}") from error

        data: dict[str, HVACUnit] = {}
        for unit in self.units:
            message = updates.get(unit.id)
            if message is not None:
                # The coordinator notifies listeners itself; the library no longer
                # exposes a with_callback parameter on _update_unit.
                unit._update_unit(message)
            # A unit absent from the bulk response keeps its last-known state.
            data[unit.id] = unit
            unit.reset_update()
        return data

    @property
    def client(self):
        return self._client
