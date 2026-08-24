"""A fake CoolAutomation cloud, for driving the integration end to end.

CoolAutomation has no sandbox, and the only real account available belongs to
a live hotel with guests in it. This stub speaks enough of the protocol to run
the integration for real — REST over `/api/v2` plus the WebSocket at `/ws/v2`
— so the failure modes that matter can be provoked on demand instead of waited
for: a rejected token, a mid-session drop, a reconnect storm, a poll that
starts failing.

The protocol was characterised from the library's generated models and from
the official web SDK bundle (`control.coolremote.net`), then confirmed against
the real API on 2026-08-24: the handshake, the `authenticate` frame, the
`{"type":"error"}` rejection, ping/pong, and the `UPDATE_UNIT` shape.

Everything is plain HTTP/WS on loopback — no TLS, so no certificate games.
"""
from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import WSMsgType, web

VALID_TOKEN = "stub-token-valid"

# Numeric id -> label, mirroring `/services/types`. The client inverts these to
# translate the numeric ids that arrive over the socket.
OPERATION_STATUSES = {"1": "on", "2": "off"}
OPERATION_MODES = {"0": "COOL", "1": "HEAT", "2": "AUTO", "3": "DRY", "4": "FAN"}
FAN_MODES = {"0": "LOW", "1": "MEDIUM", "2": "HIGH", "3": "AUTO", "4": "TOP"}
SWING_MODES = {"0": "vertical", "1": "30", "2": "horizontal", "6": "auto"}
TEMPERATURE_SCALE = {"1": "celsius", "2": "fahrenheit"}


def _unit_payload(unit_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "id": unit_id,
        "name": name,
        "type": 1,
        "isConnected": True,
        "activeOperationStatus": 1,
        "activeOperationMode": 0,
        "activeFanMode": 1,
        "activeSwingMode": 6,
        "activeSetpoint": 24,
        "ambientTemperature": 25,
        "filter": False,
        "isHalfCDegreeEnabled": False,
        "supportedOperationStatuses": [1, 2],
        "supportedOperationModes": [0, 1, 2],
        "supportedFanModes": [0, 1, 2, 3],
        "supportedSwingModes": [0, 1, 2, 6],
        "temperatureLimits": {"0": [16, 32], "1": [16, 32]},
    }
    payload.update(overrides)
    return payload


