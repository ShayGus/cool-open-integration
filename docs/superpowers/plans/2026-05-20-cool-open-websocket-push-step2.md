# Cool Open WebSocket Push Updates — Step 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the integration's 30-second bulk polling with a push-driven coordinator backed by a new aiohttp-native WebSocket subscription in `cool-open-client`, and remove the legacy thread-based WS implementation.

**Architecture:** A new async-generator method `CoolAutomationClient.subscribe_unit_updates()` yields `UnitUpdate` events from the CoolAutomation WS and `Reconnected` events after the library transparently reconnects. The HA integration runs the iterator in a background task that mutates `HVACUnit` instances in place and triggers `async_set_updated_data` per update or `async_request_refresh` per reconnect. The bulk poll from Step 1 stays but drops to a 5-minute reconciliation cadence.

**Tech Stack:** Python 3.12+, `aiohttp` (already a transitive dep via the REST client; no new packages), `marshmallow_dataclass` (existing), `homeassistant`, `pytest-homeassistant-custom-component`, `unittest.IsolatedAsyncioTestCase`.

**Spec:** `docs/superpowers/specs/2026-05-20-cool-open-websocket-push-design.md`

---

## Repository layout

Two repos. Must ship together in this order: library `0.0.20 → 0.0.21` (PyPI) first, then integration `0.0.16 → 0.0.17` (HACS).

- **Library** — `CoolControlOpenClient/`
- **Integration** — `cool-open-integration/`

## File structure

**Library (`CoolControlOpenClient/`):**

