# cool-open-integration — Claude project guide

Home Assistant custom integration for the CoolAutomation cloud platform (iocControl). HACS-distributed under `@ShayGus`.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  custom_components/cool_open_integration/                        │
│                                                                  │
│  __init__.py        Entry setup. Starts the WS pump background  │
│                     task that consumes the library's async       │
│                     iterator and routes events to the coordinator│
│                                                                  │
│  coordinator.py     DataUpdateCoordinator. Bulk poll at 5-min    │
│                     reconciliation cadence (RECONCILE_INTERVAL_  │
│                     MINUTES). Mutates HVACUnit instances in      │
│                     place and notifies entities.                 │
│                                                                  │
│  climate.py         ClimateEntity per HVACUnit.                  │
│  config_flow.py     Username/password auth → token.              │
│  entity.py          CoordinatorEntity base.                      │
│  const.py           DOMAIN, PLATFORMS, RECONCILE_INTERVAL_MINUTES│
└──────────────────────────────────────────────────────────────────┘
                              ▲
                              │ depends on
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  cool-open-client (PyPI dep, pinned in manifest.json)            │
│                                                                  │
│  CoolAutomationClient.subscribe_unit_updates()                   │
│      → AsyncIterator[UnitUpdate | Reconnected]                   │
│  CoolAutomationClient.get_updated_controllable_units()           │
│      → dict[unit_id, UnitUpdateMessage]  (used by reconcile poll)│
└──────────────────────────────────────────────────────────────────┘
```

Two update channels feed one coordinator:

1. **WS pump (primary)** — `_ws_pump` in `__init__.py` reads
   `subscribe_unit_updates()` forever. Each `UnitUpdate` mutates the
   matching `HVACUnit` and pushes via `async_push_unit_update()`. Each
   `Reconnected` triggers an immediate `async_request_refresh`.
2. **Reconciliation poll (safety net)** — `_async_update_data` issues
   one bulk HTTP call every 5 minutes, distributes the result to in-
   memory units. Catches drift if the WS missed messages.

`iot_class: cloud_push`.

## Repo layout

| Path | Purpose |
|---|---|
| `custom_components/cool_open_integration/` | The integration itself |
| `tests/` | Pytest scaffold using `pytest-homeassistant-custom-component`. Run with `.venv-test/bin/pytest tests/`. |
| `docs/superpowers/specs/` | Design docs for past initiatives (traffic reduction, WS push) |
| `docs/superpowers/plans/` | Task-level implementation plans for the same |
| `docs/memory-investigation.md` | Procedure for attributing HA memory growth to (or clearing) this integration |
| `config/` (mounted into devcontainer) | HA's config dir used during local development |

## Companion library

The integration depends on [`cool-open-client`](https://pypi.org/project/cool-open-client/), maintained in a sibling repo at `../CoolControlOpenClient/` (host path) / `/coc/` (devcontainer mount). The library version is pinned in `manifest.json` `requirements`; bump in lockstep when the library changes.

## Development

Inside the HA dev container (defined at `core/.devcontainer/devcontainer.json`):

```bash
# Install the library wheel from the mounted path (no need to copy):
pip install --force-reinstall /coc/dist/cool_open_client-X.Y.Z-py3-none-any.whl

