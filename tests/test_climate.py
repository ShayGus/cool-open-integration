"""Tests for the climate entity fan mode handling."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cool_open_integration.climate import CoolAutomationUnitEntity

API_FAN_MODES = ["LOW", "MEDIUM", "HIGH", "AUTO", "TOP"]


def _make_entity() -> MagicMock:
    entity = MagicMock(spec=CoolAutomationUnitEntity)
    entity.unit = MagicMock()
    entity.unit.fan_modes = list(API_FAN_MODES)
    entity.unit.set_fan_mode = AsyncMock()
    entity.coordinator = MagicMock()
    entity.coordinator.async_request_refresh = AsyncMock()
    return entity


async def _set_fan_mode(entity: MagicMock, fan_mode: str) -> None:
    with patch(
        "custom_components.cool_open_integration.climate.asyncio.sleep",
        new=AsyncMock(),
    ):
        await CoolAutomationUnitEntity.async_set_fan_mode(entity, fan_mode)


def test_fan_modes_property_capitalizes() -> None:
    """The fan_modes property serves capitalized names to HA."""
    entity = _make_entity()
    assert CoolAutomationUnitEntity.fan_modes.fget(entity) == [
        "Low",
        "Medium",
        "High",
        "Auto",
        "Top",
    ]


async def test_set_fan_mode_accepts_capitalized_mode() -> None:
    """A mode as served by the fan_modes property round-trips to the API string."""
    entity = _make_entity()
    await _set_fan_mode(entity, "Low")
    entity.unit.set_fan_mode.assert_awaited_once_with("LOW")


async def test_set_fan_mode_accepts_api_cased_mode() -> None:
    """The raw API casing keeps working too."""
    entity = _make_entity()
    await _set_fan_mode(entity, "MEDIUM")
    entity.unit.set_fan_mode.assert_awaited_once_with("MEDIUM")


async def test_set_fan_mode_rejects_unknown_mode() -> None:
    """An unknown mode raises and lists the HA-facing mode names."""
    entity = _make_entity()
    with pytest.raises(ValueError, match="Low, Medium, High, Auto, Top"):
        await _set_fan_mode(entity, "Turbo")
    entity.unit.set_fan_mode.assert_not_awaited()