| File | Change | Purpose |
|---|---|---|
| `cool_open_client/ws_events.py` | Create | Frozen dataclasses: `UnitUpdate`, `Reconnected`. Type alias `WsEvent`. Single import point for the integration. |
| `cool_open_client/cool_automation_client.py` | Modify | Add `subscribe_unit_updates` async generator. Remove `WebSocketThread`, `open_socket`, all `on_*_socket` handlers, `_handle_ping_pong`, `_handle_ws_message`, `register_for_updates`, `_registered_units`, `import websocket`. |
| `cool_open_client/unit.py` | Modify | Remove `regiter_callback` (sic), `notify`, `_update_pending` machinery if unused after the strip. Keep `_update_unit` and `reset_update` (the integration's coordinator still calls them). |
| `tests/test_subscribe_unit_updates.py` | Create | Four mocked tests covering the new method. |
| `tests/test_websocket.py` | Delete | Live-API WS test for the removed thread-based implementation. |
| `setup.py` | Modify | Remove `websocket-client` from `install_requires`. Bump version `0.0.20 → 0.0.21`. |
| `requirements.txt` | Modify | Remove the `websocket-client` line. |

**Integration (`cool-open-integration/`):**

| File | Change | Purpose |
|---|---|---|
| `custom_components/cool_open_integration/__init__.py` | Modify | Spawn `_ws_pump` background task after coordinator first refresh. |
| `custom_components/cool_open_integration/coordinator.py` | Modify | `update_interval` changes from `POLL_INTERVAL` seconds to `RECONCILE_INTERVAL` minutes. |
| `custom_components/cool_open_integration/const.py` | Modify | Replace `POLL_INTERVAL = 30` with `RECONCILE_INTERVAL = 5`. |
| `custom_components/cool_open_integration/manifest.json` | Modify | `iot_class: cloud_polling → cloud_push`; pin `cool-open-client==0.0.21`; version `0.0.16 → 0.0.17`. |
| `tests/test_coordinator.py` | Modify | Append three new tests covering `_ws_pump` behaviour. |

---

## Task ordering rationale

Add new before deleting old, so existing tests that reference the legacy WS code don't break mid-flight. TDD per task. Library finishes (and is locally rebuilt as a wheel) before any integration task runs.

---

## Task 1: Add WsEvent types

**Files:**
- Create: `CoolControlOpenClient/cool_open_client/ws_events.py`

- [ ] **Step 1: Create the module**

Create `CoolControlOpenClient/cool_open_client/ws_events.py`:

```python
"""Public event types yielded by `CoolAutomationClient.subscribe_unit_updates`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    # Imported only for type checkers — runtime import would create a
    # circular dependency with cool_automation_client.
    from .cool_automation_client import UnitUpdateMessage


@dataclass(frozen=True)
class UnitUpdate:
    """A real-time state change for a single HVAC unit.

    Carries the same `UnitUpdateMessage` shape produced by the HTTP path's
    `get_updated_controllable_unit` and `get_updated_controllable_units`,
    so consumers can feed it directly into `HVACUnit._update_unit`.
    """

    message: "UnitUpdateMessage"


@dataclass(frozen=True)
class Reconnected:
    """The WS connection dropped and was restored.

    Consumers should reconcile state — one or more `UnitUpdate` messages
    may have been missed during the gap.
    """

    pass


WsEvent = Union[UnitUpdate, Reconnected]
```

- [ ] **Step 2: Smoke-test the import**

Run from `CoolControlOpenClient/`:

```bash
python3 -c "from cool_open_client.ws_events import UnitUpdate, Reconnected, WsEvent; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
cd CoolControlOpenClient
git add cool_open_client/ws_events.py
git commit -m "feat: add WsEvent types (UnitUpdate, Reconnected)"
```

---

## Task 2: Add `subscribe_unit_updates` (failing tests first)

**Files:**
- Create: `CoolControlOpenClient/tests/test_subscribe_unit_updates.py`

This task writes only the tests. The implementation lands in Task 3.

- [ ] **Step 1: Write the failing tests**

Create `CoolControlOpenClient/tests/test_subscribe_unit_updates.py`:

```python
"""Mocked tests for CoolAutomationClient.subscribe_unit_updates.

No live API: aiohttp.ClientSession.ws_connect is patched to return a fake
WS that delivers a scripted sequence of messages.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

try:
    from cool_open_client.cool_automation_client import CoolAutomationClient
    from cool_open_client.utils.singleton import SingletonMeta
    from cool_open_client.ws_events import Reconnected, UnitUpdate
except ModuleNotFoundError as exc:
    if exc.name == "websocket":
        raise unittest.SkipTest("websocket-client dependency missing")
    raise


def _make_text_msg(payload: dict) -> MagicMock:
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.TEXT
    msg.data = json.dumps(payload)
    return msg


class _FakeWs:
    """Stand-in for aiohttp.ClientWebSocketResponse.

    Scripted: returns each message from `incoming` in turn; raises
    `terminate_with` after the script is exhausted (defaults to a closed
    connection error so the library treats it as a disconnect).
    """

    def __init__(self, incoming, terminate_with=None):
        self._incoming = list(incoming)
        self._terminate = terminate_with or ConnectionResetError("script done")
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise self._terminate
        return self._incoming.pop(0)

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


def _make_unit_update_payload(unit_id: str, setpoint: int = 22) -> dict:
    return {
        "name": "UPDATE_UNIT",
        "data": {
            "id": unit_id,
            "ambientTemperature": 23,
            "fan": 1,
            "filter": False,
            "operationMode": 2,
            "operationStatus": 1,
            "activeSetpoint": setpoint,
            "swing": 0,
        },
    }


class SubscribeUnitUpdatesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        SingletonMeta._instances.pop(CoolAutomationClient, None)
        self.client = CoolAutomationClient.__new__(CoolAutomationClient)
        self.client.token = "test-token"
        self.client.logger = MagicMock()
        # _transform_message reads these dictionaries; stub them to identity.
        self.client.fan_modes = MagicMock(get=lambda v: v)
        self.client.operation_modes = MagicMock(get=lambda v: v)
        self.client.operation_statuses = MagicMock(get=lambda v: v)
        self.client.swing_modes = MagicMock(get=lambda v: v)
        # subscribe_unit_updates accesses the REST client's ssl_context.
        self.client.api_client = MagicMock()
        self.client.api_client.rest_client = MagicMock()
        self.client.api_client.rest_client.ssl_context = None

    async def asyncTearDown(self):
        SingletonMeta._instances.pop(CoolAutomationClient, None)

    @staticmethod
    def _patch_ws_connect(fake_wss, fake_session_holder=None):
        """Return a patch that swaps aiohttp.ClientSession with a stub
        whose ws_connect yields each `fake_wss` in order."""

        class _Sess:
            def __init__(self):
                self._wss = list(fake_wss)
                self.closed = False

            def ws_connect(self, *a, **kw):
                ws = self._wss.pop(0)

                @asynccontextmanager
                async def _cm():
                    yield ws

                return _cm()

            async def close(self):
                self.closed = True

        sess = _Sess()
        if fake_session_holder is not None:
            fake_session_holder.append(sess)
        return patch("aiohttp.ClientSession", return_value=sess)

    async def test_yields_unit_update_per_update_unit_message(self):
        fake = _FakeWs(
            incoming=[
                _make_text_msg(_make_unit_update_payload("unit-A", setpoint=22)),
                _make_text_msg({"name": "UPDATE_SENSOR", "data": {}}),
                _make_text_msg(_make_unit_update_payload("unit-B", setpoint=20)),
            ],
            terminate_with=asyncio.CancelledError(),
        )
        with self._patch_ws_connect([fake]):
            events = []
            try:
                async for ev in self.client.subscribe_unit_updates():
                    events.append(ev)
            except asyncio.CancelledError:
                pass

        unit_updates = [e for e in events if isinstance(e, UnitUpdate)]
        self.assertEqual(len(unit_updates), 2)
        self.assertEqual(unit_updates[0].message.unit_id, "unit-A")
        self.assertEqual(unit_updates[1].message.unit_id, "unit-B")
        # Auth handshake sent on connect.
        self.assertEqual(fake.sent[0]["type"], "authenticate")
        self.assertEqual(fake.sent[0]["content"]["token"], "test-token")

    async def test_emits_reconnected_after_drop(self):
        ws1 = _FakeWs(
            incoming=[_make_text_msg(_make_unit_update_payload("unit-A"))],
            terminate_with=ConnectionResetError("drop"),
        )
        ws2 = _FakeWs(
            incoming=[_make_text_msg(_make_unit_update_payload("unit-B"))],
            terminate_with=asyncio.CancelledError(),
        )

        # Patch asyncio.sleep so backoff is instant in the test.
        async def _instant_sleep(_):
            return

        with self._patch_ws_connect([ws1, ws2]), \
             patch("asyncio.sleep", new=_instant_sleep):
            events = []
            try:
                async for ev in self.client.subscribe_unit_updates():
                    events.append(ev)
                    if len(events) >= 3:
                        raise asyncio.CancelledError()
            except asyncio.CancelledError:
                pass

        self.assertIsInstance(events[0], UnitUpdate)
        self.assertEqual(events[0].message.unit_id, "unit-A")
        self.assertIsInstance(events[1], Reconnected)
        self.assertIsInstance(events[2], UnitUpdate)
        self.assertEqual(events[2].message.unit_id, "unit-B")

    async def test_responds_to_app_level_ping_with_pong(self):
        fake = _FakeWs(
            incoming=[
                _make_text_msg({"type": "ping"}),
                _make_text_msg(_make_unit_update_payload("unit-A")),
            ],
            terminate_with=asyncio.CancelledError(),
        )
        with self._patch_ws_connect([fake]):
            events = []
            try:
                async for ev in self.client.subscribe_unit_updates():
                    events.append(ev)
            except asyncio.CancelledError:
                pass

        # Ping was answered with pong (in addition to the auth message).
        pongs = [m for m in fake.sent if m.get("type") == "pong"]
        self.assertEqual(len(pongs), 1)
        # Ping did not yield a WsEvent — only the UPDATE_UNIT did.
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], UnitUpdate)

    async def test_cancellation_closes_session(self):
        fake = _FakeWs(
            incoming=[_make_text_msg(_make_unit_update_payload("unit-A"))],
            terminate_with=asyncio.CancelledError(),
        )
        holder = []
        with self._patch_ws_connect([fake], fake_session_holder=holder):
            try:
                async for _ in self.client.subscribe_unit_updates():
                    raise asyncio.CancelledError()
            except asyncio.CancelledError:
                pass

        self.assertTrue(holder[0].closed)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run from `CoolControlOpenClient/`:

```bash
python3 -m unittest tests.test_subscribe_unit_updates -v
```

Expected: all four tests FAIL with `AttributeError: 'CoolAutomationClient' object has no attribute 'subscribe_unit_updates'`.

- [ ] **Step 3: (Do not implement yet — proceed to Task 3 for the implementation.)**

---

## Task 3: Implement `subscribe_unit_updates`

**Files:**
- Modify: `CoolControlOpenClient/cool_open_client/cool_automation_client.py`

- [ ] **Step 1: Add the method**

Open `CoolControlOpenClient/cool_open_client/cool_automation_client.py`.

1. Add to the imports near the top of the file (alongside the existing `import json`):

```python
import asyncio
from typing import AsyncIterator

import aiohttp

from .ws_events import Reconnected, UnitUpdate, WsEvent
```

(`json`, `marshmallow`, `normalize_temperature_fields`, `UnitUpdateMessageSchema`, `_transform_message` are already in scope.)

2. Add this method on `CoolAutomationClient`, immediately after `get_updated_controllable_units` (the bulk method we added in Step 1):

```python
    async def subscribe_unit_updates(self) -> AsyncIterator[WsEvent]:
        """Subscribe to real-time unit state changes via WebSocket.

        Yields a `UnitUpdate` for each server `UPDATE_UNIT` message and a
        `Reconnected` event each time the underlying connection is
        re-established following a drop. Reconnect uses exponential
        backoff (1s, 2s, 4s, 8s, 16s, 32s, capped at 60s; reset to 1s on
        successful authenticate). Iteration only ends when the caller
        breaks out or the consuming task is cancelled.
        """
        session = aiohttp.ClientSession()
        backoff = 1
        first_connect = True
        try:
            while True:
                try:
                    async with session.ws_connect(
                        self.SOCKET_URI,
                        ssl=self.api_client.rest_client.ssl_context,
                        heartbeat=30,
                    ) as ws:
                        await ws.send_json({
                            "type": "authenticate",
                            "content": {"token": self.token},
                        })
                        if not first_connect:
                            yield Reconnected()
                        first_connect = False
                        backoff = 1

                        async for raw in ws:
                            if raw.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                msg = json.loads(raw.data)
                            except (TypeError, ValueError):
                                continue
                            if msg.get("type") == "ping":
                                await ws.send_json({"type": "pong"})
                                continue
                            if msg.get("name") != "UPDATE_UNIT":
                                continue
                            data = msg.get("data")
                            if data is None:
                                continue
                            normalized = normalize_temperature_fields(data)
                            update_message = UnitUpdateMessageSchema().load(
                                normalized, unknown=marshmallow.EXCLUDE,
                            )
                            if update_message is None:
                                continue
                            update_message = self._transform_message(update_message)
                            yield UnitUpdate(update_message)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.warning(
                        "WS subscription error; reconnecting in %ds",
                        backoff,
                        exc_info=True,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
        finally:
            await session.close()
```

- [ ] **Step 2: Run the tests to verify they pass**

Run from `CoolControlOpenClient/`:

```bash
python3 -m unittest tests.test_subscribe_unit_updates -v
```

Expected: all four tests PASS.

- [ ] **Step 3: Run the full library test suite (mocked tests only)**

Run from `CoolControlOpenClient/`:

```bash
python3 -m unittest tests.test_get_updated_controllable_units tests.test_ssl_context_passthrough tests.test_subscribe_unit_updates -v
```

Expected: all eleven tests PASS (3 + 4 + 4).

- [ ] **Step 4: Commit**

```bash
cd CoolControlOpenClient
git add cool_open_client/cool_automation_client.py tests/test_subscribe_unit_updates.py
git commit -m "feat: add async WS subscription (subscribe_unit_updates)

Adds an aiohttp-native async generator that yields UnitUpdate per
UPDATE_UNIT server message and Reconnected after each successful
reconnect. Uses the existing SSL context (set by Home Assistant in
Step 1.5) so the WS connection setup never blocks the event loop.
Reuses normalize_temperature_fields + UnitUpdateMessageSchema +
_transform_message so the message shape matches the HTTP bulk path.

Replaces the thread-based path (kept here only briefly — removed in
the next commit)."
```

---

## Task 4: Remove the thread-based WebSocket implementation

**Files:**
- Modify: `CoolControlOpenClient/cool_open_client/cool_automation_client.py`
- Modify: `CoolControlOpenClient/cool_open_client/unit.py`

- [ ] **Step 1: Strip the legacy code from `cool_automation_client.py`**

In `CoolControlOpenClient/cool_open_client/cool_automation_client.py`:

Delete these elements in their entirety:
- The `WebSocketThread` class (the whole class block).
- The `import websocket` statement at the top of the file.
- The `from websocket import (...)` import block (whatever names it brings in — usually `WebSocketApp`, `WebSocketConnectionClosedException`, `WebSocketException`).
- `import threading` and `from threading import Thread` if no other code in the file uses them. (Search for `Thread\b` to confirm.) If they're used elsewhere in the file, leave them.
- The `import gc` and `import time` imports if no other code in the file uses them. (Same check.)
- The `self.socket = None` line in `__init__`.
- The `self._registered_units: dict[str, Updatable] = {}` line in `__init__`.
- The `self.ws_thread: Thread = None` line in `__init__`.
- The `register_for_updates` method.
- The `on_open_socket` method.
- The `on_close_socket` method.
- The `on_message_socket` method.
- The `on_error_socket` method.
- The `_handle_ping_pong` method.
- The `_handle_ws_message` method.
- The `open_socket` method (the whole method block, including the inner `try/except WebSocketException`).

Keep all of:
- `SOCKET_URI` constant — `subscribe_unit_updates` uses it.
- `_transform_message` method.
- `UnitUpdateMessage` dataclass + `UnitUpdateMessageSchema`.
- Everything HTTP-related.
- The new `subscribe_unit_updates` method.
- `from .utils.updatable import Updatable` import — only if `Updatable` is still referenced after the strip (search for `Updatable\b`). If not, remove this import too.

- [ ] **Step 2: Strip the dead callback machinery from `unit.py`**

In `CoolControlOpenClient/cool_open_client/unit.py`:

Delete these elements:
- The `UnitCallback` protocol (if defined here).
- The `unit_update_callback` method on the `UnitCallback` protocol.
- The `regiter_callback` (sic) method on `HVACUnit`.
- The `notify` method on `HVACUnit`.
- The `_callbacks` list attribute initialized in `HVACUnit.__init__` (if present).

Keep:
- `_update_unit(self, message, with_callback=False)` — the integration's coordinator calls this directly.
- `reset_update()`.
- All other methods.

If `HVACUnit._update_unit` previously called any callback when `with_callback=True`, the with_callback parameter and its branch become dead. Remove the parameter and the branch, leaving only the field-mutation logic. **Then update both callers in the library/integration to drop the kwarg:**

- `cool_automation_client.py` — `subscribe_unit_updates` calls `_update_unit(update_message)` (no kwarg needed after this strip; the WS-side call site was just added in Task 3, update it here).
- The integration's coordinator and `_ws_pump` will be updated in Task 8 / Task 9 to drop the kwarg.

- [ ] **Step 3: Confirm imports are clean and tests pass**

Run from `CoolControlOpenClient/`:

```bash
python3 -c "from cool_open_client.cool_automation_client import CoolAutomationClient; print('ok')"
python3 -m unittest tests.test_get_updated_controllable_units tests.test_ssl_context_passthrough tests.test_subscribe_unit_updates -v
```

Expected: import succeeds; all eleven mocked tests still pass.

- [ ] **Step 4: Delete the legacy live-API WS test**

```bash
cd CoolControlOpenClient
rm tests/test_websocket.py
```

(The deleted test exercised `open_socket` against a live server and required a `token.txt`. Its replacement is `tests/test_subscribe_unit_updates.py` from Task 2.)

- [ ] **Step 5: Commit**

```bash
cd CoolControlOpenClient
git add cool_open_client/cool_automation_client.py cool_open_client/unit.py tests/test_websocket.py
git commit -m "refactor: remove thread-based WebSocket implementation

Drops WebSocketThread, open_socket, all on_*_socket handlers,
_handle_ping_pong, _handle_ws_message, register_for_updates,
_registered_units, HVACUnit.regiter_callback, HVACUnit.notify,
and the websocket-client import — superseded by the new
subscribe_unit_updates async generator. The matching live-API
test (tests/test_websocket.py) is deleted; the new async path
is covered by tests/test_subscribe_unit_updates.py."
```

---

## Task 5: Remove the `websocket-client` dependency

**Files:**
- Modify: `CoolControlOpenClient/setup.py`
- Modify: `CoolControlOpenClient/requirements.txt`

- [ ] **Step 1: Drop the dep from `setup.py`**

In `CoolControlOpenClient/setup.py`, remove `"websocket-client"` (and its pinned version, if present) from the `install_requires` list. Save.

- [ ] **Step 2: Drop the dep from `requirements.txt`**

In `CoolControlOpenClient/requirements.txt`, delete the line that pins `websocket-client`. If there is no other dependency declared on that line, the line is removed entirely.

- [ ] **Step 3: Verify imports still work without `websocket-client` installed**

In a clean shell (or just trust the test run):

```bash
cd CoolControlOpenClient
python3 -c "
import sys
# Pretend the package is gone so an accidental import fails loudly.
sys.modules['websocket'] = None
from cool_open_client.cool_automation_client import CoolAutomationClient
print('ok — no websocket-client import')
"
```

Expected: `ok — no websocket-client import`. Any `AttributeError` here means a stray reference survived the Task 4 strip — go back and find it.

- [ ] **Step 4: Commit**

```bash
cd CoolControlOpenClient
git add setup.py requirements.txt
git commit -m "chore: drop websocket-client dependency"
```

---

## Task 6: Bump library to 0.0.21, build wheel, tag

**Files:**
- Modify: `CoolControlOpenClient/setup.py`

- [ ] **Step 1: Bump the version**

In `CoolControlOpenClient/setup.py`, change `version="0.0.20",` to `version="0.0.21",`.

- [ ] **Step 2: Build the distribution**

Run from `CoolControlOpenClient/`:

```bash
python3 -m build --sdist --wheel --outdir dist/
ls dist/cool_open_client-0.0.21*
```

Expected: `dist/cool_open_client-0.0.21-py3-none-any.whl` and `dist/cool_open_client-0.0.21.tar.gz` exist.

- [ ] **Step 3: Commit and tag**

```bash
cd CoolControlOpenClient
git add setup.py
git commit -m "chore: bump cool-open-client to 0.0.21"
git tag v0.0.21
```

- [ ] **Step 4: (Do not publish to PyPI yet.)** PyPI publish happens after the integration smoke test in Task 11.

---

## Task 7: Install the new wheel into the integration test venv

**Files:** _none modified_

The integration's `.venv-test` (created during Step 1) currently has `cool-open-client 0.0.20`. Install 0.0.21 so subsequent integration tests resolve against the new library.

- [ ] **Step 1: Install**

```bash
/home/shayg/projects/HomeAssistant/cool-open-integration/.venv-test/bin/pip install -q --force-reinstall \
  /home/shayg/projects/HomeAssistant/CoolControlOpenClient/dist/cool_open_client-0.0.21-py3-none-any.whl
```

Expected: silent success. Pydantic / urllib3 conflict warnings from `pip` are harmless (same as in Step 1.5 — the warnings don't affect our tests).

- [ ] **Step 2: Verify**

```bash
/home/shayg/projects/HomeAssistant/cool-open-integration/.venv-test/bin/python -c "
from cool_open_client.cool_automation_client import CoolAutomationClient
from cool_open_client.ws_events import UnitUpdate, Reconnected
print('have subscribe_unit_updates:', hasattr(CoolAutomationClient, 'subscribe_unit_updates'))
print('have open_socket (should be False):', hasattr(CoolAutomationClient, 'open_socket'))
"
```

Expected:
```
have subscribe_unit_updates: True
have open_socket (should be False): False
```

---

## Task 8: Integration test — WS pump routes UnitUpdate to coordinator

**Files:**
- Modify: `cool-open-integration/tests/test_coordinator.py`

- [ ] **Step 1: Append the test**

Append to `cool-open-integration/tests/test_coordinator.py`:

```python
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
    coordinator.async_set_updated_data = MagicMock()

    await _ws_pump(coordinator)

    # unit-A got the WS update; unit-B was untouched.
    units[0]._update_unit.assert_called_once()
    units[1]._update_unit.assert_not_called()
    # Coordinator was notified.
    coordinator.async_set_updated_data.assert_called_once()
```

- [ ] **Step 2: Run the test to verify it fails**

Run from `cool-open-integration/`:

```bash
.venv-test/bin/pytest tests/test_coordinator.py::test_ws_pump_routes_unit_update_to_in_memory_unit -v
```

Expected: FAIL with `ImportError: cannot import name '_ws_pump' from 'custom_components.cool_open_integration'`. We add `_ws_pump` in Task 9.

---

## Task 9: Integration test — `Reconnected` triggers a refresh

**Files:**
- Modify: `cool-open-integration/tests/test_coordinator.py`

- [ ] **Step 1: Append the test**

```python
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

    await _ws_pump(coordinator)

    coordinator.async_request_refresh.assert_awaited_once()
    # No UnitUpdate, so no _update_unit call.
    units[0]._update_unit.assert_not_called()
```

- [ ] **Step 2: Run all coordinator tests** to confirm only the two new ones fail:

```bash
.venv-test/bin/pytest tests/test_coordinator.py -v
```

Expected: 3 PASS (Step 1's existing tests), 2 FAIL (the new tests, since `_ws_pump` doesn't exist yet).

---

## Task 10: Integration test — unknown unit_id is silently ignored

**Files:**
- Modify: `cool-open-integration/tests/test_coordinator.py`

- [ ] **Step 1: Append the test**

```python
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
    coordinator.async_set_updated_data = MagicMock()

    # Must not raise.
    await _ws_pump(coordinator)

    units[0]._update_unit.assert_not_called()
    coordinator.async_set_updated_data.assert_not_called()
```

- [ ] **Step 2: Run all coordinator tests** to confirm now three of them fail:

```bash
.venv-test/bin/pytest tests/test_coordinator.py -v
```

Expected: 3 PASS, 3 FAIL.

- [ ] **Step 3: Commit the three new failing tests**

```bash
cd cool-open-integration
git add tests/test_coordinator.py
git commit -m "test: add WS pump contracts (failing — pump implemented next)"
```

---

## Task 11: Implement `_ws_pump` and rewire `async_setup_entry`

**Files:**
- Modify: `cool-open-integration/custom_components/cool_open_integration/__init__.py`

- [ ] **Step 1: Add imports and the pump**

In `cool-open-integration/custom_components/cool_open_integration/__init__.py`:

1. Add to the imports block (after the existing `from cool_open_client.cool_automation_client import ...`):

```python
import asyncio

from cool_open_client.ws_events import Reconnected, UnitUpdate
```

2. Add this module-level coroutine, just below the existing imports / above `async_setup_entry`:

```python
async def _ws_pump(coordinator: "CoolAutomationDataUpdateCoordinator") -> None:
    """Forever-loop consumer of the library's WS event stream.

    Mutates in-memory `HVACUnit` instances per `UnitUpdate` and triggers a
    bulk reconcile on each `Reconnected`. Cancellation propagates so HA
    can stop us cleanly during entry unload.
    """
    client = coordinator.client
    units_by_id = {u.id: u for u in coordinator.units}

    try:
        async for event in client.subscribe_unit_updates():
            if isinstance(event, UnitUpdate):
                unit = units_by_id.get(event.message.unit_id)
                if unit is None:
                    continue
                unit._update_unit(event.message)
                coordinator.async_set_updated_data(coordinator.data)
            elif isinstance(event, Reconnected):
                await coordinator.async_request_refresh()
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception(
            "WS pump terminated unexpectedly; entry will rely on the "
            "5-minute reconciliation poll until reload",
        )
```

3. Inside `async_setup_entry`, find the line `hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator` and insert immediately after it (before the `async_forward_entry_setups` call):

```python
    entry.async_create_background_task(
        hass,
        _ws_pump(coordinator),
        name=f"{DOMAIN}_ws_pump",
    )
```

- [ ] **Step 2: Run the test suite**

```bash
cd cool-open-integration
.venv-test/bin/pytest tests/test_coordinator.py -v
```

Expected: all six tests PASS (3 Step-1 tests + 3 new WS tests).

- [ ] **Step 3: Commit**

```bash
cd cool-open-integration
git add custom_components/cool_open_integration/__init__.py
git commit -m "feat: drive coordinator from WS pump

Spawn a background task per config entry that consumes
client.subscribe_unit_updates(). UnitUpdate mutates the in-memory
HVACUnit and notifies the coordinator immediately; Reconnected
triggers an immediate bulk reconcile. Coordinator lifecycle owns
the task — cancelled automatically on entry unload."
```

---

## Task 12: Switch coordinator to 5-minute reconciliation cadence

**Files:**
- Modify: `cool-open-integration/custom_components/cool_open_integration/const.py`
- Modify: `cool-open-integration/custom_components/cool_open_integration/coordinator.py`

- [ ] **Step 1: Replace `POLL_INTERVAL` with `RECONCILE_INTERVAL` in `const.py`**

Open `cool-open-integration/custom_components/cool_open_integration/const.py`. Find the line `POLL_INTERVAL = 30` and replace the entire line with:

```python
RECONCILE_INTERVAL_MINUTES = 5
```

(Keep the constant on its own line; other constants in the file are unchanged.)

- [ ] **Step 2: Use it in the coordinator**

Open `cool-open-integration/custom_components/cool_open_integration/coordinator.py`. Make these edits:

1. Replace this import:

```python
from .const import DOMAIN, POLL_INTERVAL
```

with:

```python
from .const import DOMAIN, RECONCILE_INTERVAL_MINUTES
```

2. Replace this line in `__init__`:

```python
super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=POLL_INTERVAL))
```

with:

```python
super().__init__(
    hass,
    _LOGGER,
    name=DOMAIN,
    update_interval=timedelta(minutes=RECONCILE_INTERVAL_MINUTES),
)
```

- [ ] **Step 3: Drop the now-removed `with_callback` kwarg from the coordinator's update call**

The `with_callback` parameter on `HVACUnit._update_unit` was removed in Task 4 (the legacy callback machinery is gone). The Step 1 coordinator code still passes `with_callback=False`. Fix that call site.

In `cool-open-integration/custom_components/cool_open_integration/coordinator.py`, find this line inside `_async_update_data`:

```python
unit._update_unit(message, with_callback=False)
```

Replace with:

```python
unit._update_unit(message)
```

- [ ] **Step 4: Re-run the coordinator tests**

```bash
cd cool-open-integration
.venv-test/bin/pytest tests/test_coordinator.py -v
```

Expected: all six tests PASS. (None of the tests depend on the specific interval, so the change is invisible to them.)

- [ ] **Step 5: Commit**

```bash
cd cool-open-integration
git add custom_components/cool_open_integration/const.py custom_components/cool_open_integration/coordinator.py
git commit -m "feat: drop reconciliation poll cadence to 5 minutes

