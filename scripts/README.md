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
COOLAUTOMATION_USERNAME=...
COOLAUTOMATION_PASSWORD=...
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
