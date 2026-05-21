# Cool Open API Traffic Reduction — Design

**Date:** 2026-05-20
**Status:** Step 1 approved; Step 2 design pending

## Context

CoolAutomation (Adam Itshar, VP R&D, 2026-05-17) reported a major spike in
"unauthorized" / over-quota API traffic traced to the `cool-open-integration`
Home Assistant custom integration. One commercial site (~87-unit hotel) is
generating ~250K requests/day. CoolAutomation has indicated they may restrict
access if usage isn't reduced.

Root cause: `cool-open-integration` polls the per-unit endpoint
(`GET /units/{id}`) once every 30 seconds for *each* unit. With N units this
issues N requests per cycle, ~2,880 × N requests/day per HA install.

Source of the issue:
- `cool-open-integration/custom_components/cool_open_integration/const.py:11` — `POLL_INTERVAL = 30`
- `cool-open-integration/custom_components/cool_open_integration/coordinator.py:33-40` — `_async_update_data` loops `await unit.refresh()` per unit
- `CoolControlOpenClient/cool_open_client/unit.py:127-129` — `HVACUnit.refresh` → `client.get_updated_controllable_unit(self._id)` (per-unit HTTP)

`cool-open-client` already exposes a bulk endpoint (`get_controllable_units()`)
and a thread-based websocket (`ws_thread`, `on_message_socket`), but neither is
used by the coordinator. The websocket cannot be safely consumed by HA today
because the underlying transport is `websocket-client` running in a `Thread` —
not asyncio-native; bridging via `async_add_executor_job` risks blocking HA's
event loop. (See memory `project-cool-open-integration-ws-decision`.)

## Plan

Two phases:

1. **Step 1 — Initial fix release.** Replace per-unit polling with a single
   bulk call per cycle. Solves Adam's traffic problem (~99% reduction) without
   touching the websocket. Ships fast.
2. **Step 2 — Long-term solution.** Refactor `cool-open-client` to expose an
   aiohttp-native async websocket subscription; flip the integration to
   `iot_class: cloud_push` and drive the coordinator via
   `async_set_updated_data`. Polling drops to a low-frequency sanity sync.
   (Design TBD — separate brainstorming round after Step 1 ships.)

This document specifies **Step 1**. Step 2 will be specified separately once
Step 1 is in users' hands and we can observe real-world behaviour.

---

## Step 1 — Initial fix release

### Goal

Reduce per-cycle HTTP request count from O(N) to O(1) for any site with N
units. Preserve current 30-second update cadence, entity identity, and
mutation semantics.

### Non-goals

- WebSocket adoption (Step 2).
- Changing `iot_class` (stays `cloud_polling`).
- Removing the per-unit `unit.refresh()` method from `cool-open-client` (kept
  for backward compatibility; just not called by the coordinator anymore).
- Renaming intentional library typos (`set_opration_mode`,
  `UNAUTHORIZES_ERROR_CODE`) — breaking changes on a published library.
- Lint/format/type-check configuration.
- Broad test coverage push beyond what's needed to lock the new path.

### Architecture

Two coordinated repos:

```
┌────────────────────────────────────────────────────────────────┐
│  cool-open-client  (PyPI: cool-open-client, 0.0.18 → 0.0.19)   │
│                                                                │
│  Add:                                                          │
│    CoolAutomationClient.get_updated_controllable_units()       │
│      → dict[str, UnitUpdateMessage]                            │
│                                                                │
│  Reuses existing:                                              │
│    - UnitsApi.units_get()  (the bulk endpoint)                 │
│    - _transform_message() (same translation per unit)          │
│                                                                │
│  Unchanged:                                                    │
│    - get_updated_controllable_unit() (per-unit, deprecated     │
│      but kept)                                                 │
│    - HVACUnit.refresh()  (still calls the per-unit path)       │
│    - WebSocket plumbing (Step 2)                               │
└────────────────────────────────────────────────────────────────┘
                              ▲
                              │ requirements pin: cool-open-client==0.0.19
                              │
┌────────────────────────────────────────────────────────────────┐
│  cool-open-integration  (manifest.json: 0.0.14 → 0.0.15)       │
│                                                                │
│  Change:                                                       │
│    coordinator._async_update_data:                             │
│      one bulk call → distribute UnitUpdateMessage to each      │
│      HVACUnit via _update_unit(msg, with_callback=False)       │
│                                                                │
│  Unchanged:                                                    │
│    - POLL_INTERVAL = 30  (cadence preserved)                   │
│    - HVACUnit instance identity (entities hold references)     │
│    - Setup flow, config flow, climate entity behaviour         │
└────────────────────────────────────────────────────────────────┘
```

