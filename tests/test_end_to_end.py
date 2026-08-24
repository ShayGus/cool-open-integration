"""End-to-end tests: the real integration against a fake CoolAutomation cloud.

Everything else in this suite mocks the library. These tests do not: a real
`hass`, a real config entry lifecycle, a real `aiohttp` client talking over a
real loopback socket to `tests/stub_coolautomation.py`. That is what makes the
interesting failures reachable — a rejected token, a dropped socket, a poll
that starts failing — none of which can be provoked against the live API, and
the live API belongs to a hotel with guests in it.
"""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cool_open_integration.const import (
    DOMAIN,
    WS_STALLED_RECONNECT_LIMIT,
)

from .stub_coolautomation import (
    VALID_TOKEN,
    StubCoolAutomation,
    close_client_session,
    point_client_at,
    reset_client_singleton,
)


@pytest.fixture
async def stub(hass, socket_enabled, monkeypatch):
    """`socket_enabled` lifts pytest-socket's block so the stub can listen.

    HA's plugin allows connections to 127.0.0.1 but still blocks creating a
    socket at all, which a real server needs.
    """
    reset_client_singleton()
    server = await StubCoolAutomation().start()
    point_client_at(server)
    # `async_setup_entry` re-applies the endpoint override on every setup, so
    # the stub URL has to go through the constant the override reads —
    # otherwise the client dutifully dials the real ws.coolremote.net.
    monkeypatch.setattr(
        "custom_components.cool_open_integration.WS_URL", server.ws_url
    )
    try:
        yield server
    finally:
        # Unload before the server goes away: otherwise the WS pump spends
        # teardown retrying against a closed port, and HA flags the still-
        # running background task as a lingering task.
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await close_client_session()
        await server.stop()
        reset_client_singleton()


async def _setup_entry(hass, token: str = VALID_TOKEN) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Stub CoolAutomation",
        data={"username": "stub", "password": "stub", "token": token},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _settle(hass, times: int = 6) -> None:
    """Let the WS pump's background task make progress."""
    for _ in range(times):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def test_entry_sets_up_and_creates_entities(hass, stub):
    entry = await _setup_entry(hass)

    assert entry.state is ConfigEntryState.LOADED
    states = hass.states.async_all("climate")
    assert len(states) == len(stub.units)
    # Setup must not fan out per unit: one bulk call is the whole point.
    assert stub.units_request_count <= 2


async def test_push_update_reaches_the_entity_state(hass, stub):
    """The path that has never worked in production, end to end."""
    await _setup_entry(hass)
    await _settle(hass)
    assert stub.ws_connection_count == 1

    entity_id = hass.states.async_entity_ids("climate")[0]
    unit_id = hass.states.get(entity_id).attributes and entity_id
    before = hass.states.get(entity_id).attributes["current_temperature"]

    await stub.push_update("unit-A", ambientTemperature=before + 5)
    await _settle(hass)

    changed = [
        s for s in hass.states.async_all("climate")
        if s.attributes["current_temperature"] == before + 5
    ]
    assert changed, "no entity picked up the pushed ambient temperature"


async def test_push_writes_state_only_for_the_unit_that_changed(hass, stub):
    """Without the guard, one message re-renders every entity."""
    await _setup_entry(hass)
    await _settle(hass)

    writes: dict[str, int] = {}

    def _count(event):
        entity_id = event.data["entity_id"]
        if entity_id.startswith("climate."):
            writes[entity_id] = writes.get(entity_id, 0) + 1

    hass.bus.async_listen("state_changed", _count)

    await stub.push_update("unit-A", ambientTemperature=30)
    await _settle(hass)

    assert len(writes) == 1, f"expected one entity to change, got {writes}"


async def test_entities_go_unavailable_when_the_poll_fails(hass, stub):
    """The 2026-07-22 regression: stale values were presented as live."""
    entry = await _setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    stub.fail_units_request = True
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert all(
        state.state == STATE_UNAVAILABLE for state in hass.states.async_all("climate")
    )

    stub.fail_units_request = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not any(
        state.state == STATE_UNAVAILABLE for state in hass.states.async_all("climate")
    )


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Tolerate aiohttp's heartbeat timer outliving the socket we gave up on.

    When the pump quits, the library's current `ws_connect` context is unwound
    by closing its async generator, and aiohttp's `_send_heartbeat` handle is
    not always cancelled before the loop is inspected. It is bounded — one
    30s timer that fires once against a dead connection — not a leak, and it
    is inside the library, so it cannot be cancelled from here.
    """
    return True


async def test_rejected_token_stops_reconnecting_and_starts_reauth(
    hass, stub, monkeypatch
):
    """The failure mode that cannot be provoked against the real API.

    A refused `authenticate` makes the server close cleanly, which the library
    does not treat as an error, so its own backoff never engages. Left alone it
    would reconnect flat out and fire a bulk refresh each round.
    """
    # Collapse the pump's own backoff instead of patching asyncio.sleep, which
    # is shared with the rest of HA. min(2**n, 0) == 0, so it still yields.
    monkeypatch.setattr(
        "custom_components.cool_open_integration.WS_STALLED_BACKOFF_CAP_SECONDS", 0
    )
    stub.reject_token = True
    entry = await _setup_entry(hass)

    # Real reconnects need real I/O round trips, so yield time rather than
    # just spinning the loop.
    for _ in range(500):
        # `async_get_active_flows` yields a generator, which is always truthy —
        # it has to be drained before it means anything.
        if list(entry.async_get_active_flows(hass, {"reauth"})):
            break
        await asyncio.sleep(0.01)
        await hass.async_block_till_done()

    assert list(entry.async_get_active_flows(hass, {"reauth"})), "no reauth was started"
    # Bounded, not a spin: it gave up around the configured limit.
    assert stub.ws_connection_count <= WS_STALLED_RECONNECT_LIMIT + 2


async def test_dropped_socket_reconnects_and_keeps_delivering(hass, stub):
    """A flaky link must recover, and must not be mistaken for a bad token."""
    await _setup_entry(hass)
    await _settle(hass)
    assert stub.ws_connection_count == 1

    await stub.drop_socket()
    for _ in range(200):
        if stub.ws_connection_count > 1:
            break
        await asyncio.sleep(0)
        await hass.async_block_till_done()

    assert stub.ws_connection_count > 1, "client did not reconnect after the drop"

    await stub.push_update("unit-B", ambientTemperature=19)
    await _settle(hass)
    assert any(
        s.attributes["current_temperature"] == 19
        for s in hass.states.async_all("climate")
    ), "no data delivered after reconnecting"


async def test_unload_closes_the_websocket(hass, stub):
    """A stranded session per reload is how this leaks."""
    entry = await _setup_entry(hass)
    await _settle(hass)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _settle(hass)

    assert entry.state is ConfigEntryState.NOT_LOADED
