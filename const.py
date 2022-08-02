"""Constants for the CoolAutomation Cloud Open Integration integration."""

from homeassistant.const import Platform


DOMAIN = "cool_open_integration"
TITLE = "Cool Automation Cloud Open Integration"
PLATFORMS = [Platform.CLIMATE]
DEFAULT_SCAN_INTERVAL = 60
TEMP_CELSIUS: Final = "°C"