from __future__ import annotations

from bisect import bisect_left
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from config.custom_components.cool_open_integration.coordinator import CoolAutomationDataUpdateCoordinator
from config.custom_components.cool_open_integration.entity import CoolAutomationUnitBaseEntity

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_STATE,
    ATTR_TEMPERATURE,
    PRECISION_TENTHS,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.temperature import convert as convert_temperature

from .const import DOMAIN

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Sensibo climate entry."""

    coordinator: CoolAutomationDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]


    entities = [
        CoolAutomationUnitEntity(coordinator, unit_id)
        for unit_id, _ in coordinator.data.items()
    ]

    async_add_entities(entities)


class CoolAutomationUnitEntity(CoolAutomationUnitBaseEntity, ClimateEntity):

    def __init__(
        self, coordinator: CoolAutomationDataUpdateCoordinator, unit_id: str
    ) -> None:
        """Initiate SensiboClimate."""
        super().__init__(coordinator, unit_id)
        self._attr_unique_id = unit_id
        self._attr_temperature_unit = TEMP_CELSIUS
        self._attr_supported_features = self.get_features()
        self._attr_precision = PRECISION_TENTHS
