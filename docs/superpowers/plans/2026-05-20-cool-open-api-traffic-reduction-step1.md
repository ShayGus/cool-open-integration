# Cool Open API Traffic Reduction — Step 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-unit polling in `cool-open-integration` with one bulk
HTTP call per cycle, cutting API traffic by ~99% for the hotel customer
flagged by CoolAutomation.

**Architecture:** Add a symmetrical bulk method
`CoolAutomationClient.get_updated_controllable_units()` to the
`cool-open-client` library (returns `dict[unit_id, UnitUpdateMessage]`), then
rewrite the integration's `DataUpdateCoordinator._async_update_data` to call
it once per cycle and mutate existing `HVACUnit` instances in place. Polling
cadence (30s), entity identity, and `iot_class: cloud_polling` are preserved.
WebSocket adoption is deferred to Step 2.

**Tech Stack:** Python 3.12+, aiohttp, `cool-open-client` (PyPI),
`homeassistant`, `pytest-homeassistant-custom-component`,
`unittest.IsolatedAsyncioTestCase`.

**Spec:** `docs/superpowers/specs/2026-05-20-cool-open-api-traffic-reduction-design.md`

---

## Repository layout

Two repos, must ship together:

- **Library** — `CoolControlOpenClient/` (PyPI package `cool-open-client`)
- **Integration** — `cool-open-integration/` (HA custom component)

The library changes ship first (`0.0.18 → 0.0.19`), then the integration pins
to the new version (`0.0.14 → 0.0.15`).

## File structure

**Library (`CoolControlOpenClient/`):**

| File | Change | Purpose |
|---|---|---|
| `cool_open_client/utils/units_payload.py` | Create | Module-level `extract_units_mapping`, `ensure_dict` helpers lifted from `HVACUnitsFactory` so the new client method and the factory share parsing |
| `cool_open_client/hvac_units_factory.py` | Modify | Replace the two private static methods with imports from `utils/units_payload` |
| `cool_open_client/cool_automation_client.py` | Modify | Add `get_updated_controllable_units` method |
| `tests/test_get_updated_controllable_units.py` | Create | Mocked async test (no live API) for the new method |
| `setup.py` | Modify | Version `0.0.18 → 0.0.19` |

**Integration (`cool-open-integration/`):**

| File | Change | Purpose |
|---|---|---|
| `tests/__init__.py` | Create | Make tests a package |
| `tests/conftest.py` | Create | `pytest-homeassistant-custom-component` fixtures + `enable_custom_integrations` autouse |
| `tests/test_coordinator.py` | Create | Three coordinator tests: bulk fan-out, entity identity, missing-unit tolerance |
| `requirements_test.txt` | Create | Pin `pytest-homeassistant-custom-component` |
| `custom_components/cool_open_integration/coordinator.py` | Modify | Rewrite `_async_update_data` to use the bulk method |
| `custom_components/cool_open_integration/manifest.json` | Modify | Pin `cool-open-client==0.0.19`, bump `version` to `0.0.15` |

---

## Task ordering rationale

TDD-ordered with library work first since the integration imports from it.
Refactor before adding new code (Task 1) so the new method can use shared
helpers without duplicating logic. Each task ends with a commit on the
appropriate repo's main branch (or a feature branch — author's choice).

---

## Task 1: Lift shared payload helpers to a module

**Files:**
- Create: `CoolControlOpenClient/cool_open_client/utils/units_payload.py`
- Modify: `CoolControlOpenClient/cool_open_client/hvac_units_factory.py`

This is a pure refactor — no behavior change. We move two private helpers
out of `HVACUnitsFactory` so the new client method (Task 2) can reuse them
without duplicating the defensive payload-parsing logic.

- [ ] **Step 1: Create the helpers module**

Create `CoolControlOpenClient/cool_open_client/utils/units_payload.py`:

