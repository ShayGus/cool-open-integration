"""Tests for ``_async_migrate_unique_ids`` (entity-registry re-key migration).

Releases <= 0.0.18 keyed climate entities by ``unit.name``; 0.0.19 switched to
``unit.id`` without migrating, orphaning users' customized entities. The
migration re-keys each original name-based registry entry onto ``unit.id`` so
customizations (name/area) survive, while carefully skipping ambiguous or
foreign-owned entries.

These tests drive a real entity registry (``er.async_get(hass)``) and seed it
with ``MockConfigEntry``-owned climate entities, then assert the observable
post-migration registry state through the public lookup API.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from homeassistant.helpers import area_registry as ar, entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cool_open_integration import _async_migrate_unique_ids
from custom_components.cool_open_integration.const import DOMAIN

CLIMATE = "climate"


def _unit(name, unit_id):
    """Stub for an HVAC unit: ``.name`` is the old unique_id, ``.id`` the new."""
    return SimpleNamespace(name=name, id=unit_id)


def _entry(hass):
    """A config entry for this integration, registered with hass."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    return entry


async def test_old_only_rekey_preserves_customizations(hass):
    """A lone name-keyed entity is re-keyed onto unit.id, keeping id/name/area."""
    registry = er.async_get(hass)
    entry = _entry(hass)
    area = ar.async_get(hass).async_create("Study Area")

    old = registry.async_get_or_create(
        CLIMATE, DOMAIN, "Study", config_entry=entry, suggested_object_id="study"
    )
    registry.async_update_entity(old.entity_id, name="Study A/C", area_id=area.id)

    _async_migrate_unique_ids(hass, entry, [_unit("Study", "unit-abc")])

    # Name-based key is gone; the id-based key now resolves.
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "Study") is None
    new_entity_id = registry.async_get_entity_id(CLIMATE, DOMAIN, "unit-abc")
    assert new_entity_id == old.entity_id  # same entity, re-keyed in place

    migrated = registry.async_get(new_entity_id)
    assert migrated.unique_id == "unit-abc"
    assert migrated.name == "Study A/C"  # customization preserved
    assert migrated.area_id == area.id


async def test_rekey_removes_autocreated_id_entity_same_entry(hass):
    """An already auto-created id-keyed orphan is removed so the customized
    name-keyed entity can migrate onto unit.id."""
    registry = er.async_get(hass)
    entry = _entry(hass)

    old = registry.async_get_or_create(
        CLIMATE, DOMAIN, "Study", config_entry=entry, suggested_object_id="study"
    )
    registry.async_update_entity(old.entity_id, name="Study A/C")
    # 0.0.19 auto-created a fresh id-keyed entity (no customization). Its
    # suggested object id collides with "study", so HA assigns "study_2".
    auto = registry.async_get_or_create(
        CLIMATE, DOMAIN, "unit-abc", config_entry=entry, suggested_object_id="study"
    )
    assert auto.entity_id != old.entity_id

    _async_migrate_unique_ids(hass, entry, [_unit("Study", "unit-abc")])

    # The auto-created orphan is removed.
    assert registry.async_get(auto.entity_id) is None
    # The ORIGINAL customized entity now owns unit.id and keeps its name.
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "unit-abc") == old.entity_id
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "Study") is None
    assert registry.async_get(old.entity_id).name == "Study A/C"


async def test_duplicate_unit_names_skipped(hass, caplog):
    """Two units sharing a name make the single name-keyed entry ambiguous, so
    no re-key happens for that name."""
    registry = er.async_get(hass)
    entry = _entry(hass)

    old = registry.async_get_or_create(
        CLIMATE, DOMAIN, "AC", config_entry=entry, suggested_object_id="ac"
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.cool_open_integration"):
        _async_migrate_unique_ids(
            hass, entry, [_unit("AC", "id-1"), _unit("AC", "id-2")]
        )

    # Untouched: still name-keyed, and no id-based entries were created.
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "AC") == old.entity_id
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "id-1") is None
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "id-2") is None
    # The ambiguous name is reported so the user can disambiguate the units.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AC" in r.getMessage() for r in warnings)


