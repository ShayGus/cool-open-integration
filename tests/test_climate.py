"""Regression tests for CoolAutomationUnitEntity.async_set_fan_mode.

Covers GitHub issue #14 ("Fan mode change returns error"). Home Assistant
sends back the *title-cased* value exposed by the `fan_modes` property (which
`.capitalize()`s the API's uppercase modes). The fixed method must normalize
that display value back to the API's uppercase form before validating and
sending it, so `set_fan_mode` reaches the client with the raw mode.

These tests stub the cool_open_client surface the method touches so no HTTP
calls are made, and patch REFRESH_DELAY to 0 so no real sleeping occurs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.cool_open_integration.climate import CoolAutomationUnitEntity

# The API's fan modes are UPPERCASE (see climate.py). The `fan_modes` property
# exposes them title-cased via `str.capitalize()`.
RAW_FAN_MODES = ["LOW", "MEDIUM", "HIGH", "AUTO", "TOP", "VERYLOW"]

# The API's swing modes are LOWERCASE raw strings, exposed verbatim by the
# `swing_modes` property (no case conversion, unlike fan modes).
RAW_SWING_MODES = ["vertical", "30", "45", "60", "horizontal", "auto"]

# Full real API enum (from a live CoolAutomation response). Supersets the
# subsets above with the extended/hyphenated fan modes ('SHIGH', 'FU-LO',
# 'FU-HI', 'OFF') and the swing 'off'/'on' states not exercised above.
FULL_REAL_FAN_MODES = [
    "LOW", "MEDIUM", "HIGH", "AUTO", "TOP",
    "VERYLOW", "SHIGH", "FU-LO", "FU-HI", "OFF",
]
FULL_REAL_SWING_MODES = [
    "vertical", "30", "45", "60", "horizontal", "auto", "off", "on",
]
# Exact display strings the `fan_modes` property must expose to HA (the
# `.capitalize()` of each raw mode). Hardcoded so a change to the display
# transform reddens the test instead of mirroring the implementation.
FULL_REAL_FAN_DISPLAY = [
    "Low", "Medium", "High", "Auto", "Top",
    "Verylow", "Shigh", "Fu-lo", "Fu-hi", "Off",
]


@pytest.fixture(autouse=True)
def _no_refresh_delay():
    """Collapse the post-set refresh delay so success paths never sleep 3s."""
    with patch(
        "custom_components.cool_open_integration.climate.REFRESH_DELAY", 0
    ):
        yield


def _make_entity(
    fan_modes=None,
    set_fan_mode=None,
    swing_modes=None,
    set_swing_mode=None,
    swing_mode=None,
):
    """Build a CoolAutomationUnitEntity isolated to the method under test.

    Bypasses the heavy __init__ (which reads coordinator.data/client and
    builds DeviceInfo) and wires only `unit` and `coordinator`, which is all
    the fan- and swing-mode methods and properties touch.
    """
    unit = MagicMock()
    unit.fan_modes = list(RAW_FAN_MODES) if fan_modes is None else fan_modes
    unit.set_fan_mode = AsyncMock() if set_fan_mode is None else set_fan_mode
    unit.swing_modes = list(RAW_SWING_MODES) if swing_modes is None else swing_modes
    unit.swing_mode = swing_mode
    unit.set_swing_mode = AsyncMock() if set_swing_mode is None else set_swing_mode

    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()

    entity = CoolAutomationUnitEntity.__new__(CoolAutomationUnitEntity)
    entity.unit = unit
    entity.coordinator = coordinator
    return entity


async def test_title_cased_value_reaches_raw_api():
    """The exact issue #14 case: 'Medium' must reach the client as 'MEDIUM'."""
    entity = _make_entity()

    await entity.async_set_fan_mode("Medium")

    entity.unit.set_fan_mode.assert_awaited_once_with("MEDIUM")


@pytest.mark.parametrize("raw_mode", RAW_FAN_MODES)
async def test_round_trip_display_value_maps_to_raw_mode(raw_mode):
    """Every value the property exposes maps back to its raw uppercase mode.

    Explicitly includes 'Verylow' -> 'VERYLOW' via parametrization over
    RAW_FAN_MODES. The display value is taken from the real `fan_modes`
    property so the round-trip is pinned to the actual public surface HA uses.
    """
    entity = _make_entity()

    display_value = raw_mode.capitalize()
    # Guard: the property must actually expose this display value to HA.
    assert display_value in entity.fan_modes

    await entity.async_set_fan_mode(display_value)

    entity.unit.set_fan_mode.assert_awaited_once_with(raw_mode)


