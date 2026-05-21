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
   matching `HVACUnit` and pushes via `async_set_updated_data`. Each
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

## Known constraints / non-obvious behaviour

- Token refresh during a live WS session is out of scope. On token
  expiry, the library backs off forever; the user must reload the
  config entry to trigger reauth.
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
