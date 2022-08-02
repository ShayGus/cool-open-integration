"""Config flow for CoolAutomation Cloud Open Integration integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError


from cool_open_client.cool_automation_client import CoolAutomationClient

from .const import DOMAIN, TITLE

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)

# TODO adjust the data schema to the data that you need
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("username", default=""): cv.string,
        vol.Required("password", default=""): cv.string,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    # If your PyPI package is not built with async, pass your methods
    # to the executor:
    # await hass.async_add_executor_job(
    #     your_validate_func, data["username"], data["password"]
    # )

    token = await CoolAutomationClient.authenticate(data["username"], data["password"])

    if token == "Unauthorized":
        raise InvalidAuth

    api: CoolAutomationClient = await CoolAutomationClient.create(token)
    devices = await api.get_devices()
    me = await api.get_me()
    id = me.id

    if not devices:
        raise Exception("No devices available")

    # If you cannot connect:
    # throw CannotConnect
    # If the authentication is wrong:
    # InvalidAuth

    # Return info that you want to store in the config entry.
    devices = [device.serial for device in devices]
    return {"username": data["username"], "password": data["password"], "token": token, "devices": devices, "id": id}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CoolAutomation Cloud Open Integration."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

        errors = {}

        try:
            data = await validate_input(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(data["id"])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=TITLE, data=data)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
