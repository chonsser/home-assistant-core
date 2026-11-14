"""Tests for the Tailcat diagnostics."""

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from homeassistant.core import HomeAssistant

from .conftest import ADDRESS, PAIRED_DEVICE, PAIRED_PUBLIC_KEY, SERVER_KEY


@pytest.mark.parametrize("subentries_data", [[PAIRED_DEVICE]])
async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Test diagnostics redact the address and keys."""
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert result == snapshot
    text = str(result)
    for secret in (ADDRESS, PAIRED_PUBLIC_KEY, SERVER_KEY["Private"]):
        assert secret not in text
