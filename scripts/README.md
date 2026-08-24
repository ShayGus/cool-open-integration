# scripts/

Manual, live-API tools for `cool-open-integration`. None of these are part of
`pytest` — they need real CoolAutomation credentials and network access.

## live_ws_check.py

Proves against a real account that CoolAutomation's WebSocket push actually
delivers data. The unit suite covers the wiring; this covers the thing only a
live account can show — that `UPDATE_UNIT` frames arrive at all. Written
2026-08-24, when the WS endpoint was found to be pointing at the wrong host
(see the repo `CLAUDE.md`), so nothing had ever been received over that socket
in production.

**Read-only.** It authenticates, lists units and listens. It never issues a
control command, so it cannot change a room's temperature.

Credentials come from the environment or from `Controlá/.env` (outside every
git repo) and are never printed:

```
COOLAUTOMATION_TOKEN=...        # preferred — avoids minting a new token
# or
COOLAUTOMATION_USER=...
COOLAUTOMATION_PASS=...
```

```bash
python3 scripts/live_ws_check.py                 # 120s through the real code path
python3 scripts/live_ws_check.py --seconds 300   # quiet site? listen longer
python3 scripts/live_ws_check.py --raw           # every frame, incl. those the
                                                 # library filters out
python3 scripts/live_ws_check.py --no-override   # reproduce the 404 for the record
```

Exit codes: `0` push confirmed, `1` rejected/reconnect loop, `2` inconclusive
(socket held, nothing pushed — HVAC state may simply not have changed).

`--raw` is the diagnostic mode: `subscribe_unit_updates()` forwards only
`UPDATE_UNIT` and silently drops everything else, including the
`{"type":"error"}` frame the server sends for a bad token. Use `--raw` whenever
the library-level run says nothing is arriving and you need to know why.

## compare_installations.py

Compares the integration across two Home Assistant installations and says, with
evidence, whether **push is actually working on each one** or whether the
5-minute reconciliation poll is carrying the entities on its own. Built as an
A/B while validating the WS endpoint fix: Demo2 on the patched build against
Sercotel still on the released one.

The tell is cadence. With push, units arrive independently, seconds apart. With
the poll alone, nothing moves between one 5-minute tick and the next. The script
samples `last_updated` on both installations over the *same* window and compares
the gaps, alongside the integration version, config entry state, entity counts,
and any `cool_open_client` entries in the system log.

**Read-only.** It never calls a service and never writes to either HA. Reads
`HA_<NAME>_URL` / `HA_<NAME>_TOKEN` from `Controlá/.env`, and reuses the
`.env`/WebSocket helpers from `Controlá/scripts/ha_ws_call.py` (so it needs
`websocket-client`).

```bash
python3 scripts/compare_installations.py                      # DEMO2 vs SERCOTEL, 8 min
python3 scripts/compare_installations.py --no-sample          # inventory only, instant
python3 scripts/compare_installations.py --seconds 600
python3 scripts/compare_installations.py --installations DEMO2 SERCOTEL
```

Verdicts per installation: `WS ROTO` (a `cool_open_client` reconnect loop in the
system log — the released build's 404), `PUSH ACTIVO` (changes arriving well
inside the 5-minute poll window), `SOLO POLL`, or `INCONCLUSO` (too few changes
in the window — lengthen `--seconds`).

Keep `--seconds` above ~360: an installation without push only changes state on
the 5-minute tick, so a short window makes a healthy poll-only site look
inconclusive rather than poll-only.

Two gotchas it already handles, both learned the hard way: Cloudflare fronts
`*.controla.cloud` and rejects urllib's User-Agent with `403 error code: 1010`,
and `system_log` keeps only the first line of a record — the
`WSServerHandshakeError` itself is in the traceback you cannot see, so the loop
is detected from its summary line instead.