async def test_successful_set_requests_coordinator_refresh():
    """A successful set triggers a coordinator refresh so HA re-reads state."""
    entity = _make_entity()

    await entity.async_set_fan_mode("High")

    entity.coordinator.async_request_refresh.assert_awaited_once()


async def test_invalid_mode_raises_value_error_without_calling_client():
    """An unknown mode is rejected before any client call is made."""
    entity = _make_entity()

    with pytest.raises(ValueError):
        await entity.async_set_fan_mode("Turbo")

    entity.unit.set_fan_mode.assert_not_awaited()


@pytest.mark.parametrize("bad_value", ["", "   "])
async def test_empty_or_whitespace_raises_value_error(bad_value):
    """Empty or whitespace-only input is rejected as invalid."""
    entity = _make_entity()

    with pytest.raises(ValueError):
        await entity.async_set_fan_mode(bad_value)

    entity.unit.set_fan_mode.assert_not_awaited()


async def test_no_fan_support_raises_home_assistant_error():
    """When the current mode exposes no fan modes, surface a HA error."""
    entity = _make_entity(fan_modes=[])

    with pytest.raises(HomeAssistantError):
        await entity.async_set_fan_mode("Medium")

    entity.unit.set_fan_mode.assert_not_awaited()


async def test_client_failure_is_wrapped_in_home_assistant_error():
    """A failure from the client is re-raised as a HomeAssistantError."""
    entity = _make_entity(set_fan_mode=AsyncMock(side_effect=Exception("boom")))

    with pytest.raises(HomeAssistantError):
        await entity.async_set_fan_mode("Medium")


# ---------------------------------------------------------------------------
# Swing-mode regression tests.
#
# The API's swing modes are LOWERCASE raw strings (e.g. "vertical", "30",
# "horizontal"). Unlike fan mode, swing does NOT normalize case: the value HA
# sends (an exact entry from the `swing_modes` list) must reach the client
# verbatim. The fix also removed a `.capitalize()` on the current value so the
# active option matches a list entry and highlights in the UI.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", RAW_SWING_MODES)
async def test_swing_value_reaches_client_verbatim(mode):
    """Every swing option passes through to the client unchanged.

    Swing must NOT normalize case (unlike fan mode). HA sends the exact
    lowercase/numeric list value and it must reach set_swing_mode verbatim,
    including numeric strings ('30'/'45'/'60') and 'horizontal'/'auto'.
    """
    entity = _make_entity()

    await entity.async_set_swing_mode(mode)

    entity.unit.set_swing_mode.assert_awaited_once_with(mode)


async def test_swing_value_is_stripped_before_send():
    """Surrounding whitespace is trimmed before validation and send.

    '  vertical  ' must validate against the raw list and reach the client as
    the stripped 'vertical' (still with no case change).
    """
    entity = _make_entity()

    await entity.async_set_swing_mode("  vertical  ")

    entity.unit.set_swing_mode.assert_awaited_once_with("vertical")


@pytest.mark.parametrize("current", ["vertical", "horizontal"])
async def test_current_swing_mode_is_raw_and_matches_list(current):
    """The active swing value is returned raw and matches a list entry.

    The fix removed a .capitalize() on the current value; a capitalized
    'Vertical' would not match the lowercase 'vertical' in swing_modes, so the
    active option would fail to highlight in the UI.
    """
    entity = _make_entity(swing_mode=current)

    assert entity.swing_mode == current
    assert entity.swing_mode in entity.swing_modes


async def test_successful_swing_set_requests_coordinator_refresh():
    """A successful swing set triggers a coordinator refresh."""
    entity = _make_entity()

    await entity.async_set_swing_mode("auto")

    entity.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.parametrize("bad_mode", ["Vertical", "diagonal"])
async def test_invalid_swing_mode_raises_value_error_without_calling_client(bad_mode):
    """Swing is exact-match/case-sensitive: wrong case or unknown is rejected.

    'Vertical' (capitalized) is invalid because swing does no normalization;
    'diagonal' is simply not offered. Neither reaches the client.
    """
    entity = _make_entity()

    with pytest.raises(ValueError):
        await entity.async_set_swing_mode(bad_mode)

    entity.unit.set_swing_mode.assert_not_awaited()


@pytest.mark.parametrize("bad_value", ["", "   "])
async def test_empty_or_whitespace_swing_raises_value_error(bad_value):
    """Empty or whitespace-only swing input is rejected as invalid."""
    entity = _make_entity()

    with pytest.raises(ValueError):
        await entity.async_set_swing_mode(bad_value)

    entity.unit.set_swing_mode.assert_not_awaited()