With the WS pump driving live state, the bulk poll is now a drift-
correction safety net rather than the primary update mechanism.
288 req/day per site instead of ~2,880."
```

---

## Task 13: Bump integration manifest

**Files:**
- Modify: `cool-open-integration/custom_components/cool_open_integration/manifest.json`

- [ ] **Step 1: Edit `manifest.json`**

Open `cool-open-integration/custom_components/cool_open_integration/manifest.json` and apply three changes:

1. `"iot_class": "cloud_polling"` → `"iot_class": "cloud_push"`
2. `"cool-open-client==0.0.20"` → `"cool-open-client==0.0.21"`
3. `"version": "0.0.16"` → `"version": "0.0.17"`

The file should look like:

```json
{
  "domain": "cool_open_integration",
  "name": "CoolAutomation Cloud Open Integration",
  "codeowners": ["@ShayGus"],
  "config_flow": true,
  "dependencies": [],
  "documentation": "https://github.com/ShayGus/cool-open-integration/wiki",
  "homekit": {},
  "iot_class": "cloud_push",
  "issue_tracker": "https://github.com/ShayGus/cool-open-integration/issues",
  "requirements": ["cool-open-client==0.0.21"],
  "ssdp": [],
  "version": "0.0.17",
  "zeroconf": []
}
```

- [ ] **Step 2: Confirm tests still pass**

```bash
cd cool-open-integration
.venv-test/bin/pytest tests/test_coordinator.py -v
```

Expected: 6/6 PASS.

- [ ] **Step 3: Commit**

```bash
cd cool-open-integration
git add custom_components/cool_open_integration/manifest.json
git commit -m "chore: pin cool-open-client==0.0.21, version 0.0.17, iot_class cloud_push"
```

---

## Task 14: Manual smoke test on the dev container

This is a human verification step — automated tests cannot prove the WS pump talks to CoolAutomation's real server.

- [ ] **Step 1: Install the 0.0.21 wheel into the devcontainer**

Inside the running `vsc-core` devcontainer:

```bash
pip install --force-reinstall /coc/dist/cool_open_client-0.0.21-py3-none-any.whl
```

- [ ] **Step 2: Restart HA and enable DEBUG logging**

In `/workspaces/core/config/configuration.yaml`, ensure:

```yaml
logger:
  default: warning
  logs:
    custom_components.cool_open_integration: debug
    cool_open_client: debug
