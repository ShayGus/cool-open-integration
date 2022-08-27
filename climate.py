from __future__ import annotations
import logging

from typing import Any

from config.custom_components.cool_open_integration.coordinator import CoolAutomationDataUpdateCoordinator
from cool_open_client.unit import UnitCallback

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
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
    TEMP_CELSIUS,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

# from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from cool_open_client.unit import HVACUnit

from .const import DOMAIN
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

_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Sensibo climate entry."""

    coordinator: CoolAutomationDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [CoolAutomationUnitEntity(coordinator, unit_id) for unit_id, _ in coordinator.data.items()]

    async_add_entities(entities)
    _LOGGER.debug("Entities added to HA")
    coordinator.client.open_socket()


class CoolAutomationUnitEntity(CoordinatorEntity[CoolAutomationDataUpdateCoordinator], ClimateEntity, UnitCallback):

    _attr_has_entity_name = True

    def __init__(self, coordinator: CoolAutomationDataUpdateCoordinator, unit_id: str) -> None:
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
        self._attr_temperature_unit = TEMP_CELSIUS
        self._attr_supported_features = self.get_supported_features()
        self._attr_precision = self.get_precision()
        self.unit.regiter_callback(self)

    @property
    def unit_data(self) -> HVACUnit:
        return self.coordinator.data[self._device_id]

    async def async_turn_on(self) -> None:
        """Turn HVAC unit on."""
        await self.unit.turn_on()

    async def async_turn_off(self) -> None:
        """Turn HVAC unit on."""
        await self.unit.turn_off()

    def get_precision(self):
        return PRECISION_HALVES if self.unit.is_half_degree else PRECISION_WHOLE

    def get_supported_features(self):
        supported = 0
        supported |= ClimateEntityFeature.TARGET_TEMPERATURE
        supported |= ClimateEntityFeature.FAN_MODE if self.unit.is_fan_mode else 0
        supported |= ClimateEntityFeature.SWING_MODE if self.unit.is_swing_mode else 0
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
        hvac_modes = [OPEN_CLIENT_TO_HA_MODES[mode] for mode in self.unit.operation_modes]
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
        # return OPEN_CLIENT_TO_HA_FAN_MODES[self.unit.fan_mode] if self.unit.is_on else None
        return self.unit.fan_mode.capitalize()

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the swing setting."""
        # fan_modes = [OPEN_CLIENT_TO_HA_FAN_MODES[mode] for mode in self.unit.fan_modes]
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
        return [mode.capitalize() for mode in swing_modes] if swing_modes else None

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
        await self.unit.set_temperature_set_point(new_temp)
        self.async_write_ha_state()
        self.coordinator.async_refresh()

    def _get_valid_temperature(self, target: float) -> float:
        if target <= self.min_temp:
            return self.min_temp
        if target >= self.max_temp:
            return self.max_temp
        return target

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        if not self.unit.fan_modes:
            raise HomeAssistantError("Current mode doesn't support setting Fanlevel")

        await self.unit.set_fan_mode(fan_mode.upper())
        self.async_write_ha_state()
        self.coordinator.async_refresh()

    async def async_set_swing_mode(self, swing_mode) -> None:
        """Set new target swing operation."""
        if not self.unit.swing_modes:
            raise HomeAssistantError("Current mode doesn't support setting Fanlevel")

        await self.unit.set_swing_mode(swing_mode.upper())
        self.async_write_ha_state()
        self.coordinator.async_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target operation mode."""
        if hvac_mode == HVACMode.OFF:
            await self.unit.turn_off()
            self.async_write_ha_state()
            self.coordinator.async_refresh()
            return
        elif self.hvac_mode == HVACMode.OFF:
            await self.unit.turn_on()

        _LOGGER.debug(str(OPEN_CLIENT_TO_HA_MODES))
        mode = [k for k, v in OPEN_CLIENT_TO_HA_MODES.items() if v == hvac_mode]
        _LOGGER.debug("Changing mode to %s" % mode)
        if not mode:
            raise ValueError("Unsupported mode was provided")

        await self.unit.set_opration_mode(mode[0])
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    def unit_update_callback(self) -> None:
        _LOGGER.warning("Unit update callback")
        _LOGGER.warning(str(self.unit))
        self.hass.create_task(self.async_assume_state())

    async def async_assume_state(self) -> None:
        try:
            # self.coordinator.async_refresh()
            # self.async_schedule_update_ha_state()
            self.async_write_ha_state()
        except Exception as error:
            _LOGGER.error(f"Failed to set state for unit {self.name}: {error}")
