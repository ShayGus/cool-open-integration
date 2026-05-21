# Cool Open WebSocket Push Updates — Step 2 Design

**Date:** 2026-05-20
**Status:** Approved scope; pending implementation plan
**Predecessor:** `2026-05-20-cool-open-api-traffic-reduction-design.md` (Step 1 + Step 1.5 — bulk endpoint + SSL context, shipped as `cool-open-client 0.0.20` / `cool-open-integration 0.0.16`)

## Context

Step 1 dropped per-cycle API traffic from O(N) to O(1) by switching the
coordinator's 30-second poll to a single bulk request. This solved
CoolAutomation's immediate concern (the 87-unit hotel customer went from
~250K req/day to ~2,880).

Step 2 finishes the job by moving the integration from polling to **push
updates over the existing CoolAutomation WebSocket**. The library already
has working WS plumbing (now bug-fixed by community PR #2 in 0.0.21-pending),
but uses the thread-based `websocket-client` package — not safe for the HA
event loop. (See memory `project-cool-open-integration-ws-decision`.) This
spec replaces it with an `aiohttp`-native async path and rewires the
integration's coordinator to be push-driven, with a low-frequency
reconciliation poll as a safety net.

Net traffic per site after Step 2: 1 long-lived WS connection plus ~288
reconciliation HTTP requests per day, regardless of unit count.

## Goals

- Convert `cool-open-integration` to `iot_class: cloud_push`.
- Replace the integration's 30-second bulk poll with a 5-minute
  reconciliation poll plus real-time WS updates.
- Remove the thread-based WS implementation from `cool-open-client` and
  drop the `websocket-client` PyPI dependency.
- Preserve all behaviour Step 1 locked in: `HVACUnit` instance identity,
  in-place mutation, missing-unit tolerance.

## Non-goals

- Driving entity *commands* over WS (still HTTP via existing client methods).
- Surfacing non-`UPDATE_UNIT` WS messages (sensors, power meters, events).
- Multi-account / multi-entry shared WS connection.
- A user-facing config option to disable WS. (Easy to add later if a need
  emerges; not adding speculatively.)
- Changes to the bulk `_async_update_data` body — same one-line bulk call
  shipped in Step 1.

## Architecture

```
                ┌──────────────────────────────────────────────┐
                │  cool-open-client  (new 0.0.21)              │
                │                                              │
                │  CoolAutomationClient.subscribe_unit_updates()│
                │    → AsyncIterator[WsEvent]                  │
                │                                              │
                │    WsEvent = UnitUpdate(message) |           │
                │              Reconnected()                   │
                │                                              │
                │  Internal aiohttp.ClientSession.ws_connect   │
                │  with exponential backoff (1,2,4,…,60s)      │
                │  Filters to UPDATE_UNIT only                 │
                │  Reuses _transform_message, UnitUpdateMessage│
                │                                              │
                │  REMOVED: open_socket, WebSocketThread,      │
                │   on_open/message/error/close, ping/pong,    │
                │   HVACUnit.regiter_callback,                 │
                │   `websocket-client` PyPI dep                │
                └──────────────────────────────────────────────┘
                                  ▲
                                  │ async iterator
                                  │
                ┌──────────────────────────────────────────────┐
                │  cool-open-integration  (new 0.0.17)         │
                │                                              │
                │  __init__.py async_setup_entry:              │
                │    after coordinator first refresh,          │
                │    entry.async_create_background_task(       │
                │        _ws_pump(coordinator))                │
                │                                              │
                │  _ws_pump:                                   │
                │    async for ev in client.subscribe_         │
                │                     unit_updates():          │
                │      if UnitUpdate:                          │
                │        unit._update_unit(ev.message,         │
                │                          with_callback=False)│
                │        coordinator.async_set_updated_data(…) │
                │      elif Reconnected:                       │
                │        await coordinator.async_request_      │
                │                         refresh()            │
                │                                              │
                │  coordinator update_interval = 5 minutes     │
                │  (was 30 s); _async_update_data unchanged    │
                │                                              │
                │  manifest.json: iot_class → cloud_push       │
                └──────────────────────────────────────────────┘
```

Two complementary update channels feed one `DataUpdateCoordinator`:

1. **WS pump (primary).** A background task tied to the config entry's
   lifecycle, reading the library's async iterator forever. Each
   `UnitUpdate` mutates the corresponding in-memory `HVACUnit` and pushes a
   coordinator update.
2. **Reconciliation (safety net).** `update_interval = 5 minutes` runs the
   same one-bulk-call `_async_update_data` we wrote in Step 1. The WS pump
   also triggers an immediate `async_request_refresh()` on every
   `Reconnected` event so we catch up without waiting up to 5 minutes.

## Library component (`cool-open-client`)

### New public API

```python
# cool_open_client/ws_events.py  (new module)
from dataclasses import dataclass
from typing import Union
from .cool_automation_client import UnitUpdateMessage


@dataclass(frozen=True)
class UnitUpdate:
    """A real-time state change for a single HVAC unit."""
    message: UnitUpdateMessage


@dataclass(frozen=True)
class Reconnected:
    """The WS connection dropped and was restored.

    Consumers should reconcile state since one or more UnitUpdate
    messages may have been missed during the gap.
    """
    pass


WsEvent = Union[UnitUpdate, Reconnected]
```

```python
# new method on CoolAutomationClient
async def subscribe_unit_updates(self) -> AsyncIterator[WsEvent]:
    """Subscribe to real-time unit state changes via WebSocket.

    Yields:
        UnitUpdate: for each `UPDATE_UNIT` server message.
        Reconnected: after the underlying connection is re-established
            following a drop.

    Reconnect is handled internally with exponential backoff (1s, 2s,
    4s, 8s, 16s, 32s, capped at 60s; reset to 1s on successful
    authenticate). Iteration only ends when the caller breaks out or
    the consuming task is cancelled.
    """
```

### Internal implementation

A new module `cool_open_client/ws_subscription.py` holds the WS loop
(~120 lines). `CoolAutomationClient.subscribe_unit_updates` delegates to
it. Pseudocode:

```python
async def _run(self) -> AsyncIterator[WsEvent]:
    backoff = 1
    first = True
    while True:
        try:
            async with self._session.ws_connect(
                SOCKET_URI,
                ssl=self._ssl_context,
                heartbeat=30,
            ) as ws:
                await ws.send_json({
                    "type": "authenticate",
                    "content": {"token": self._token},
                })
                # Auth response is implicit — first failure surfaces below.
                if not first:
                    yield Reconnected()
                first = False
                backoff = 1

                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        continue
                    msg = json.loads(raw.data)
                    if msg.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                        continue
                    if msg.get("name") != "UPDATE_UNIT":
                        continue
                    update_message = self._build_message(msg.get("data") or {})
                    if update_message is not None:
                        yield UnitUpdate(update_message)

        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.warning("WS pump error; reconnecting in %ds", backoff,
                                 exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
```

Key properties:
- **SSL context reuse.** Passes `ssl=self.api_client.rest_client.ssl_context`
  — the same Step 1.5 context already constructed off the event loop.
- **Heartbeat.** `heartbeat=30` lets aiohttp send WS-level pings and surface
  dead connections quickly rather than waiting for a TCP timeout.
- **App-level ping/pong.** The CoolAutomation server emits `{"type":"ping"}`
  messages too; we reply with `pong` per the existing protocol (preserved
  from the deleted thread-based code).
- **Filter.** Drops anything where `name != "UPDATE_UNIT"`. (PR #2's fix —
  inherited via the master merge that lands in 0.0.21.)
- **Cancellation-safe.** `CancelledError` propagates; `async with` closes
  the WS session cleanly.

### Removed surface (breaking)

| File / symbol | Action |
|---|---|
| `cool_automation_client.py` — `open_socket`, `on_open_socket`, `on_message_socket`, `on_error_socket`, `on_close_socket`, `_handle_ping_pong`, `_handle_ws_message`, `ws_thread` attr | Delete |
| `cool_automation_client.py` — `WebSocketThread` class | Delete |
| `cool_automation_client.py` — `import websocket`, `from websocket import …` | Delete |
| `unit.py` — `HVACUnit.regiter_callback` (sic), `unit_update_callback` protocol, `notify` plumbing | Delete |
| `cool_automation_client.py` — `_registered_units` dict + related methods | Delete |
| `setup.py` / `requirements.txt` — `websocket-client` | Remove |
| `tests/test_websocket.py` (existing live-API test, currently skipped without `token.txt`) | Delete or rewrite to exercise the new aiohttp path |

`aiohttp` is already a transitive dep via the REST client; no new package.

### Kept and reused

- `UnitUpdateMessage` dataclass — single source of truth for the message
  shape, consumed by both the HTTP bulk path (Step 1) and the WS path
  (Step 2).
- `_transform_message` — same numeric-id-to-string translation for both
  paths. Factored if needed so `subscribe_unit_updates` can call it
  without reaching into the bulk endpoint logic.
- `HVACUnit._update_unit(message, with_callback=False)` — integration calls
  it directly per `UnitUpdate` event, exactly as Step 1's coordinator does
  per bulk response. With_callback=False is preserved.

## Integration component (`cool-open-integration`)

### Setup flow

```python
# __init__.py — key changes (only the new bits shown)

from cool_open_client.ws_events import UnitUpdate, Reconnected

async def async_setup_entry(hass, entry):
    # ... (unchanged: SSL context, token, client, factory, initial units,
    #     coordinator construction, async_config_entry_first_refresh) ...

    entry.async_create_background_task(
        hass,
        _ws_pump(coordinator),
        name=f"{DOMAIN}_ws_pump",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _ws_pump(coordinator):
    """Long-running consumer of the library's WS event stream."""
    client = coordinator.client
    units_by_id = {u.id: u for u in coordinator.units}

    try:
        async for event in client.subscribe_unit_updates():
            if isinstance(event, UnitUpdate):
                unit = units_by_id.get(event.message.unit_id)
                if unit is None:
                    # Unit appeared after setup; will be picked up by the
                    # next reconciliation poll.
                    continue
                unit._update_unit(event.message, with_callback=False)
                coordinator.async_set_updated_data(coordinator.data)
            elif isinstance(event, Reconnected):
                # WS came back; some messages may have been missed during
                # the gap. Trigger one immediate bulk reconcile.
                await coordinator.async_request_refresh()
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception(
            "WS pump terminated unexpectedly; entry will rely on the "
            "5-minute reconciliation poll until reload",
        )
```

### Coordinator change

```python
# coordinator.py
RECONCILE_INTERVAL = timedelta(minutes=5)

class CoolAutomationDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client, units):
        # ...
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=RECONCILE_INTERVAL,
        )

    async def _async_update_data(self):
        # ↳ unchanged from Step 1 — one bulk call, distribute to in-memory units
        ...
```

`POLL_INTERVAL` in `const.py` becomes dead. Remove it.

### Manifest

```diff
-  "iot_class": "cloud_polling",
+  "iot_class": "cloud_push",
-  "requirements": ["cool-open-client==0.0.20"],
+  "requirements": ["cool-open-client==0.0.21"],
-  "version": "0.0.16",
+  "version": "0.0.17",
```

### Unload behaviour

No integration-side changes needed. `entry.async_create_background_task`
registers the task with the entry; HA cancels it automatically during
`async_unload_entry`. The library catches `CancelledError`, closes the
aiohttp WS session, and re-raises — clean shutdown.

## Data flow per event

```
CoolAutomation server pushes {"name":"UPDATE_UNIT", "data":{…}}
        │
        ▼
aiohttp.ClientWebSocketResponse  (inside subscribe_unit_updates)
        │
        ▼
filter: name == "UPDATE_UNIT"
        │
        ▼
build UnitUpdateMessage from data; _transform_message
        │
        ▼
yield UnitUpdate(message)
        │  ← async iterator boundary
        ▼
_ws_pump receives UnitUpdate
        │
        ▼
units_by_id[message.unit_id]._update_unit(message, with_callback=False)
        │
        ▼
coordinator.async_set_updated_data(coordinator.data)
        │
        ▼
CoordinatorEntity subscribers (climate entities) re-render
```

Reconnect path:

```
WS drops  → exception inside subscribe_unit_updates  → log + backoff sleep
        → reconnect, authenticate                    → yield Reconnected()
        │  ← async iterator boundary
        ▼
_ws_pump receives Reconnected
        ▼
await coordinator.async_request_refresh()
        ▼
_async_update_data runs (bulk poll), distributes fresh state to all units
        ▼
coordinator notifies subscribers
```

## Failure modes

| Scenario | Behaviour |
|---|---|
| WS fails to connect on first try (or repeatedly) | Library retries with exponential backoff. Integration state still arrives via the 5-minute reconciliation poll. UI works, just less snappy. |
| Network blip mid-stream | Library reconnects. On success: `Reconnected` event → immediate reconcile. Typical drift window < 10 s. |
| WS pump task crashes with unexpected exception | `_LOGGER.exception` once with traceback; coordinator continues running the reconciliation poll alone. User can reload entry to restart the WS pump. |
| Unit added or removed mid-session | Reconciliation poll discovers the change next cycle (≤ 5 min). WS messages for an unknown `unit_id` are silently skipped. |
| Token expires mid-stream | Library raises `InvalidTokenException` on the failing reconnect attempt; integration's existing reauth logic in `async_setup_entry` kicks in on next reload. (Token refresh during a live WS session is out of scope here — same surface as today.) |
| Race: bulk-poll reconcile mutates a unit at the exact moment a WS message does | Both paths call `unit._update_unit` (idempotent on identical payloads) and then push coordinator updates. Worst case: one redundant entity state push. |

## Testing

### Library (mocked — no live API)

1. **`test_subscribe_yields_unit_update_per_update_unit_message`** — patch
   `aiohttp.ClientSession.ws_connect` to a fake context manager yielding a
   sequence of WS messages including one `UPDATE_UNIT` and one
   `UPDATE_SENSOR`. Assert the iterator yields exactly one `UnitUpdate`
   with the correct payload.
2. **`test_subscribe_emits_reconnected_after_drop`** — fake WS raises after
   the first message; assert iterator yields the first `UnitUpdate`,
   then `Reconnected`, then resumes yielding from the second connection.
3. **`test_subscribe_responds_to_app_level_ping`** — fake WS pushes
   `{"type":"ping"}`; assert the integration replies with `{"type":"pong"}`
   via `ws.send_json`.
4. **`test_subscribe_cancellation_closes_session`** — start iteration,
   cancel the consuming task, assert the WS session was closed.

Co-located with existing Step 1 tests under `CoolControlOpenClient/tests/`.

### Integration

1. **`test_ws_unit_update_mutates_coordinator_data`** — feed `_ws_pump`
   a stub async iterator emitting one `UnitUpdate`; assert the
   corresponding `HVACUnit._update_unit` was called and
   `async_set_updated_data` fired.
2. **`test_ws_reconnect_triggers_refresh`** — emit a `Reconnected` event;
   assert `coordinator.async_request_refresh()` was called.
3. **`test_ws_pump_unknown_unit_id_ignored`** — emit a `UnitUpdate` for a
   `unit_id` not in the integration's units list; assert no exception and
   no mutation.

Existing Step 1 coordinator tests (3) continue to pass unchanged.

## Release sequencing

1. **Library first.** Cut `cool-open-client 0.0.21` from `master` (already
   has community PR #2's fixes); publish to PyPI. End users still on
   integration 0.0.16 are unaffected — their pin `cool-open-client==0.0.20`
   continues to resolve.
2. **Integration next.** PR onto `main`, merge, tag `0.0.17`. HACS rolls it
   out. Pip resolves `cool-open-client==0.0.21` on each HA at first
   restart.

Rollback path is symmetric: HACS downgrade reverts the pin; the WS code
becomes dead code on the previous integration version.

## Risks worth tracking

| Risk | Mitigation |
|---|---|
| CoolAutomation has undocumented WS rate limits (e.g., one connection per token) | Pending Adam's reply. If multi-instance HA setups break, throttle reconnects further or add a "WS disabled" config option in a follow-up. |
| Reconciliation poll racing with a WS update during the same moment | Both call paths mutate `HVACUnit` in place. Worst case: one redundant entity state push. Idempotent. |
| HA versions without `entry.async_create_background_task` (added 2023.8) | Current HA installs comfortably satisfy this. Document min HA version in `manifest.json` if needed. |
| `aiohttp.ClientSession.ws_connect` ssl param compatibility with HA's bundled aiohttp | Same SSL context used successfully in Step 1.5 for REST; WS uses the same transport. Low risk. |
| Token-refresh-during-live-WS | Out of scope; same surface as today. Open as a follow-up if user complaints emerge. |

## Success criteria

- Integration declares `iot_class: cloud_push`.
- Steady-state HTTP traffic per site: ~288 reconciliation requests/day +
  1 long-lived WS connection.
- Real-world: a unit changed via wall remote appears in the HA UI within
  seconds, not 30 s.
- All Step 1 coordinator tests + new Step 2 tests pass.
- Adam confirms (or doesn't complain about) the new traffic shape and
  WS connection count.
