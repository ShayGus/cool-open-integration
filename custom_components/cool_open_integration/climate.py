from __future__ import annotations

import asyncio
import logging
from typing import Any

from cool_open_client.unit import HVACUnit

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_MIDDLE,
    FAN_TOP,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_HALVES,
    PRECISION_WHOLE,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, REFRESH_DELAY
from .coordinator import CoolAutomationDataUpdateCoordinator

# Fan Modes ['LOW', 'MEDIUM', 'HIGH', 'AUTO', 'TOP', 'VERYLOW']
# Operation Modes ['COOL', 'HEAT', 'DRY', 'FAN', 'AUTO']
# Operation Statuses ['on', 'off']
# Swing Modes ['vertical', '30', '45', '60', 'horizontal', 'auto']

OPEN_CLIENT_TO_HA_MODES = {
    "COOL": HVACMode.COOL,
    "HEAT": HVACMode.HEAT,
    "DRY": HVACMode.DRY,
    "FAN": HVACMode.FAN_ONLY,
    "AUTO": HVACMode.HEAT_COOL,
}

OPEN_CLIENT_TO_HA_FAN_MODES = {
    "VERYLOW": FAN_LOW,
    "LOW": FAN_MEDIUM,
    "MEDIUM": FAN_MIDDLE,
    "HIGH": FAN_HIGH,
    "TOP": FAN_TOP,
    "AUTO": FAN_AUTO,
}

CELSIUS = UnitOfTemperature.CELSIUS

