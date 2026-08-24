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


from types import SimpleNamespace

from custom_components.cool_open_integration import _ws_pump


def _make_unit_update_event(unit_id: str):
    """Stand-in for cool_open_client.ws_events.UnitUpdate; identity tested by isinstance against the real class."""
    from cool_open_client.ws_events import UnitUpdate
    msg = SimpleNamespace(unit_id=unit_id)
    return UnitUpdate(msg)


async def _aiter_from(events):
    for ev in events:
        yield ev


@pytest.mark.asyncio
async def test_ws_pump_routes_unit_update_to_in_memory_unit(hass):
    units = [_make_unit("unit-A"), _make_unit("unit-B")]
    client = MagicMock()
    client.subscribe_unit_updates = MagicMock(
        return_value=_aiter_from([_make_unit_update_event("unit-A")])
    )

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
    coordinator.data = {u.id: u for u in units}
    coordinator.async_update_listeners = MagicMock()

    await _ws_pump(hass, entry, coordinator)

    # unit-A got the WS update; unit-B was untouched.
    units[0]._update_unit.assert_called_once()
    units[1]._update_unit.assert_not_called()
    # Coordinator was notified, and named the unit the push was about.
    coordinator.async_update_listeners.assert_called_once()
    assert coordinator.last_pushed_unit_id == "unit-A"


@pytest.mark.asyncio
async def test_ws_pump_reconnected_triggers_refresh(hass):
    from cool_open_client.ws_events import Reconnected

    units = [_make_unit("unit-A")]
    client = MagicMock()
    client.subscribe_unit_updates = MagicMock(return_value=_aiter_from([Reconnected()]))

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
    coordinator.data = {u.id: u for u in units}
    coordinator.async_request_refresh = AsyncMock()

    await _ws_pump(hass, entry, coordinator)

    coordinator.async_request_refresh.assert_awaited_once()
    # No UnitUpdate, so no _update_unit call.
    units[0]._update_unit.assert_not_called()


@pytest.mark.asyncio
async def test_ws_pump_unknown_unit_id_ignored(hass):
    units = [_make_unit("unit-A")]
    client = MagicMock()
    client.subscribe_unit_updates = MagicMock(
        return_value=_aiter_from([_make_unit_update_event("unit-NEW")])
    )

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
    coordinator.data = {u.id: u for u in units}
    coordinator.async_update_listeners = MagicMock()

    # Must not raise.
    await _ws_pump(hass, entry, coordinator)

    units[0]._update_unit.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_poll_clears_pushed_unit_id(hass):
    """A bulk poll concerns every unit, so no single unit may stay flagged."""
    units = [_make_unit("unit-A"), _make_unit("unit-B")]
    client = MagicMock()
    client.get_updated_controllable_units = AsyncMock(
        return_value={u.id: _make_update_message(u.id) for u in units}
    )

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
    coordinator.last_pushed_unit_id = "unit-A"

    await coordinator._async_update_data()

    assert coordinator.last_pushed_unit_id is None


@pytest.mark.asyncio
async def test_ws_pump_closes_the_event_stream_on_exit(hass):
    """The library closes its aiohttp session in the generator's `finally`.

    That only runs if someone closes the generator, so the pump must not
    leave it to the event loop's asyncgen finalizer.
    """
    closed = []

    async def _stream():
        try:
            yield _make_unit_update_event("unit-A")
            # Suspend here so the pump has to close us rather than us ending.
            yield _make_unit_update_event("unit-A")
        finally:
            closed.append(True)

    units = [_make_unit("unit-A")]
    client = MagicMock()
    client.subscribe_unit_updates = MagicMock(return_value=_stream())

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
    coordinator.data = {u.id: u for u in units}
    coordinator.async_update_listeners = MagicMock()

    await _ws_pump(hass, entry, coordinator)

    assert closed == [True]


@pytest.mark.asyncio
async def test_dataless_reconnect_storm_stops_and_asks_for_reauth(hass, monkeypatch):
    """A server that hangs up right after the handshake must not spin forever."""
    from custom_components.cool_open_integration import const
    from cool_open_client.ws_events import Reconnected

    slept: list[float] = []

    async def _fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(
        "custom_components.cool_open_integration.asyncio.sleep", _fake_sleep
    )

    # Far more reconnects than the limit; the pump must bail at the limit.
    events = [Reconnected() for _ in range(const.WS_STALLED_RECONNECT_LIMIT + 25)]
    units = [_make_unit("unit-A")]
    client = MagicMock()
    client.subscribe_unit_updates = MagicMock(return_value=_aiter_from(events))

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
    coordinator.data = {u.id: u for u in units}
    coordinator.async_request_refresh = AsyncMock()

    await _ws_pump(hass, entry, coordinator)

    # Gave up at the limit instead of draining all the events.
    assert (
        coordinator.async_request_refresh.await_count
        == const.WS_STALLED_RECONNECT_LIMIT - 1
    )
    entry.async_start_reauth.assert_called_once_with(hass)
    # And it backed off in between rather than reconnecting flat out.
    assert slept == [
        min(2.0 ** (n - 1), const.WS_STALLED_BACKOFF_CAP_SECONDS)
        for n in range(2, const.WS_STALLED_RECONNECT_LIMIT)
    ]


@pytest.mark.asyncio
async def test_a_real_unit_update_resets_the_stall_counter(hass, monkeypatch):
    """Reconnects that do deliver data are healthy and must not trip the guard."""
    from custom_components.cool_open_integration import const
    from cool_open_client.ws_events import Reconnected

    async def _fake_sleep(delay):
        return None

    monkeypatch.setattr(
        "custom_components.cool_open_integration.asyncio.sleep", _fake_sleep
    )

    # Two reconnects, real data, then two more — never reaches the limit.
    events = []
    for _ in range(3):
        events += [Reconnected(), Reconnected(), _make_unit_update_event("unit-A")]

    units = [_make_unit("unit-A")]
    client = MagicMock()
    client.subscribe_unit_updates = MagicMock(return_value=_aiter_from(events))

    entry = MagicMock()
    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
    coordinator.data = {u.id: u for u in units}
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_update_listeners = MagicMock()

    await _ws_pump(hass, entry, coordinator)

    entry.async_start_reauth.assert_not_called()
    assert coordinator.async_update_listeners.call_count == 3
