"""Shared pytest fixtures for the cool_open_integration tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Auto-enable custom_components for every test in this directory.

    `enable_custom_integrations` is a fixture supplied by
    `pytest-homeassistant-custom-component`. Wrapping it in an autouse
    fixture lets every test pick it up without listing it explicitly.
    """
    yield