async def test_no_swing_support_raises_home_assistant_error():
    """When the current mode exposes no swing modes, surface a HA error."""
    entity = _make_entity(swing_modes=[])

    with pytest.raises(HomeAssistantError):
        await entity.async_set_swing_mode("vertical")

    entity.unit.set_swing_mode.assert_not_awaited()


async def test_swing_client_failure_is_wrapped_in_home_assistant_error():
    """A failure from the client is re-raised as a HomeAssistantError."""
    entity = _make_entity(
        set_swing_mode=AsyncMock(side_effect=Exception("boom"))
    )

    with pytest.raises(HomeAssistantError):
        await entity.async_set_swing_mode("vertical")


# ---------------------------------------------------------------------------
# Full real-enum coverage.
#
# The subsets above pin the fix; these exercise the *complete* mode list from a
# live API response, including the extended/hyphenated fan modes ('SHIGH',
# 'FU-LO', 'FU-HI', 'OFF') and the swing 'off'/'on' states. The hyphenated fan
# modes are the sharp case: '.capitalize()' lowercases everything after the
# first letter ('FU-LO' -> 'Fu-lo'), and the set path must '.upper()' it back
# to the raw 'FU-LO' the client expects.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_mode", FULL_REAL_FAN_MODES)
async def test_full_fan_enum_display_round_trips_to_raw(raw_mode):
    """Every real fan mode's display form reaches the client as its raw mode.

    Covers the extended modes the subset test omits: 'SHIGH', 'FU-LO',
    'FU-HI' and 'OFF'.
    """
    entity = _make_entity(fan_modes=list(FULL_REAL_FAN_MODES))

    display_value = raw_mode.capitalize()
    # Guard: the property must actually expose this display value to HA.
    assert display_value in entity.fan_modes

    await entity.async_set_fan_mode(display_value)

    entity.unit.set_fan_mode.assert_awaited_once_with(raw_mode)


@pytest.mark.parametrize(
    "display_value, raw_mode",
    [("Fu-lo", "FU-LO"), ("Fu-hi", "FU-HI")],
)
async def test_hyphenated_fan_display_survives_round_trip(display_value, raw_mode):
    """The hyphen survives the capitalize/upper round-trip.

    'Fu-lo'/'Fu-hi' (the display forms) must normalize back to the raw
    'FU-LO'/'FU-HI' the client expects, hyphen intact.
    """
    entity = _make_entity(fan_modes=list(FULL_REAL_FAN_MODES))

    assert display_value in entity.fan_modes

    await entity.async_set_fan_mode(display_value)

    entity.unit.set_fan_mode.assert_awaited_once_with(raw_mode)


async def test_fan_modes_property_exposes_full_capitalized_forms():
    """The property exposes exactly the capitalized display strings for HA.

    Pinned to a hardcoded expected list (not a mirror of the implementation)
    so any change to the display transform — including the hyphenated
    'Fu-lo'/'Fu-hi' and 'Shigh'/'Verylow'/'Off' — reddens this test.
    """
    entity = _make_entity(fan_modes=list(FULL_REAL_FAN_MODES))

    assert entity.fan_modes == FULL_REAL_FAN_DISPLAY


@pytest.mark.parametrize("mode", FULL_REAL_SWING_MODES)
async def test_full_swing_enum_reaches_client_verbatim(mode):
    """Every real swing mode passes through unchanged, incl. 'off'/'on'.

    The subset test omits the 'off'/'on' states; swing does no case
    normalization, so each list value must reach the client verbatim.
    """
    entity = _make_entity(swing_modes=list(FULL_REAL_SWING_MODES))

    # Guard: the property must actually offer this value to HA.
    assert mode in entity.swing_modes

    await entity.async_set_swing_mode(mode)

    entity.unit.set_swing_mode.assert_awaited_once_with(mode)


@pytest.mark.parametrize("current", ["off", "on"])
async def test_current_swing_mode_off_on_raw_and_in_list(current):
    """The active 'off'/'on' swing value is returned raw and matches the list.

    These two states were untested; like the other modes they must be
    returned unchanged so the active option highlights in the UI.
    """
    entity = _make_entity(
        swing_modes=list(FULL_REAL_SWING_MODES), swing_mode=current
    )

    assert entity.swing_mode == current
    assert entity.swing_mode in entity.swing_modes