async def test_foreign_owned_old_entity_not_touched(hass):
    """A name-keyed entity owned by another config entry is left alone."""
    registry = er.async_get(hass)
    entry = _entry(hass)
    other = _entry(hass)

    foreign = registry.async_get_or_create(
        CLIMATE, DOMAIN, "Study", config_entry=other, suggested_object_id="study"
    )

    _async_migrate_unique_ids(hass, entry, [_unit("Study", "unit-abc")])

    # Foreign entity unchanged: still name-keyed, still owned by `other`.
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "Study") == foreign.entity_id
    assert registry.async_get(foreign.entity_id).config_entry_id == other.entry_id
    # No id-based entry was created for our entry.
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "unit-abc") is None


async def test_foreign_owned_target_id_skips_migration(hass, caplog):
    """When the target unit.id is already claimed by another config entry, the
    migration must skip: don't touch our old entity, don't remove the foreign one."""
    registry = er.async_get(hass)
    entry = _entry(hass)
    other = _entry(hass)

    old = registry.async_get_or_create(
        CLIMATE, DOMAIN, "Study", config_entry=entry, suggested_object_id="study"
    )
    registry.async_update_entity(old.entity_id, name="Study A/C")
    foreign_new = registry.async_get_or_create(
        CLIMATE, DOMAIN, "unit-abc", config_entry=other, suggested_object_id="other"
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.cool_open_integration"):
        _async_migrate_unique_ids(hass, entry, [_unit("Study", "unit-abc")])

    # Our old entity is left as-is (still name-keyed, name preserved).
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "Study") == old.entity_id
    assert registry.async_get(old.entity_id).name == "Study A/C"
    # The foreign id-keyed entity is NOT removed and stays with `other`.
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "unit-abc") == foreign_new.entity_id
    assert registry.async_get(foreign_new.entity_id).config_entry_id == other.entry_id
    # The collision is reported (target id owned by another config entry).
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unit-abc" in r.getMessage() for r in warnings)


async def test_fresh_install_creates_nothing(hass):
    """No pre-existing entities: the migration creates nothing."""
    registry = er.async_get(hass)
    entry = _entry(hass)

    _async_migrate_unique_ids(hass, entry, [_unit("Study", "unit-abc")])

    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "Study") is None
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "unit-abc") is None
    assert len(registry.entities) == 0


async def test_already_migrated_id_entity_untouched(hass):
    """When only the id-keyed entity exists (no name-keyed), leave it alone."""
    registry = er.async_get(hass)
    entry = _entry(hass)

    existing = registry.async_get_or_create(
        CLIMATE, DOMAIN, "unit-abc", config_entry=entry, suggested_object_id="study"
    )
    registry.async_update_entity(existing.entity_id, name="Study A/C")

    _async_migrate_unique_ids(hass, entry, [_unit("Study", "unit-abc")])

    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "Study") is None
    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "unit-abc") == existing.entity_id
    assert registry.async_get(existing.entity_id).name == "Study A/C"
    assert len(registry.entities) == 1


async def test_name_equals_id_skipped(hass):
    """unit.name == unit.id: nothing to migrate; the entity is untouched."""
    registry = er.async_get(hass)
    entry = _entry(hass)

    ent = registry.async_get_or_create(
        CLIMATE, DOMAIN, "same-id", config_entry=entry, suggested_object_id="x"
    )
    registry.async_update_entity(ent.entity_id, name="Custom")

    _async_migrate_unique_ids(hass, entry, [_unit("same-id", "same-id")])

    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "same-id") == ent.entity_id
    assert registry.async_get(ent.entity_id).name == "Custom"
    assert len(registry.entities) == 1


async def test_falsy_unit_name_skipped(hass):
    """A falsy unit.name yields no name-based lookup; an id-keyed entity that
    happens to exist for that unit is untouched (no re-key attempted)."""
    registry = er.async_get(hass)
    entry = _entry(hass)

    ent = registry.async_get_or_create(
        CLIMATE, DOMAIN, "unit-x", config_entry=entry, suggested_object_id="x"
    )
    registry.async_update_entity(ent.entity_id, name="Kept")

    _async_migrate_unique_ids(hass, entry, [_unit("", "unit-x")])

    assert registry.async_get_entity_id(CLIMATE, DOMAIN, "unit-x") == ent.entity_id
    assert registry.async_get(ent.entity_id).name == "Kept"
    assert len(registry.entities) == 1
