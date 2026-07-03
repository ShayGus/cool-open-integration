"""The CoolAutomation Cloud Open Integration integration."""
from __future__ import annotations

import asyncio
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.util.ssl import client_context
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er

from cool_open_client.hvac_units_factory import HVACUnitsFactory
from cool_open_client.cool_automation_client import (
    CoolAutomationClient,
    InvalidTokenException,
)
from cool_open_client.ws_events import Reconnected, UnitUpdate

from .const import DOMAIN, PLATFORMS
from .coordinator import CoolAutomationDataUpdateCoordinator

# TODO List the platforms that you want to support.
# For your initial PR, limit it to 1 platform.

_LOGGER = logging.getLogger(__name__)


async def _ws_pump(coordinator: "CoolAutomationDataUpdateCoordinator") -> None:
    """Forever-loop consumer of the library's WS event stream.

    Mutates in-memory `HVACUnit` instances per `UnitUpdate` and triggers a
    bulk reconcile on each `Reconnected`. Cancellation propagates so HA
    can stop us cleanly during entry unload.
    """
    client = coordinator.client
    units_by_id = {u.id: u for u in coordinator.units}

    try:
        async for event in client.subscribe_unit_updates():
            if isinstance(event, UnitUpdate):
                unit = units_by_id.get(event.message.unit_id)
                if unit is None:
                    continue
                unit._update_unit(event.message)
                coordinator.async_set_updated_data(coordinator.data)
            elif isinstance(event, Reconnected):
                await coordinator.async_request_refresh()
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception(
            "WS pump terminated unexpectedly; entry will rely on the "
            "5-minute reconciliation poll until reload",
        )


@callback
def _async_migrate_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry, units: list
) -> None:
    """Migrate climate entity unique_ids from unit name to stable unit id.

    Releases <= 0.0.18 keyed climate entities by ``unit.name``; 0.0.19 switched
    to ``unit.id`` without a migration, which orphaned users' customized
    entities (names/areas) and created fresh id-keyed ones. Re-key each original
    name-based registry entry onto ``unit.id`` to restore those customizations.

    An id-keyed entry already auto-created by 0.0.19/0.0.20 is removed first so
    the re-key does not collide. Units that share a ``name`` are skipped: the
    single name-based entry is ambiguous, so re-keying could attach the wrong
    unit's customizations. All lookups are scoped to this config entry, since
    ``async_get_entity_id`` matches globally across accounts.
    """
    registry = er.async_get(hass)

    name_counts: dict[str, int] = {}
    for unit in units:
        if unit.name:
            name_counts[unit.name] = name_counts.get(unit.name, 0) + 1

    for unit in units:
        old_unique_id = unit.name
        new_unique_id = unit.id
        if not old_unique_id or old_unique_id == new_unique_id:
            continue

        old_entity_id = registry.async_get_entity_id(
            Platform.CLIMATE, DOMAIN, old_unique_id
        )
        if old_entity_id is None:
            continue  # nothing keyed by name: already migrated or fresh install

        old_entry = registry.async_get(old_entity_id)
        if old_entry is None or old_entry.config_entry_id != entry.entry_id:
            continue  # belongs to a different config entry / account

        if name_counts.get(old_unique_id, 0) > 1:
            _LOGGER.warning(
                "Skipping unique_id migration for duplicate unit name %r; "
                "re-key would be ambiguous. Rename the units to disambiguate",
                old_unique_id,
            )
            continue

        new_entity_id = registry.async_get_entity_id(
            Platform.CLIMATE, DOMAIN, new_unique_id
        )
        if new_entity_id is not None and new_entity_id != old_entity_id:
            new_entry = registry.async_get(new_entity_id)
            if new_entry is None or new_entry.config_entry_id != entry.entry_id:
                _LOGGER.warning(
                    "Cannot migrate %r -> %s: target unique_id is already used by "
                    "another config entry; skipping",
                    old_unique_id,
                    new_unique_id,
                )
                continue
            _LOGGER.debug(
                "Removing auto-created climate entity %s so %r can migrate to %s",
                new_entity_id,
                old_unique_id,
                new_unique_id,
            )
            registry.async_remove(new_entity_id)

        _LOGGER.info(
            "Migrating climate unique_id %r -> %s", old_unique_id, new_unique_id
        )
        registry.async_update_entity(old_entity_id, new_unique_id=new_unique_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CoolAutomation Cloud Open Integration from a config entry."""

    # hass.data[DOMAIN] = config
    # conf: ConfigType | None = config.get(DOMAIN)

    # if conf is None:
    #     # If we have a config entry, setup is done by that config entry.
    #     # If there is no config entry, this should fail.
    #     return bool(hass.config_entries.async_entries(DOMAIN))

    _LOGGER.debug("async setup")
    # Build the SSL context off the event loop once, then thread it through
    # every cool-open-client call site so the library never blocks the loop
    # reading the system CA bundle.
    ssl_ctx = await hass.async_add_executor_job(client_context)
    token = entry.data["token"]
    try:
        client = await CoolAutomationClient.create(token=token, ssl_context=ssl_ctx)
    except OSError as error:
        raise ConfigEntryNotReady() from error
    except InvalidTokenException as error:
        _LOGGER.error("Invalid token, reauthenticating...")
        username = entry.data["username"]
        password = entry.data["password"]
        try:
            token = await CoolAutomationClient.authenticate(
                username, password, ssl_context=ssl_ctx
            )
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "token": token}
            )
            client = await CoolAutomationClient.create(token=token, ssl_context=ssl_ctx)
        except Exception as error:
            _LOGGER.error("Can't authenticate, wrong credentials: %s", error)
            raise ConfigEntryAuthFailed(
                "Authentication is no longer valid. Please reauthenticate"
            ) from error
    except Exception as error:
        _LOGGER.error("General Error: %s", error)
        raise ConfigEntryNotReady() from error
    try:
        units_factory = await HVACUnitsFactory.create(token=token, ssl_context=ssl_ctx)
        units = await units_factory.generate_units_from_api()
        if not units:
            raise ConfigEntryNotReady
    except OSError as error:
        raise ConfigEntryNotReady() from error
    except InvalidTokenException as error:
        _LOGGER.error("Invalid token")
        raise ConfigEntryAuthFailed(error) from error
    except Exception as error:
        _LOGGER.error("General Error: %s", error)
        raise ConfigEntryNotReady() from error

    coordinator = CoolAutomationDataUpdateCoordinator(hass, entry, client, units)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_create_background_task(
        hass,
        _ws_pump(coordinator),
        name=f"{DOMAIN}_ws_pump",
    )
    _async_migrate_unique_ids(hass, entry, units)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # hass.config_entries.async_setup_platforms(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
