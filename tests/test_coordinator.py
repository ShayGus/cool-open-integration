"""Tests for CoolAutomationDataUpdateCoordinator.

These tests stub the cool_open_client surface used by the coordinator so
no HTTP calls are made.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.cool_open_integration.coordinator import (
    CoolAutomationDataUpdateCoordinator,
)


def _make_unit(unit_id: str):
    """Return a stub HVACUnit with the surface the coordinator touches."""
    unit = MagicMock()
    unit.id = unit_id
    unit._update_unit = MagicMock()
    unit.reset_update = MagicMock()
    return unit


def _make_update_message(unit_id: str):
    """Return a stub UnitUpdateMessage; identity is enough for our tests."""
    return SimpleNamespace(unit_id=unit_id)


@pytest.mark.asyncio
async def test_one_bulk_call_per_cycle_regardless_of_unit_count(hass):
    units = [_make_unit(f"unit-{i}") for i in range(100)]
    client = MagicMock()
    client.get_updated_controllable_units = AsyncMock(
        return_value={u.id: _make_update_message(u.id) for u in units}
    )
    # Per-unit fetcher must not be called by the new path.
    client.get_updated_controllable_unit = AsyncMock()

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)

    data = await coordinator._async_update_data()

    assert client.get_updated_controllable_units.await_count == 1
    assert client.get_updated_controllable_unit.await_count == 0
    assert set(data.keys()) == {u.id for u in units}
    for unit in units:
        unit._update_unit.assert_called_once()


@pytest.mark.asyncio
async def test_entity_identity_preserved_across_refresh(hass):
    units = [_make_unit("unit-A"), _make_unit("unit-B")]
    original_a, original_b = units[0], units[1]
    client = MagicMock()
    client.get_updated_controllable_units = AsyncMock(
        return_value={
            "unit-A": _make_update_message("unit-A"),
            "unit-B": _make_update_message("unit-B"),
        }
    )

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)

    data = await coordinator._async_update_data()

    # Same object identity — entities hold references and must not be invalidated.
    assert data["unit-A"] is original_a
    assert data["unit-B"] is original_b
    # And _update_unit was invoked for each (mutation path preserved).
    original_a._update_unit.assert_called_once()
    original_b._update_unit.assert_called_once()


@pytest.mark.asyncio
async def test_missing_unit_in_bulk_response_keeps_last_known_state(hass):
    units = [_make_unit("unit-A"), _make_unit("unit-B")]
    client = MagicMock()
    # Only unit-A is in the bulk response; unit-B is omitted.
    client.get_updated_controllable_units = AsyncMock(
        return_value={"unit-A": _make_update_message("unit-A")}
    )

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)

    data = await coordinator._async_update_data()

    # Both units present, no exception raised.
    assert set(data.keys()) == {"unit-A", "unit-B"}
    # unit-A got an update; unit-B did NOT have _update_unit called.
    units[0]._update_unit.assert_called_once()
    units[1]._update_unit.assert_not_called()
