# Attributing HA memory growth to (or clearing) this integration

**Status:** open question. Written 2026-08-24 while fixing the WS 404 loop.

## Why this exists

`ha-sercotel` (VM 308, Sercotel Granada Suites) OOMed on 2026-08-21, peaking at
~3.1 GiB in a 4 GiB VM. `cool_open_integration` was flagged as a suspect because
it was visibly misbehaving at the time — `cool_open_client` was looping on a
WebSocket handshake `404` with 4s→60s backoff and had been since the post-OOM
restart. **Suspicion is not evidence, and reading the code did not turn the
suspicion into a case.** This document says what was ruled out, what was fixed
anyway, and how to actually settle it with measurements.

## What the code review found

### Ruled out as an OOM cause

The 404 reconnect loop itself. At the 60s backoff cap it runs ~1440 times a day;
it reuses a single `aiohttp.ClientSession` across iterations, and HA's
`system_log` ring is fixed-size and deduplicates by call site (which is why the
observed `count` climbed while the entry count did not). Noisy, but bounded.
Nothing here scales toward gigabytes.

### Fixed anyway (real, but small or latent)

- **Orphaned WS session per reload.** `subscribe_unit_updates()` opens a fresh
  `ClientSession` per call and closes it in the generator's `finally`. When the
  background task is cancelled at unload, running that `finally` was left to the
  event loop's asyncgen finalizer — not deterministic. Each reload could strand
  a session plus its `TCPConnector`. Now closed explicitly via `aclosing()`.
- **30x state-write amplification, which the WS fix would have switched on.**
  One push concerns one unit, but `async_set_updated_data()` notified every
  listener, so each message re-rendered all ~30 climate entities. It measured
  zero until now only because the socket never connected. Entities now filter on
  `coordinator.last_pushed_unit_id`.
- **The reconciliation poll could be starved.** `async_set_updated_data()`
  reschedules the update timer, so a steady push stream would have pushed the
  5-minute safety-net poll indefinitely into the future. Push now goes through
  `async_update_listeners()`, which leaves the schedule alone.
- **Unthrottled reconnect storm on a rejected token.** The server closes cleanly
  after refusing an `authenticate`, which the library does not treat as an
  error, so its backoff never engaged — it would reconnect as fast as the
  network allowed and fire a bulk HTTP refresh each round. `_ws_pump` now paces
  and eventually stops the stream.
- **Double logging.** The library's import-time `StreamHandler(sys.stdout)` plus
  propagation meant every record was emitted twice and could not be silenced
  from `configuration.yaml`. Disk, not RAM, but it distorts any log-volume
  reasoning. Removed at setup.

## How to settle it

Run in order and stop as soon as one step exonerates the integration.

**1 — Is the noise actually gone?** After deploying ≥ 0.0.22:

```bash
python3 scripts/ha_ws_call.py SERCOTEL system_log/list --quiet   # from Controlá/
```

`WSServerHandshakeError` for `cool_open_client` should stop accumulating
`count`. If it is still climbing, the override did not take — check for the
`Overriding cool-open-client WS endpoint` line at INFO on startup.

**2 — Is the growth even specific to this host?** Compare HA's RSS on
`ha-sercotel` against a hotel VM that does **not** run this integration, over
the same window. `cool_open_integration` is deployed only at Sercotel, so if
both hosts grow alike, it is not the cause. See `docs/access.md` in
`controla-infra` for how to reach each VM.

**3 — Only if 1 and 2 still point here.** Add HA's `profiler` integration and
take two samples several hours apart:

```
profiler.memory           # allocation snapshot
profiler.dump_log_objects # object-count growth by type
```

Growth concentrated in `aiohttp` / `ClientSession` / `TCPConnector` objects
would implicate this integration. Growth in recorder or state-machine objects
points elsewhere.

> **This is a write action on a production hotel.** Get Sergio's explicit
> confirmation before installing `profiler` or calling its services.

**4 — Watch the first hours after deploy specifically.** This release makes the
WebSocket connect *for the first time ever* on that host, so it introduces a
live traffic path that has never run at scale anywhere. Treat the post-deploy
window as new behaviour to observe, not as a regression test that has already
passed.

## Loose ends

- `controla-infra/docs/issues/ha-sercotel-oom-2026-08-21.md` is referenced by
  `Controlá/scripts/README.md` and by the project memory, but **does not exist**
  in the repo (`docs/issues/` was never created). Create it or fix both
  references.
- Sercotel has a stray `climate.l1_200_4055` entity — a raw CoolAutomation unit
  id that never got mapped to a room under the naming contract. Unrelated to
  memory, still unexplained.
