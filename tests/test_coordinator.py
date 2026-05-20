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