### Components — `cool-open-client`

#### New method: `CoolAutomationClient.get_updated_controllable_units`

```python
@with_exception
async def get_updated_controllable_units(self) -> dict[str, UnitUpdateMessage]:
    """
    Bulk equivalent of get_updated_controllable_unit. Issues one HTTP
    request and returns a mapping of unit_id → transformed update message.
    """
    api = UnitsApi(api_client=self.api_client)
    response: UnitsResponse = await api.units_get(
        x_access_token=self.token,
        origin=self.ORIGIN,
        referer=self.REFERER,
    )

    updates: dict[str, UnitUpdateMessage] = {}
    payload = _extract_units_mapping(response.data)  # module-level helper

    for unit_id, raw in payload.items():
        raw_dict = _ensure_dict(raw)
        # Skip non-controllable types (matches HVACUnitsFactory filter
        # exactly — filter runs on the raw dict, before model construction)
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

The unit-payload extraction (`_extract_units_mapping`) is lifted from
`HVACUnitsFactory._extract_mapping` / `_ensure_dict` and made a module-level
helper so both the factory (setup-time) and the new client method
(update-time) share the same defensive parsing.

#### Backward compatibility

- `get_updated_controllable_unit(unit_id)` stays. Callers outside this repo
  (if any) keep working.
- `HVACUnit.refresh()` stays. It still calls the per-unit endpoint. The
  integration's coordinator simply stops calling it.

#### Versioning

`cool-open-client`: `0.0.18 → 0.0.19`. Patch-level; additive change only.

### Components — `cool-open-integration`

#### Rewrite: `CoolAutomationDataUpdateCoordinator._async_update_data`

Current:

```python
async def _async_update_data(self):
    try:
        data = {}
        for unit in self.units:
            await unit.refresh()
            data[unit.id] = unit
            unit.reset_update()
    except OSError as error:
        raise UpdateFailed from error
    return data
```

New:

```python
async def _async_update_data(self):
    try:
        updates = await self._client.get_updated_controllable_units()
    except OSError as error:
        raise UpdateFailed from error
    except Exception as error:
        # Auth or transport failure: surface as UpdateFailed so HA keeps
        # the last known good data instead of blanking entities.
        raise UpdateFailed(f"Bulk unit update failed: {error}") from error

    data: dict[str, HVACUnit] = {}
    for unit in self.units:
        message = updates.get(unit.id)
        if message is not None:
            unit._update_unit(message, with_callback=False)
        # If a unit is missing from the bulk response, keep its last-known
        # state rather than dropping it — matches today's behaviour when a
        # transient per-unit fetch fails.
        data[unit.id] = unit
        unit.reset_update()
    return data
```

Key properties:
- **One HTTP request per cycle** regardless of N.
- **Entity identity preserved.** Existing `HVACUnit` instances are mutated
  in place. Climate entities and the coordinator both reference units by
  the same `unit_id` keys, so no cross-reference invalidation.
- **`reset_update()` retained** — keeps the existing `_update_pending`
  debounce behaviour after entity-driven writes.
- **Missing-unit handling** — a unit absent from the bulk response is left
  alone, not deleted. Matches existing tolerance for transient failures.

#### Pin bump

`cool-open-integration/custom_components/cool_open_integration/manifest.json`:

```diff
-  "requirements": ["cool-open-client==0.0.18"],
+  "requirements": ["cool-open-client==0.0.19"],
-  "version": "0.0.14",
+  "version": "0.0.15",
```

`iot_class` stays `cloud_polling`. (Step 2 flips it.)

### Data flow

```
HA scheduler fires every 30s
        │
        ▼
coordinator._async_update_data()
        │
        ▼
client.get_updated_controllable_units()  ── ONE HTTPS GET ──►  CoolAutomation API
        │                                                            │
        │                          ◄─────  UnitsResponse  ───────────┘
        ▼
dict[str, UnitUpdateMessage]
        │
        ▼
for unit in self.units:
    unit._update_unit(updates[unit.id], with_callback=False)
        │
        ▼
DataUpdateCoordinator notifies CoordinatorEntity subscribers
        │
        ▼
