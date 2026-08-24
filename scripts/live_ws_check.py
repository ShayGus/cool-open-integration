#!/usr/bin/env python3
"""Live, read-only check that CoolAutomation's WebSocket push actually works.

The unit tests prove the wiring; only a real account proves that the socket
delivers `UPDATE_UNIT` frames. This script authenticates, applies the same
endpoint override the integration applies, subscribes for a while and reports
what arrived.

**It never sends a control command.** It only reads: authenticate, list units,
listen. Nothing here can change a room's temperature.

Credentials come from the environment or from `Controlá/.env` (which is outside
every git repo). They are never printed or logged:

    COOLAUTOMATION_TOKEN=...                 # preferred, see --help
    # or
    COOLAUTOMATION_USER=...
    COOLAUTOMATION_PASS=...

Usage:
    python3 scripts/live_ws_check.py                 # 120s subscription
    python3 scripts/live_ws_check.py --seconds 300
    python3 scripts/live_ws_check.py --raw           # every frame, unfiltered
    python3 scripts/live_ws_check.py --no-override   # reproduce the 404
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

WS_URL_FALLBACK = "wss://ws.coolremote.net/ws/v2"


# Canonical names. `USER`/`PASS` are accepted for older .env files, but note
# that `USER` is *already* set by the shell to the unix account name: it has to
# be mapped into a dedicated variable rather than read from the environment,
# or we would happily send "truha" to CoolAutomation as a username.
_ALIASES = {
    "USER": "COOLAUTOMATION_USER",
    "PASS": "COOLAUTOMATION_PASS",
    "COOLAUTOMATION_USERNAME": "COOLAUTOMATION_USER",
    "COOLAUTOMATION_PASSWORD": "COOLAUTOMATION_PASS",
}


def _load_dotenv() -> None:
    """Pull credentials from a .env file. Earlier files win; the shell does not."""
    for candidate in (REPO_ROOT / ".env", REPO_ROOT.parent / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            target = _ALIASES.get(key)
            if target is None and key.startswith("COOLAUTOMATION_"):
                target = key
            if target is not None:
                os.environ.setdefault(target, value.strip().strip("'\""))


def _ws_url() -> str:
    try:
        from custom_components.cool_open_integration.const import WS_URL

        return WS_URL
    except Exception:
        return WS_URL_FALLBACK


async def _get_token(ssl_ctx) -> str:
    from cool_open_client.cool_automation_client import CoolAutomationClient

    token = os.environ.get("COOLAUTOMATION_TOKEN")
    if token:
        print("token      : reusing COOLAUTOMATION_TOKEN from the environment")
        return token

    username = os.environ.get("COOLAUTOMATION_USER")
    password = os.environ.get("COOLAUTOMATION_PASS")
    if not username or not password:
        sys.exit(
            "No credentials. Set COOLAUTOMATION_TOKEN, or "
            "COOLAUTOMATION_USER + COOLAUTOMATION_PASS, in .env"
        )

    print(f"auth       : POST /users/authenticate as {username[:2]}***")
    token = await CoolAutomationClient.authenticate(
        username, password, ssl_context=ssl_ctx
    )
    if not token or token == "Unauthorized":
        sys.exit("auth       : REJECTED — check the username/password")
    print(f"auth       : OK (token {len(token)} chars, not shown)")
    return token


async def run_raw(token: str, seconds: int, ssl_ctx) -> int:
    """Tap the socket directly, so frames the library filters out are visible."""
    import aiohttp

    url = _ws_url()
    print(f"\nraw tap    : {url} for {seconds}s")
    kinds: Counter[str] = Counter()
    deadline = time.monotonic() + seconds

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, ssl=ssl_ctx, heartbeat=30) as ws:
            print("handshake  : 101 Switching Protocols")
            await ws.send_json({"type": "authenticate", "content": {"token": token}})
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(
                        ws.receive(), timeout=max(1, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    break
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    kinds[f"<{msg.type.name}>"] += 1
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        print(f"closed     : {msg.type.name} {str(msg.data)[:120]}")
                        break
                    continue
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    kinds["<non-json>"] += 1
                    continue
                label = payload.get("name") or payload.get("type") or "<unlabelled>"
                kinds[label] += 1
                if label == "error":
                    print(f"REJECTED   : {msg.data[:200]}")
                    return 1
                if kinds[label] == 1:
                    print(f"  first {label}: {msg.data[:180]}")
                if payload.get("type") == "ping":
                    await ws.send_json({"type": "pong"})

    print("\nframes seen:")
    for label, count in kinds.most_common():
        print(f"  {count:6d}  {label}")
    return 0 if kinds.get("UPDATE_UNIT") else 2


async def run_library(token: str, seconds: int, ssl_ctx, override: bool) -> int:
    """Exercise the integration's real code path end to end."""
    from cool_open_client.cool_automation_client import CoolAutomationClient
    from cool_open_client.hvac_units_factory import HVACUnitsFactory
    from cool_open_client.ws_events import Reconnected, UnitUpdate

    if override:
        CoolAutomationClient.SOCKET_URI = _ws_url()
    print(f"ws url     : {CoolAutomationClient.SOCKET_URI}")

    client = await CoolAutomationClient.create(token=token, ssl_context=ssl_ctx)
    factory = await HVACUnitsFactory.create(token=token, ssl_context=ssl_ctx)
    units = await factory.generate_units_from_api()
    units_by_id = {u.id: u for u in units}
    print(f"rest       : {len(units)} controllable units")

    updates: Counter[str] = Counter()
    reconnects = 0
    deadline = time.monotonic() + seconds
    print(f"subscribe  : listening {seconds}s (read-only)\n")

    stream = client.subscribe_unit_updates()
    try:
        while time.monotonic() < deadline:
            try:
                event = await asyncio.wait_for(
                    stream.__anext__(), timeout=max(1, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                break
            except StopAsyncIteration:
                break

            if isinstance(event, UnitUpdate):
                unit_id = event.message.unit_id
                updates[unit_id] += 1
                if sum(updates.values()) <= 8:
                    unit = units_by_id.get(unit_id)
                    name = unit.name if unit else "<unknown unit>"
                    print(
                        f"  UPDATE_UNIT {name}: mode={event.message.operation_mode} "
                        f"status={event.message.operation_status} "
                        f"setpoint={event.message.setpoint} "
                        f"ambient={event.message.ambient_temperature}"
                    )
            elif isinstance(event, Reconnected):
                reconnects += 1
                print(f"  RECONNECTED (#{reconnects})")
    finally:
        await stream.aclose()

    total = sum(updates.values())
    print(
        f"\nresult     : {total} updates from {len(updates)} distinct units, "
        f"{reconnects} reconnects"
    )
    if reconnects and not total:
        print("verdict    : FAIL — reconnect loop with no data (token rejected?)")
        return 1
    if not total:
        print(
            "verdict    : INCONCLUSIVE — handshake held, but nothing was pushed.\n"
            "             HVAC state may simply not have changed. Re-run for longer,\n"
            "             or nudge a thermostat and watch for its unit id."
        )
        return 2
    print("verdict    : PASS — push updates are arriving")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="tap the socket directly; shows frames the library filters out",
    )
    parser.add_argument(
        "--no-override",
        action="store_true",
        help="leave the library's own SOCKET_URI alone (reproduces the 404)",
    )
    args = parser.parse_args()

    _load_dotenv()
    ssl_ctx = ssl.create_default_context()
    token = await _get_token(ssl_ctx)

    if args.raw:
        return await run_raw(token, args.seconds, ssl_ctx)
    return await run_library(token, args.seconds, ssl_ctx, not args.no_override)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
