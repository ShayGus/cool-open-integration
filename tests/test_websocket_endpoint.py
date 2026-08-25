"""Regression coverage for the temporary cool-open-client endpoint override."""

from cool_open_client.cool_automation_client import CoolAutomationClient

from custom_components.cool_open_integration import _apply_ws_endpoint_override
from custom_components.cool_open_integration.const import WS_URL


def test_ws_endpoint_override_uses_websocket_host():
    original = CoolAutomationClient.SOCKET_URI
    try:
        CoolAutomationClient.SOCKET_URI = "wss://api.coolremote.net/ws/v2"

        _apply_ws_endpoint_override()

        assert CoolAutomationClient.SOCKET_URI == WS_URL
    finally:
        CoolAutomationClient.SOCKET_URI = original