```

Restart HA.

- [ ] **Step 3: Verify the WS pump connected**

Wait ~10 seconds, then:

```bash
grep -E "subscribe_unit_updates|WS subscription|authenticate|cool_open_integration_ws_pump" \
  /workspaces/core/config/home-assistant.log | tail -20
```

Expected: an authentication message went out shortly after setup; the pump task is running. No `Detected blocking call` warnings.

- [ ] **Step 4: Verify push updates arrive**

Change a unit's setpoint or mode physically (wall remote) or via CoolAutomation's own UI. Within ~2 seconds, the HA UI should reflect the change. In the log:

```bash
tail -f /workspaces/core/config/home-assistant.log | grep -Ei 'UPDATE_UNIT|update_message|_ws_pump'
```

Expected: an `UPDATE_UNIT` message landed in the pump, `_update_unit` was called, the entity refreshed.

- [ ] **Step 5: Verify the reconciliation poll runs at the new cadence**

```bash
grep -E "get_updated_controllable_units|units_get" /workspaces/core/config/home-assistant.log | tail -10
```

Expected: bulk fetches roughly every 5 minutes — not every 30 seconds.

- [ ] **Step 6: Verify reconnect behaviour**

Disconnect the container's network briefly (`docker network disconnect bridge <container>` then reconnect), or restart your router. Watch the log:

Expected: a "WS subscription error; reconnecting in 1s" warning, then a successful reconnect, then a `Reconnected`-triggered bulk reconcile fires (an extra `units_get` immediately after the reconnect).

- [ ] **Step 7: Verify entity controls still work**

In the HA UI, change a setpoint / mode from the entity card. Confirm:
1. The change is accepted (HTTP call still works).
2. The new state shows up within ~2 seconds (echoed back via the WS).
3. No errors logged.

---

## Task 15: Ship the library — push branch + tag + publish to PyPI

This is the customer-facing release step.

- [ ] **Step 1: Push branch and tag**

```bash
cd CoolControlOpenClient
git push origin <feature-branch>
git push origin v0.0.21
```

- [ ] **Step 2: Open the library PR**

```bash
gh pr create --base master --head <feature-branch> --title "Push updates via async WS subscription (0.0.21)" --body "$(cat <<'EOF'
## Summary

