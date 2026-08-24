"""Constants for the CoolAutomation Cloud Open Integration integration."""

from homeassistant.const import Platform


DOMAIN = "cool_open_integration"
TITLE = "Cool Automation Cloud Open Integration"
PLATFORMS = [Platform.CLIMATE]
TEMP_CELSIUS = "°C"
REFRESH_DELAY = 3.0
RECONCILE_INTERVAL_MINUTES = 5

# CoolAutomation serves REST and WebSocket from two *different* hosts. The
# official web SDK (control.coolremote.net) declares:
#
#     baseUrl -> https://api.coolremote.net/api/v2
#     wsUrl   -> wss://ws.coolremote.net/ws/v2
#
# cool-open-client hardcodes the REST host for both, and `api.coolremote.net`
# does not serve `/ws/v2` at all — the handshake gets a flat 404 and the
# library retries forever. Verified with a raw HTTP/1.1 upgrade:
#
#     wss://ws.coolremote.net/ws/v2  -> 101 Switching Protocols
#     wss://api.coolremote.net/ws/v2 -> 404 {"errorCode":"NOT_FOUND_2"}
#
# Applied as a class-attribute override in `async_setup_entry` until the fix
# lands upstream in cool-open-client; see CLAUDE.md.
WS_URL = "wss://ws.coolremote.net/ws/v2"

# A `Reconnected` with no `UnitUpdate` in between means the socket opened and
# then dropped without delivering data — the signature of the server rejecting
# our token right after the handshake. The library treats that clean close as
# a non-error, so its own backoff never kicks in; we throttle it from the pump
# instead (the async generator only advances when we ask for the next event).
WS_STALLED_RECONNECT_LIMIT = 10
WS_STALLED_BACKOFF_CAP_SECONDS = 60.0
