"""The CoolAutomation Cloud Open Integration integration."""
from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from cool_open_client.hvac_units_factory import HVACUnitsFactory
from cool_open_client.cool_automation_client import CoolAutomationClient

from .const import DOMAIN, PLATFORMS
from .coordinator import CoolAutomationDataUpdateCoordinator

# TODO List the platforms that you want to support.
# For your initial PR, limit it to 1 platform.

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CoolAutomation Cloud Open Integration from a config entry."""
   
    # hass.data[DOMAIN] = config
    # conf: ConfigType | None = config.get(DOMAIN)

    # if conf is None:
    #     # If we have a config entry, setup is done by that config entry.
    #     # If there is no config entry, this should fail.
    #     return bool(hass.config_entries.async_entries(DOMAIN))

    _LOGGER.debug("async setup")
    token = entry.data["token"]
    try:
        units_factory = await HVACUnitsFactory.create(token=token)
        client = await CoolAutomationClient.create(token=token)
        units = await units_factory.generate_units_from_api()
        if not units:
            raise ConfigEntryNotReady
    except OSError as error:
        raise ConfigEntryNotReady() from error

    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # hass.config_entries.async_setup_platforms(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