_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the climate entry."""

    coordinator: CoolAutomationDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        CoolAutomationUnitEntity(coordinator, unit_id)
        for unit_id, _ in coordinator.data.items()
    ]

    async_add_entities(entities)
    _LOGGER.debug("Entities added to HA")


class CoolAutomationUnitEntity(
    CoordinatorEntity[CoolAutomationDataUpdateCoordinator], ClimateEntity
):
    """HVAC Entity of CoolAutomation controllable HVAC unit."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: CoolAutomationDataUpdateCoordinator, unit_id: str
    ) -> None:
        """Initiate SensiboClimate."""
        super().__init__(coordinator)
        self._device_id = unit_id

        self.__attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.unit_data.id)},
            name=self.unit_data.name,
            manufacturer="CoolAutomations",
            suggested_area=self.unit_data.name,
        )

        self._client = coordinator.client
        self.unit: HVACUnit = coordinator.data[unit_id]
        self._attr_unique_id = self.unit.name
        self._attr_temperature_unit = CELSIUS
        self._attr_supported_features = self.get_supported_features()
        self._attr_precision = self.get_precision()

    @property
    def unit_data(self) -> HVACUnit:
        """Data of the controllable unit.

        Returns:
            HVACUnit: The controllable unit to control

        """
        return self.coordinator.data[self._device_id]

    async def async_turn_on(self) -> None:
        """Turn HVAC unit on."""
        await self.unit.turn_on()
        await asyncio.sleep(REFRESH_DELAY)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Turn HVAC unit off."""
        await self.unit.turn_off()
        await asyncio.sleep(REFRESH_DELAY)
        await self.coordinator.async_request_refresh()

    def get_precision(self) -> float:
        """Get Temperature.

        Returns:
            float: precision of temperature.

        """
        return PRECISION_HALVES if self.unit.is_half_degree else PRECISION_WHOLE

    def get_supported_features(self) -> int:
        """Get supported features.

        Returns:
            int: int mask of supported features

        """
        supported = 0
        supported |= ClimateEntityFeature.TARGET_TEMPERATURE
        supported |= ClimateEntityFeature.FAN_MODE if self.unit.is_fan_mode else 0
        supported |= ClimateEntityFeature.SWING_MODE if self.unit.is_swing_mode else 0
        supported |= ClimateEntityFeature.TURN_ON
        supported |= ClimateEntityFeature.TURN_OFF
        return supported

    @property
    def name(self) -> str:
        return self.unit.name

    @property
    def hvac_mode(self) -> HVACMode:
        """Return hvac operation."""
        if self.unit.is_on:
            return OPEN_CLIENT_TO_HA_MODES[self.unit.operation_mode]
        return HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available hvac operation modes."""
        hvac_modes = [
            OPEN_CLIENT_TO_HA_MODES[mode] for mode in self.unit.operation_modes
        ]
        hvac_modes.append(HVACMode.OFF)
        return hvac_modes if hvac_modes else [HVACMode.OFF]

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self.unit.ambient_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self.unit.setpoint if self.unit.is_on else None

    @property
    def target_temperature_step(self) -> float | None:
        """Return the supported step of target temperature."""
        return 0.5 if self.unit.is_half_degree else 1

    @property
    def fan_mode(self) -> str | None:
        """Return the fan setting."""
        return self.unit.fan_mode.capitalize()

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the swing setting."""
        fan_modes = self.unit.fan_modes
        return [mode.capitalize() for mode in fan_modes] if fan_modes else None

    @property
    def swing_mode(self) -> str | None:
        """Return the fan setting."""
        return self.unit.swing_mode.capitalize()

    @property
    def swing_modes(self) -> list[str] | None:
        """Return the list of available swing modes."""
        swing_modes = self.unit.swing_modes
        return [mode for mode in swing_modes] if swing_modes else None

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        min_temp = self.unit.min_temp
        return min_temp

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        max_temp = self.unit.max_temp
        return max_temp

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return True

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""

        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            raise ValueError("No target temperature provided")

        if temperature == self.target_temperature:
            return

        new_temp = self._get_valid_temperature(temperature)
        try:
            # API expects integer temperature as positional argument
            await self.unit.set_temperature_set_point(int(new_temp))
        except Exception as error:
            _LOGGER.error("Failed to set temperature: %s", error)
            # Check if this is the known API validation error
            if "validation errors for UnitControlApi" in str(
                error
            ) and "unit_control_setpoints_body" in str(error):
                raise HomeAssistantError(
                    "API compatibility issue: The cool-open-client library needs to be updated. "
                    "Please check for a newer version or report this issue."
                ) from error
            raise HomeAssistantError(f"Temperature setting failed: {error}") from error
        await asyncio.sleep(REFRESH_DELAY)
        await self.coordinator.async_request_refresh()

    def _get_valid_temperature(self, target: float) -> float:
        if target <= self.min_temp:
            return self.min_temp
        if target >= self.max_temp:
            return self.max_temp
        return target

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode.

        fan_mode: str - fan mode to set

        """
        if not self.unit.fan_modes:
            raise HomeAssistantError("Current mode doesn't support setting Fanlevel")

        # Validate fan mode
        if not fan_mode or not fan_mode.strip():
            raise ValueError("Fan mode cannot be empty")

        # Use exact mode strings from API - no case conversion
        available_modes = list(self.unit.fan_modes)

        if fan_mode not in available_modes:
            raise ValueError(
                f"Fan mode {fan_mode} is not valid. Valid fan modes are: {', '.join(available_modes)}"
            )

        try:
            # Pass the exact mode string as provided by the API
            await self.unit.set_fan_mode(fan_mode)
        except Exception as error:
            _LOGGER.error("Failed to set fan mode: %s", error)
            raise HomeAssistantError(f"Fan mode setting failed: {error}") from error
        await asyncio.sleep(REFRESH_DELAY)
        await self.coordinator.async_request_refresh()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set new target swing operation.
        swing_mode: str - swing mode to set
        """
        if not self.unit.swing_modes:
            raise HomeAssistantError("Current mode doesn't support setting Fanlevel")

        await self.unit.set_swing_mode(swing_mode)
        await asyncio.sleep(REFRESH_DELAY)
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target operation mode."""
        turn_on = False
        if hvac_mode == HVACMode.OFF:
            await self.unit.turn_off()
            await asyncio.sleep(REFRESH_DELAY)
            await self.coordinator.async_request_refresh()
            return
        if self.hvac_mode == HVACMode.OFF:
            turn_on = True

        _LOGGER.debug(str(OPEN_CLIENT_TO_HA_MODES))
        mode = [k for k, v in OPEN_CLIENT_TO_HA_MODES.items() if v == hvac_mode]
        _LOGGER.debug("Changing mode to %s", mode)
        if not mode:
            raise ValueError("Unsupported mode was provided")

        try:
            # API has typo in method name: set_opration_mode (missing 'e')
            await self.unit.set_opration_mode(mode[0])
        except Exception as error:
            _LOGGER.error("Failed to set operation mode: %s", error)
            raise HomeAssistantError(
                f"Operation mode setting failed: {error}"
            ) from error
        if turn_on:
            await self.unit.turn_on()
        await asyncio.sleep(REFRESH_DELAY)
        await self.coordinator.async_request_refresh()