```python
"""Shared helpers for parsing the `UnitsResponse` payload shape.

Lifted from `HVACUnitsFactory` so both the factory (setup-time) and the
client's bulk update method (poll-time) parse the API response with the
same defensive logic.
"""
from __future__ import annotations

from typing import Any, Dict


def extract_units_mapping(payload: Any) -> Dict[str, Any]:
    """Return a `{unit_id: payload}` mapping from a `UnitsResponse.data` value."""
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    additional = getattr(payload, "additional_properties", None)
    if isinstance(additional, dict):
        return additional
    if hasattr(payload, "to_dict"):
        dumped = payload.to_dict()
        if isinstance(dumped, dict):
            return dumped
    return {}


def ensure_dict(payload: Any) -> Dict[str, Any]:
    """Coerce a single-unit payload into a plain dict."""
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "model_dump"):
        dumped = payload.model_dump(by_alias=True, exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    if hasattr(payload, "to_dict"):
        dumped = payload.to_dict()
        if isinstance(dumped, dict):
            return dumped
    return {}
```

- [ ] **Step 2: Refactor `HVACUnitsFactory` to import the helpers**

In `CoolControlOpenClient/cool_open_client/hvac_units_factory.py`:

1. Add at the top of the file (after existing imports):

```python
from .utils.units_payload import ensure_dict, extract_units_mapping
```

2. In `generate_units_from_api`, replace the call site
   `units_payload = self._extract_mapping(units.data)` with
   `units_payload = extract_units_mapping(units.data)`.

3. In `generate_units_from_api`, replace `raw_unit = self._ensure_dict(payload)` with
   `raw_unit = ensure_dict(payload)`.

4. **Delete** the two static methods `_extract_mapping` and `_ensure_dict` from
   `HVACUnitsFactory`. (Search for `@staticmethod` blocks defining those names
   and remove their bodies entirely. They are no longer referenced.)

- [ ] **Step 3: Run the existing factory tests to verify no regression**

Run from `CoolControlOpenClient/`:

```bash
python -m unittest tests.test_hvac_units_factory -v
```

Expected: same pass/skip status as before the refactor (any tests that
required a `token.txt` fixture will still skip cleanly).

- [ ] **Step 4: Commit**

```bash
cd CoolControlOpenClient
git add cool_open_client/utils/units_payload.py cool_open_client/hvac_units_factory.py
git commit -m "refactor: lift units payload helpers to a shared module"
```

---

## Task 2: Add `get_updated_controllable_units` to the client

**Files:**
- Modify: `CoolControlOpenClient/cool_open_client/cool_automation_client.py`
- Create: `CoolControlOpenClient/tests/test_get_updated_controllable_units.py`

- [ ] **Step 1: Write the failing test**

Create `CoolControlOpenClient/tests/test_get_updated_controllable_units.py`:

```python
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from cool_open_client.cool_automation_client import (
        CoolAutomationClient,
        UnitUpdateMessage,
    )
    from cool_open_client.utils.singleton import SingletonMeta
except ModuleNotFoundError as exc:
    if exc.name == "websocket":
        raise unittest.SkipTest("websocket-client dependency missing")
    raise


class GetUpdatedControllableUnitsTest(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the new bulk-update client method. No live API."""

    async def asyncSetUp(self):
        # Bypass CoolAutomationClient.create() so we don't need a real token.
        SingletonMeta._instances.pop(CoolAutomationClient, None)
        self.client = CoolAutomationClient.__new__(CoolAutomationClient)
        self.client.token = "test-token"
        self.client.api_client = MagicMock()
        self.client.api_client.close = AsyncMock()
        # Minimal dictionary stubs (`_transform_message` reads these).
        self.client.fan_modes = MagicMock(get=lambda v: v)
        self.client.operation_modes = MagicMock(get=lambda v: v)
        self.client.operation_statuses = MagicMock(get=lambda v: v)
        self.client.swing_modes = MagicMock(get=lambda v: v)

    async def asyncTearDown(self):
        await self.client.api_client.close()
        SingletonMeta._instances.pop(CoolAutomationClient, None)

    async def test_returns_message_per_unit(self):
        fake_response = MagicMock()
        fake_response.data = {
            "unit-A": {
                "id": "unit-A",
                "name": "Lobby",
                "type": 1,
                "active_fan_mode": 1,
                "active_operation_mode": 2,
                "active_operation_status": 1,
                "active_setpoint": 22.0,
                "active_swing_mode": 0,
                "ambient_temperature": 23,
                "filter": False,
            },
            "unit-B": {
                "id": "unit-B",
                "name": "Suite 101",
                "type": 1,
                "active_fan_mode": 2,
                "active_operation_mode": 1,
                "active_operation_status": 1,
                "active_setpoint": 20.0,
                "active_swing_mode": 1,
                "ambient_temperature": 21,
                "filter": False,
            },
        }
        with patch(
            "cool_open_client.cool_automation_client.UnitsApi"
        ) as fake_units_api_cls:
            fake_units_api = fake_units_api_cls.return_value
            fake_units_api.units_get = AsyncMock(return_value=fake_response)

            result = await self.client.get_updated_controllable_units()

        self.assertEqual(set(result.keys()), {"unit-A", "unit-B"})
        for message in result.values():
            self.assertIsInstance(message, UnitUpdateMessage)
        self.assertEqual(result["unit-A"].unit_id, "unit-A")
        # One bulk HTTP call total — the whole point of the new method.
        self.assertEqual(fake_units_api.units_get.await_count, 1)

    async def test_skips_non_controllable_types(self):
        fake_response = MagicMock()
        fake_response.data = {
            "unit-A": {"id": "unit-A", "type": 1, "active_setpoint": 22.0},
            "device-X": {"id": "device-X", "type": 2},  # not a unit
        }
        with patch(
            "cool_open_client.cool_automation_client.UnitsApi"
        ) as fake_units_api_cls:
            fake_units_api = fake_units_api_cls.return_value
            fake_units_api.units_get = AsyncMock(return_value=fake_response)

            result = await self.client.get_updated_controllable_units()

        self.assertIn("unit-A", result)
        self.assertNotIn("device-X", result)
```

