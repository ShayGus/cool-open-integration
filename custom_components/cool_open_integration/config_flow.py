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
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaFlowFormStep,
    SchemaOptionsFlowHandler,
)
from collections.abc import Mapping

from cool_open_client.cool_automation_client import CoolAutomationClient

from .const import DOMAIN, TITLE

_LOGGER = logging.getLogger(__package__)

# TODO adjust the data schema to the data that you need
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("username", default=""): cv.string,
        vol.Required("password", default=""): cv.string,
    }
)

OPTIONS_FLOW = {
    "init": SchemaFlowFormStep(STEP_USER_DATA_SCHEMA),
}


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

    api: CoolAutomationClient = await CoolAutomationClient.create(token, logger=_LOGGER)
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
    return {
        "username": data["username"],
        "password": data["password"],
        "token": token,
        "devices": devices,
        "id": id,
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CoolAutomation Cloud Open Integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self.entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

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

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_user_reauth(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user_reauth", data_schema=STEP_USER_DATA_SCHEMA
            )

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
            self.hass.config_entries.async_update_entry(self.entry, data=data)
            await self.hass.config_entries.async_reload(self.entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="user_reauth", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, _: Mapping[str, Any]) -> FlowResult:
        """Handle configuration by re-auth."""
        self.entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        # return await self.async_step_reauth_perform()
        return await self.async_step_user_reauth()

    async def async_step_reauth_perform(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Confirm reauth dialog."""
        # if user_input is not None:
        return await self.async_step_user_reauth()

        # return self.async_show_form(
        #     step_id="reauth_perform", data_schema=STEP_USER_DATA_SCHEMA
        # )

    # @staticmethod
    # @callback
    # def async_get_options_flow(
    #     config_entry: config_entries.ConfigEntry,
    # ) -> CoolOpenIntegrationOptionsFlowHandler:
    #     """Get the options flow for this handler."""
    #     return CoolOpenIntegrationOptionsFlowHandler(config_entry)


# class CoolOpenIntegrationOptionsFlowHandler(config_entries.OptionsFlow):
#     def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
#         """Initialize CoolOpenIntegration options flow."""
#         self.arm_options = config_entry.options.get(OPTIONS_ARM, DEFAULT_ARM_OPTIONS)
#         self.zone_options = config_entry.options.get(
#             OPTIONS_ZONES, DEFAULT_ZONE_OPTIONS
#         )
#         self.selected_zone = None


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