class StubCoolAutomation:
    """A controllable fake of the CoolAutomation cloud.

    Test bodies drive it through the `fail_*` / `reject_token` flags and the
    `push_update` / `drop_socket` helpers.
    """

    def __init__(self, units: dict[str, str] | None = None) -> None:
        self.units: dict[str, dict[str, Any]] = {
            unit_id: _unit_payload(unit_id, name)
            for unit_id, name in (units or {"unit-A": "Room 101", "unit-B": "Room 102"}).items()
        }
        # Knobs the tests turn.
        self.reject_token = False
        self.fail_units_request = False
        self.units_request_count = 0
        self.ws_connection_count = 0
        self.authenticated_tokens: list[str] = []

        self._sockets: list[web.WebSocketResponse] = []
        self._runner: web.AppRunner | None = None
        self.base_url = ""
        self.ws_url = ""

    # --- lifecycle ------------------------------------------------------
    async def start(self) -> "StubCoolAutomation":
        app = web.Application()
        app.router.add_get("/api/v2/services/types", self._types)
        app.router.add_get("/api/v2/units", self._units)
        app.router.add_get("/api/v2/users/me", self._me)
        app.router.add_get("/ws/v2", self._websocket)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}/api/v2"
        self.ws_url = f"http://127.0.0.1:{port}/ws/v2"
        return self

    async def stop(self) -> None:
        for socket in list(self._sockets):
            await socket.close()
        if self._runner is not None:
            await self._runner.cleanup()

    # --- REST -----------------------------------------------------------
    async def _types(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "data": {
                    "temperatureScale": TEMPERATURE_SCALE,
                    "operationStatuses": OPERATION_STATUSES,
                    "operationModes": OPERATION_MODES,
                    "fanModes": FAN_MODES,
                    "swingModes": SWING_MODES,
                }
            }
        )

    async def _units(self, request: web.Request) -> web.Response:
        self.units_request_count += 1
        if self.fail_units_request:
            return web.json_response(
                {"success": False, "message": "Internal error"}, status=500
            )
        return web.json_response({"data": dict(self.units)})

    async def _me(self, request: web.Request) -> web.Response:
        return web.json_response({"data": {"id": "stub-user"}})

    # --- WebSocket ------------------------------------------------------
    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(heartbeat=None)
        await socket.prepare(request)
        self.ws_connection_count += 1

        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                continue
            payload = message.json()
            if payload.get("type") != "authenticate":
                continue
            token = (payload.get("content") or {}).get("token")
            self.authenticated_tokens.append(token)
            if self.reject_token or token != VALID_TOKEN:
                # Exactly what the real server does: an error frame, then it
                # hangs up. No close code, no exception on the client side.
                await socket.send_json(
                    {"type": "error", "payload": {"error": "Authentication failed"}}
                )
                await socket.close()
                return socket
            self._sockets.append(socket)
            break

        # Authenticated: hold the socket open until the test drops it.
        async for message in socket:
            if message.type is WSMsgType.TEXT and message.json().get("type") == "pong":
                continue
        if socket in self._sockets:
            self._sockets.remove(socket)
        return socket

    # --- knobs ----------------------------------------------------------
    async def push_update(self, unit_id: str, **changes: Any) -> None:
        """Mutate a unit and push it to every live socket, as the cloud does."""
        self.units[unit_id].update(changes)
        unit = self.units[unit_id]
        frame = {
            "name": "UPDATE_UNIT",
            "data": {
                "unitId": unit_id,
                "operationStatus": unit["activeOperationStatus"],
                "operationMode": unit["activeOperationMode"],
                "fan": unit["activeFanMode"],
                "swing": unit["activeSwingMode"],
                "setpoint": unit["activeSetpoint"],
                "ambientTemperature": unit["ambientTemperature"],
                "filter": unit["filter"],
            },
        }
        for socket in list(self._sockets):
            await socket.send_json(frame)
        await asyncio.sleep(0)

    async def drop_socket(self) -> None:
        """Hang up on the client without warning, as a flaky link would."""
        for socket in list(self._sockets):
            await socket.close()
            if socket in self._sockets:
                self._sockets.remove(socket)


def point_client_at(stub: StubCoolAutomation) -> None:
    """Redirect both the REST base path and the WS URL at the stub."""
    from cool_open_client.client.configuration import Configuration
    from cool_open_client.cool_automation_client import CoolAutomationClient

    Configuration.set_default(Configuration(host=stub.base_url))
    CoolAutomationClient.SOCKET_URI = stub.ws_url


def reset_client_singleton() -> None:
    """Forget the process-wide client between tests.

    `CoolAutomationClient` subclasses the library's `Singleton`, so without
    this a later test reuses the previous test's client — still holding an
    `ApiClient` bound to a port that no longer exists.
    """
    from cool_open_client.utils.singleton import SingletonMeta

    SingletonMeta._instances.clear()


async def close_client_session() -> None:
    """Close the singleton client's HTTP session.

    The integration deliberately leaves this open (the singleton hands the same
    session to the next setup), but a test process has to close it or aiohttp
    reports an unclosed session and connector.
    """
    from cool_open_client.cool_automation_client import CoolAutomationClient
    from cool_open_client.utils.singleton import SingletonMeta

    instance = SingletonMeta._instances.get(CoolAutomationClient)
    if instance is not None and getattr(instance, "api_client", None) is not None:
        await instance.api_client.close()