- [ ] **Step 2: Run the test to verify it fails**

Run from `CoolControlOpenClient/`:

```bash
python -m unittest tests.test_get_updated_controllable_units -v
```

Expected: FAIL with `AttributeError: 'CoolAutomationClient' object has no attribute 'get_updated_controllable_units'` (or test errors of that shape).

- [ ] **Step 3: Add the method**

In `CoolControlOpenClient/cool_open_client/cool_automation_client.py`:

1. Add imports near the top (alongside the existing `from .utils.dict_to_model import dict_to_model`):

```python
from .utils.units_payload import ensure_dict, extract_units_mapping
```

2. Locate `async def get_updated_controllable_unit(self, unit_id: str)` (the existing per-unit method) and insert this method directly below it:

```python
    @with_exception
    async def get_updated_controllable_units(self) -> dict[str, UnitUpdateMessage]:
        """
        Bulk equivalent of `get_updated_controllable_unit`.

        Issues a single HTTP request and returns a mapping of
        `unit_id -> UnitUpdateMessage` for every controllable unit
        (those with `type` in `(None, 1)`). Mirrors the transformation
        applied by the per-unit method so callers can feed the messages
        straight into `HVACUnit._update_unit`.
        """
        api = UnitsApi(api_client=self.api_client)
        response: UnitsResponse = await api.units_get(
            x_access_token=self.token,
            origin=self.ORIGIN,
            referer=self.REFERER,
        )

        updates: dict[str, UnitUpdateMessage] = {}
        payload = extract_units_mapping(response.data)

        for unit_id, raw in payload.items():
            raw_dict = ensure_dict(raw)
            # Match HVACUnitsFactory's filter exactly: applied to the raw
            # dict, before model construction, so we don't waste work on
            # entries the factory itself would skip.
            if isinstance(raw_dict, dict) and raw_dict.get("type") not in (None, 1):
                continue
            try:
                unit = dict_to_model(UnitResponseData, raw)
            except TypeError:
                continue

            message = UnitUpdateMessage(
                ambient_temperature=round_temperature_value(unit.ambient_temperature),
                fan_mode=unit.active_fan_mode,
                operation_mode=unit.active_operation_mode,
                setpoint=round_temperature_value(unit.active_setpoint),
                swing=unit.active_swing_mode,
                operation_status=unit.active_operation_status,
                filter=unit.filter,
                unit_id=unit.id or unit_id,
            )
            updates[message.unit_id] = self._transform_message(message)

        return updates
```

3. If `UnitResponseData` is not already imported at the top of the file, add:

```python
from .client.models.unit_response_data import UnitResponseData
```

(Check the existing imports first — `get_updated_controllable_unit` already
uses `UnitResponseData` directly via the per-unit path, so it may already be
imported.)

