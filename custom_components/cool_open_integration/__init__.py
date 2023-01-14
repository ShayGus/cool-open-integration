"""The CoolAutomation Cloud Open Integration integration."""
from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from cool_open_client.hvac_units_factory import HVACUnitsFactory
from cool_open_client.cool_automation_client import (
    CoolAutomationClient,
    InvalidTokenException,
)

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
        client = await CoolAutomationClient.create(token=token)
    except OSError as error:
        raise ConfigEntryNotReady() from error
    except InvalidTokenException as error:
        _LOGGER.error("Invalid token, reauthenticating...")
        username = entry.data["username"]
        password = entry.data["password"]
        try:
            token = await CoolAutomationClient.authenticate(username, password)
            hass.config_entries.async_update_entry(
                entry, data={"username": username, "password": password, "token": token}
            )
            client = await CoolAutomationClient.create(token=token)
        except Exception as error:
            _LOGGER.error("Can't authenticate, wrong credentials: %s", error)
            raise ConfigEntryAuthFailed(
                "Authentication is no longer valid. Please reauthenticate"
            ) from error
    except Exception as error:
        _LOGGER.error("General Error: %s", error)
        raise ConfigEntryNotReady() from error
    try:
        units_factory = await HVACUnitsFactory.create(token=token)
        units = await units_factory.generate_units_from_api()
        if not units:
            raise ConfigEntryNotReady
    except OSError as error:
        raise ConfigEntryNotReady() from error
    except InvalidTokenException as error:
        _LOGGER.error("Invalid token")
        raise ConfigEntryAuthFailed(error) from error
    except Exception as error:
        _LOGGER.error("General Error: %s", error)
        raise ConfigEntryNotReady() from error

    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
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