- New async generator `CoolAutomationClient.subscribe_unit_updates()` yields `UnitUpdate` events from the CoolAutomation WebSocket and `Reconnected` events after transparent reconnects.
- Implementation is aiohttp-native and reuses the SSL context Home Assistant passes in (set up in Step 1.5), so the WS connection setup never blocks the event loop.
- Removes the legacy thread-based WS implementation (`WebSocketThread`, `open_socket`, `on_*_socket`, `_handle_ws_message`, `register_for_updates`, the `websocket-client` dependency).

## Breaking changes

Pre-1.0 library. Callers using `open_socket` / `register_for_updates` / `HVACUnit.regiter_callback` will need to switch to `subscribe_unit_updates`. The only known consumer (`cool-open-integration`) is updated in tandem.

## Tests

Four new mocked tests covering: UPDATE_UNIT yield path, Reconnected emission, app-level ping → pong, cancellation closes session.
EOF
)"
```

- [ ] **Step 3: Merge** the PR via the GitHub UI (or `gh pr merge`).

- [ ] **Step 4: Publish to PyPI**

Inside the project root (where `script/publish.sh` lives):

```bash
ls dist/                                                # confirm only 0.0.21 artifacts
rm dist/cool_open_client-0.0.20*                        # purge previously-published version
script/publish.sh
```

Confirm visible at <https://pypi.org/project/cool-open-client/0.0.21/>.

---

## Task 16: Ship the integration — push branch + PR + tag + HACS rollout

- [ ] **Step 1: Push branch**

```bash
cd cool-open-integration
git push origin <feature-branch>
```

- [ ] **Step 2: Open the integration PR**

```bash
gh pr create --base main --head <feature-branch> --title "Push-driven coordinator via WS subscription (0.0.17)" --body "$(cat <<'EOF'
## Summary