- [ ] **Step 4: Run the test to verify it passes**

Run from `CoolControlOpenClient/`:

```bash
python -m unittest tests.test_get_updated_controllable_units -v
```

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
cd CoolControlOpenClient
git add cool_open_client/cool_automation_client.py tests/test_get_updated_controllable_units.py
git commit -m "feat: add get_updated_controllable_units bulk method"
```

---

## Task 3: Bump library version

**Files:**
- Modify: `CoolControlOpenClient/setup.py`

- [ ] **Step 1: Bump the version**

In `CoolControlOpenClient/setup.py`, find the line `version="0.0.18",` and
change it to:

```python
    version="0.0.19",
```

- [ ] **Step 2: Verify the package still builds**

Run from `CoolControlOpenClient/`:

```bash
python -m build --sdist --wheel --outdir dist/
```

Expected: builds succeed, produces `dist/cool_open_client-0.0.19*` artifacts.

(If `build` is not available, install it once: `pip install build`.)

- [ ] **Step 3: Commit and tag**

```bash
cd CoolControlOpenClient
git add setup.py
git commit -m "chore: bump cool-open-client to 0.0.19"
git tag v0.0.19
```

- [ ] **Step 4: Publish to PyPI** *(performed by the maintainer; not by an automated agent)*

```bash
cd CoolControlOpenClient
python -m twine upload dist/cool_open_client-0.0.19*
```

This is a human-in-the-loop step. Confirm the new version is visible at
<https://pypi.org/project/cool-open-client/> before moving to Task 4.

---

## Task 4: Set up the integration test scaffold

**Files:**
- Create: `cool-open-integration/requirements_test.txt`
- Create: `cool-open-integration/tests/__init__.py`
- Create: `cool-open-integration/tests/conftest.py`

The integration has zero tests today. Establish the minimum needed to write
the coordinator tests in Task 5.

- [ ] **Step 1: Create the test-requirements file**

Create `cool-open-integration/requirements_test.txt`:

```
pytest>=8.0
pytest-asyncio>=0.23
pytest-homeassistant-custom-component>=0.13
```

- [ ] **Step 2: Install test requirements**

Run from `cool-open-integration/`:

```bash
pip install -r requirements_test.txt
```

- [ ] **Step 3: Create the tests package marker**

Create `cool-open-integration/tests/__init__.py` (empty file).

- [ ] **Step 4: Create the conftest**

Create `cool-open-integration/tests/conftest.py`:

```python
"""Shared pytest fixtures for the cool_open_integration tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Auto-enable custom_components for every test in this directory.

    `enable_custom_integrations` is a fixture supplied by
    `pytest-homeassistant-custom-component`. Wrapping it in an autouse
    fixture lets every test pick it up without listing it explicitly.
    """
    yield
```

- [ ] **Step 5: Run pytest collection to confirm the scaffold works**

Run from `cool-open-integration/`:

```bash
pytest tests/ --collect-only -q
```

Expected: collection succeeds with 0 tests collected (no tests yet).

- [ ] **Step 6: Commit**

```bash
cd cool-open-integration
git add requirements_test.txt tests/__init__.py tests/conftest.py
git commit -m "test: add pytest scaffolding for the integration"
```

---

## Task 5: Coordinator test — bulk fan-out

**Files:**
- Create: `cool-open-integration/tests/test_coordinator.py`

This test is written first and will fail. It encodes the contract we want:
exactly one bulk HTTP call per cycle, regardless of how many units exist.

- [ ] **Step 1: Write the failing test**

Create `cool-open-integration/tests/test_coordinator.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run from `cool-open-integration/`:

```bash
pytest tests/test_coordinator.py::test_one_bulk_call_per_cycle_regardless_of_unit_count -v
```

Expected: FAIL — current coordinator either calls `unit.refresh()` (which
hits a missing per-unit endpoint on the mock) or, if the mock tolerates it,
fails the `await_count == 1` assertion on `get_updated_controllable_units`.

- [ ] **Step 3: (Do not implement yet — proceed to Task 6 for the rewrite.)**

The implementation lives in Task 6 so the rewrite step has all three
coordinator tests in front of it.

---

## Task 6: Rewrite the coordinator to use the bulk method