Climate entities re-render
```

### Error handling

| Failure | Behaviour |
|---|---|
| `OSError` (network) | `UpdateFailed`, HA shows entity unavailability after threshold |
| `aiohttp.ClientError` / HTTP 5xx | Wrapped → `UpdateFailed` |
| HTTP 401 / token expired | Wrapped → `UpdateFailed`. (Token-refresh logic is out of scope; same behaviour as today.) |
| Bulk response missing a unit_id | That unit keeps last-known state; no exception |
| Bulk response contains unknown unit_ids | Ignored (we only mutate units we know about) |
| Pydantic / schema mismatch on a single unit | That unit skipped; loop continues for others. Log at WARNING. |

The integration explicitly does **not** fall back to per-unit polling on
bulk failure. Falling back would re-introduce the traffic problem any time
the bulk endpoint has a transient issue — and bulk failures are typically
auth / availability problems that per-unit calls would also hit.

### Testing

Establish a minimal `pytest-homeassistant-custom-component` scaffold and
two coordinator tests. Goal: lock in the new contract; not exhaustive
coverage.

**Test 1 — request fan-out:** Mock `client.get_updated_controllable_units`
to return a fixture of 100 unit updates. Assert that after one
`_async_update_data()` cycle, `client.get_updated_controllable_unit` was
called **zero** times and `client.get_updated_controllable_units` was
called **once**.

**Test 2 — entity identity preserved:** Start with N units, run a refresh,
assert `coordinator.data[unit_id] is original_unit` for every id (same
object, mutated in place). Then assert one mutated field (e.g. setpoint)
reflects the bulk response.

**Test 3 — missing unit tolerance:** Start with N units; bulk response
omits one. Assert that unit remains in `coordinator.data` with its prior
state and `UpdateFailed` is *not* raised.

Tests live in `cool-open-integration/tests/test_coordinator.py`. Library
already has a test directory (`CoolControlOpenClient/tests/`) — add one
test there exercising the new method against a recorded fixture (no live
API), patterned after the existing async test files.

### Release plan

1. Land library change in `CoolControlOpenClient` main; tag `0.0.19`;
   publish to PyPI.
2. Open integration PR with coordinator change, requirement bump, version
   bump, and tests.
3. Manual smoke test on a real account: confirm one HTTP call per 30s
   cycle in DEBUG logs.
4. Merge integration PR; tag `0.0.15`.
5. Notify Adam with the version and expected traffic shape (~2,880
   req/day per site, regardless of unit count).

### Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Bulk endpoint schema drift breaks parsing | Low | Defensive `_extract_units_mapping` already tolerates pydantic-strict mismatches; per-unit skip on parse error |
| Adam's team ships their own PR with a conflicting shape | Medium | If their PR arrives first and is correct, merge theirs instead. Spec describes the end state, not authorship. |
| HVACUnit.notify() callbacks fire unexpectedly via `_update_unit` | Low | Call site passes `with_callback=False`, matching current `refresh()` |
| Bulk endpoint slower than per-unit at very small N (<5 units) | Very low | At N=5, bulk = 1 req, per-unit = 5 req — bulk is still ≤ per-unit. No regression scenario. |
| Existing users with token expiry get worse error UX | Low | Same surface as today; deferred to Step 2 |

### Success criteria

- Traffic from a 100-unit installation: ≤ 3,000 requests/day (currently ~288,000).
- No regression in climate entity behaviour observable from HA UI.
- All three new tests pass.
- Adam confirms reduced traffic from the hotel customer within one week of
  release.

---

## Step 2 — Long-term solution

**Status:** Shipped 2026-05-21 as `cool-open-client 0.0.21` (PyPI) and
`cool-open-integration 0.0.17` (HACS).

**Design spec:** `docs/superpowers/specs/2026-05-20-cool-open-websocket-push-design.md`

**Implementation plan:** `docs/superpowers/plans/2026-05-20-cool-open-websocket-push-step2.md`

**Outcome:** Replaced the 30-second bulk poll with a push-driven coordinator
backed by `CoolAutomationClient.subscribe_unit_updates()` — an aiohttp-native
async iterator yielding `UnitUpdate` events per server `UPDATE_UNIT` and
`Reconnected` events after each transparent reconnect. The bulk poll is
retained as a 5-minute drift-correction safety net. The legacy thread-based
WebSocket implementation and the `websocket-client` PyPI dependency were
removed entirely. `iot_class` is now `cloud_push`.

**Net traffic per site after Step 2:** ~288 reconciliation HTTP requests/day
plus 1 long-lived WebSocket connection — another ~90% drop on top of Step 1.