- Coordinator is now WS-driven: a background task consumes `cool_open_client.CoolAutomationClient.subscribe_unit_updates()` and pushes state changes into the entity layer in near-real-time.
- The Step 1 bulk poll is kept as a 5-minute reconciliation safety net.
- `iot_class` flipped to `cloud_push`. Manifest pinned to `cool-open-client==0.0.21`.

## Effect

- Per-site traffic drops further: ~288 reconciliation HTTP requests/day + 1 long-lived WS connection. (Was: ~2,880 req/day after Step 1; ~250K/day before.)
- Unit state changes from wall remotes appear in HA within ~2 seconds instead of up to 30.

## Test plan

- [ ] `pytest tests/` — 6/6 (3 Step 1 + 3 new WS pump tests).
- [ ] Manual smoke test on real HA confirms WS connects, UPDATE_UNIT messages flow, reconnect triggers immediate reconcile, controls still work.
EOF
)"
```

- [ ] **Step 3: Merge** the PR.

- [ ] **Step 4: Tag and push the release**

```bash
cd cool-open-integration
git checkout main && git pull --ff-only origin main
git tag 0.0.17
git push origin 0.0.17
```

HACS picks up the new tag and rolls 0.0.17 out to subscribed users over the next 24-48 hours.

---

## Done criteria

- [ ] `cool-open-client==0.0.21` is on PyPI; includes `subscribe_unit_updates` and no `websocket-client` dependency.
- [ ] `cool-open-integration==0.0.17` is tagged on `main`; `manifest.json` pins the new client and declares `iot_class: cloud_push`.
- [ ] All eleven library mocked tests and all six integration tests pass under `pytest` / `unittest`.
- [ ] Manual smoke test on real HA confirms: (1) WS connects without `Detected blocking call` warnings, (2) UPDATE_UNIT pushes arrive in < 2 s, (3) reconciliation poll cadence dropped to 5 min, (4) reconnect triggers immediate bulk reconcile, (5) entity controls still work via HTTP.
- [ ] Adam (CoolAutomation) confirms the new traffic shape after rollout.