**Files:**
- Modify: `cool-open-integration/custom_components/cool_open_integration/coordinator.py`

- [ ] **Step 1: Rewrite `_async_update_data`**

Open `cool-open-integration/custom_components/cool_open_integration/coordinator.py`.

Replace the entire `_async_update_data` method body so the file looks
exactly like this (only the method body changes; class definition,
imports, and `__init__` are unchanged):

```python
from __future__ import annotations

from datetime import timedelta
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from cool_open_client.cool_automation_client import CoolAutomationClient
from cool_open_client.unit import HVACUnit

from .const import DOMAIN, POLL_INTERVAL

_LOGGER = logging.getLogger(__package__)


class CoolAutomationDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Coolmaster data."""

    data: dict[str, HVACUnit]

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: CoolAutomationClient, units: list[HVACUnit]
    ) -> None:
        """Initialize global Coolmaster data updater."""
        _LOGGER.debug("Init Cool Automation update coordinator")
        self._client = client
        self.hass = hass
        self.units = units

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=POLL_INTERVAL))

    async def _async_update_data(self):
        """Fetch data from Coolmaster.

        Issues a single bulk request for all units and distributes the
        resulting `UnitUpdateMessage` objects to the in-memory `HVACUnit`
        instances. Replaces the previous per-unit fan-out which caused
        excessive API traffic on large installations.
        """
        try:
            updates = await self._client.get_updated_controllable_units()
        except OSError as error:
            raise UpdateFailed from error
        except Exception as error:  # noqa: BLE001 — surface as UpdateFailed
            raise UpdateFailed(f"Bulk unit update failed: {error}") from error

        data: dict[str, HVACUnit] = {}
        for unit in self.units:
            message = updates.get(unit.id)
            if message is not None:
                # `with_callback=False` matches the prior `unit.refresh()`
                # behaviour: the coordinator notifies listeners itself.
                unit._update_unit(message, with_callback=False)
            # A unit absent from the bulk response keeps its last-known
            # state — same tolerance the previous code had for transient
            # per-unit failures.
            data[unit.id] = unit
            unit.reset_update()
        return data

    @property
    def client(self):
        return self._client
```

- [ ] **Step 2: Run the bulk-call test to verify it passes**

Run from `cool-open-integration/`:

```bash
pytest tests/test_coordinator.py::test_one_bulk_call_per_cycle_regardless_of_unit_count -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd cool-open-integration
git add custom_components/cool_open_integration/coordinator.py tests/test_coordinator.py
git commit -m "feat: use bulk endpoint in coordinator (fixes traffic spike)"
```

---

## Task 7: Coordinator test — entity identity preserved

**Files:**
- Modify: `cool-open-integration/tests/test_coordinator.py`

- [ ] **Step 1: Add the test**

Append to `cool-open-integration/tests/test_coordinator.py`:

```python
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
    # And `_update_unit` was invoked for each (mutation path preserved).
    original_a._update_unit.assert_called_once()
    original_b._update_unit.assert_called_once()
```

- [ ] **Step 2: Run the test to verify it passes**

Run from `cool-open-integration/`:

```bash
pytest tests/test_coordinator.py::test_entity_identity_preserved_across_refresh -v
```

Expected: PASS (the rewrite from Task 6 already satisfies this contract).

- [ ] **Step 3: Commit**

```bash
cd cool-open-integration
git add tests/test_coordinator.py
git commit -m "test: lock entity identity contract across coordinator refresh"
```

---

## Task 8: Coordinator test — missing-unit tolerance

**Files:**
- Modify: `cool-open-integration/tests/test_coordinator.py`

- [ ] **Step 1: Add the test**