# Run integration tests outside the container:
cd /home/shayg/projects/HomeAssistant/cool-open-integration
.venv-test/bin/pytest tests/ -v
```

Integration source is bind-mounted into the container at `/workspaces/core/config/custom_components/cool_open_integration` — edits land live without copying. Restart HA to pick them up.

## Release flow

1. **Library** (`cool-open-client`):
   - Bump `setup.py`, build wheel, commit, tag `vX.Y.Z`, push.
   - Open PR against `master`, merge.
   - Publish to PyPI: clean `dist/` of older versions, then
     `pipx run --spec 'twine>=6.1' twine upload --non-interactive -u __token__ -p "$(< pypi-token.txt)" dist/cool_open_client-X.Y.Z*`.
2. **Integration** (this repo):
   - Bump `manifest.json` `requirements` pin and `version`. Commit.
   - Open PR against `main`, merge.
   - `git tag X.Y.Z && git push origin X.Y.Z`.
   - **`gh release create X.Y.Z --title X.Y.Z --notes "..."`** — HACS picks
     up GitHub Releases, not just tags. Easy to forget.

## The WebSocket endpoint override (temporary)

CoolAutomation serves REST and WebSocket from **two different hosts**. The
official web SDK (`control.coolremote.net`, `vendors.*.js`) declares:

```js
baseUrl -> https://api.coolremote.net/api/v2
wsUrl   -> wss://ws.coolremote.net/ws/v2
```

`cool-open-client` (every version up to and including 0.0.22) hardcodes
`SOCKET_URI = "wss://api.coolremote.net:443/ws/v2"` — the REST host, which does
not serve `/ws/v2` at all. Verified with a raw HTTP/1.1 upgrade (curl defaults
to HTTP/2, which returns a misleading 404 for both):

```
wss://ws.coolremote.net/ws/v2   -> 101 Switching Protocols
wss://api.coolremote.net/ws/v2  -> 404 {"errorCode":"NOT_FOUND_2"}
```

The API answers `401 BAD_OR_MISSING_CREDENTIALS` for bad auth and
`404 NOT_FOUND_2` for an unknown route, so this is a missing route, not a
credential problem. **Consequence: WS push has never worked in production** —
0.0.20 (thread-based) and 0.0.21/0.0.22 (aiohttp) carry the same wrong URL.
Deployments have been running on the 5-minute reconciliation poll alone.

`_apply_ws_endpoint_override()` in `__init__.py` patches
`CoolAutomationClient.SOCKET_URI` at setup. **This is temporary.** Once the
one-line fix ships in `cool-open-client`, raise the `manifest.json` pin and
delete the override, `WS_URL` in `const.py`, and
`test_ws_endpoint_override_points_at_the_host_that_serves_the_socket`.

## Known constraints / non-obvious behaviour

- **Bad token = clean close, not an exception.** The server answers a rejected
  `authenticate` with `{"type":"error","payload":{"error":"Authentication
  failed"}}` and hangs up. The library drops that frame (it only forwards
  `UPDATE_UNIT`) and treats the close as normal, so its own backoff stays at
  1s and it would reconnect flat out, firing a bulk HTTP refresh each time.
  `_ws_pump` counts `Reconnected` events with no `UnitUpdate` between them,
  backs the stream off (an async generator only advances while you pull from
  it, so sleeping in the pump is what paces the library), and after
  `WS_STALLED_RECONNECT_LIMIT` gives up and calls `entry.async_start_reauth`.
- Push updates use `coordinator.async_push_unit_update()` →
  `async_update_listeners()`, **not** `async_set_updated_data()`. The latter
  reschedules the update timer, so a steady push stream would keep shoving the
  5-minute reconciliation poll into the future and the drift safety net would
  never run.
- Entities only re-write state for the unit a push actually concerned
  (`last_pushed_unit_id`). Without that guard one message re-renders all N
  climate entities — 30x amplification on a 30-room site.
- `available` must stay inherited from `CoordinatorEntity`. It used to be
  hardcoded `True`, which meant a failing coordinator left entities presenting
  stale values as live; that is what made the 2026-07-22 CoolAutomation outage
  invisible (27 entities frozen at the same millisecond, nothing flagged).
  `algorithms-hass`'s `climate/online.yaml.jinja` keys its connectivity icon
  off `states('climate.…') == 'unavailable'` and depends on this being honest.
- `CoolAutomationClient` is a **process-wide singleton** (`utils/singleton.py`)
  whose `create()` reassigns `self.token`. Two config entries would share one
  token and one WS connection. Setup logs a warning; it is not fixable from
  here. For the same reason `async_unload_entry` must **not** close
  `client.api_client` — the next setup gets the same instance back.
- The library attaches its own `StreamHandler(sys.stdout)` at import and pins
  its level to WARNING while leaving propagation on, so every record lands
  twice and cannot be silenced from `configuration.yaml`.
  `_quiet_library_stdout_logging()` undoes that at setup.
- Units added or removed mid-session won't surface as entities until
  the entry is reloaded. The WS pump builds `units_by_id` once at
  setup; the reconciliation poll sees state changes but doesn't add
  new entities.
- `HVACUnit._update_unit` is a leading-underscore "private" method on
  the library, called from this integration. If the library refactors
  the message-apply API in future, this call site needs updating.
- The `with_callback` parameter on `_update_unit` was removed in
  `cool-open-client 0.0.21`. Callers must not pass it.

## Past initiatives

- **Step 1** (0.0.15) — Replaced per-unit polling with one bulk call per cycle. ~99% traffic drop. Spec: `docs/superpowers/specs/2026-05-20-cool-open-api-traffic-reduction-design.md`.
- **Step 1.5** (0.0.16) — Threaded HA's pre-built SSL context through the library to fix `Detected blocking call to load_default_certs` warnings.
- **Step 2** (0.0.17) — Added WebSocket-driven push updates. `iot_class: cloud_push`. Spec: `docs/superpowers/specs/2026-05-20-cool-open-websocket-push-design.md`.
