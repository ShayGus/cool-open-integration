"""Constants for the CoolAutomation Cloud Open Integration integration."""

from homeassistant.const import Platform


DOMAIN = "cool_open_integration"
TITLE = "Cool Automation Cloud Open Integration"
PLATFORMS = [Platform.CLIMATE]
TEMP_CELSIUS = "°C"
REFRESH_DELAY = 3.0
RECONCILE_INTERVAL_MINUTES = 5

# cool-open-client 0.0.22 uses the REST host for its WebSocket connection.
WS_URL = "wss://ws.coolremote.net/ws/v2"