Append to `cool-open-integration/tests/test_coordinator.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it passes**

Run from `cool-open-integration/`:

```bash
pytest tests/test_coordinator.py::test_missing_unit_in_bulk_response_keeps_last_known_state -v
```

Expected: PASS (the rewrite from Task 6 already satisfies this contract).

- [ ] **Step 3: Run the full coordinator test file**

Run from `cool-open-integration/`:

```bash
pytest tests/test_coordinator.py -v
```

Expected: all three tests PASS.

- [ ] **Step 4: Commit**

```bash
cd cool-open-integration
git add tests/test_coordinator.py
git commit -m "test: tolerate units missing from bulk update response"
```

---

## Task 9: Bump integration requirement and version

**Files:**
- Modify: `cool-open-integration/custom_components/cool_open_integration/manifest.json`

- [ ] **Step 1: Update `manifest.json`**

Open `cool-open-integration/custom_components/cool_open_integration/manifest.json` and apply these two edits:

1. In the `requirements` array, change
   `"cool-open-client==0.0.18"` to
   `"cool-open-client==0.0.19"`.

2. Change the top-level `"version"` field from
   `"0.0.14"` to
   `"0.0.15"`.

The file should look like (other fields untouched):

```json
{
  "domain": "cool_open_integration",
  "name": "CoolAutomation Cloud Open Integration",
  "codeowners": ["@ShayGus"],
  "config_flow": true,
  "dependencies": [],
  "documentation": "https://github.com/ShayGus/cool-open-integration/wiki",
  "homekit": {},
  "iot_class": "cloud_polling",
  "issue_tracker": "https://github.com/ShayGus/cool-open-integration/issues",
  "requirements": ["cool-open-client==0.0.19"],
  "ssdp": [],
  "version": "0.0.15",
  "zeroconf": []
}
```

- [ ] **Step 2: Re-run the test suite to confirm nothing broke**

Run from `cool-open-integration/`:

```bash
pytest tests/ -v
```

Expected: all three tests PASS.

- [ ] **Step 3: Commit**

```bash
cd cool-open-integration
git add custom_components/cool_open_integration/manifest.json
git commit -m "chore: pin cool-open-client==0.0.19 and bump version to 0.0.15"
```

---

## Task 10: Manual smoke test on a real Home Assistant install

This is a human verification step — the automated tests cannot prove a real
HTTP call goes to CoolAutomation's API. Do not skip.

- [ ] **Step 1: Install the new integration version in a test HA instance**

Copy `cool-open-integration/custom_components/cool_open_integration/` into
your test HA's `custom_components/` directory (or pull via HACS once
released). Make sure `pip` resolves `cool-open-client==0.0.19` — restart HA
if it had the old version cached.

- [ ] **Step 2: Enable DEBUG logging for the integration**

Add to the test HA's `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.cool_open_integration: debug
    cool_open_client: debug
```

Restart HA.

- [ ] **Step 3: Watch the logs for one full poll cycle**

Tail the log:

```bash
tail -f /path/to/homeassistant.log | grep -Ei 'cool_open|units'
```

Wait ~35 seconds. You should see exactly one bulk request per cycle. Expect a log line confirming `get_updated_controllable_units` was called, and **no** repeated `get_updated_controllable_unit` (singular) calls.

- [ ] **Step 4: Confirm climate entities still update**

In the HA UI, change one of the climate entities' setpoint or operation
mode via the integration. Confirm:

1. The change is accepted (no error).
2. Within ~30 seconds the entity reflects the new state.
3. Other entities' states have not been blanked or duplicated.

- [ ] **Step 5: Record the request rate**

Note the number of HTTP requests logged to CoolAutomation in a 5-minute
window. For a 10-unit install: expect ~10 requests (one bulk per 30s × 10
cycles). Previously this would have been ~100.

- [ ] **Step 6: Notify Adam**

Send an update to `adam@coolautomation.com`:

> Released `cool-open-integration` 0.0.15 with `cool-open-client` 0.0.19.
> The integration now issues a single bulk request per 30-second cycle
> instead of one per unit. Expected traffic shape: ~2,880 requests/day
> per HA install regardless of unit count. The hotel customer (and any
> commercial sites flagged) should see >98% reduction within 24 hours
> as installs auto-update. Step 2 (WebSocket-driven push updates) is in
> design and will land in a follow-up release.

---

## Done criteria

- [ ] `cool-open-client==0.0.19` is on PyPI and includes
  `get_updated_controllable_units`.
- [ ] `cool-open-integration==0.0.15` is tagged, the manifest pins the new
  client version, and the coordinator uses the bulk method.
- [ ] All three coordinator tests pass under `pytest tests/`.
- [ ] All library tests still pass (existing tests + the new mocked test).
- [ ] Manual smoke test on a real account confirms one HTTP request per cycle.
- [ ] Adam has been notified.
