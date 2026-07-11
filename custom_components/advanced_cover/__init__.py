"""The Advanced Cover integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryHider

from .const import CONF_HIDE_WRAPPED_ENTITY, CONF_WRAPPED_ENTITY, DEFAULT_HIDE_WRAPPED_ENTITY

PLATFORMS = ["cover"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Advanced Cover from a config entry."""

    entry.async_on_unload(entry.add_update_listener(update_listener))
    _async_apply_wrapped_entity_hiding(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


@callback
def _async_apply_wrapped_entity_hiding(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Hide or unhide the wrapped entity to match the configured setting.

    Never overrides a manual (USER) hide in either direction.
    """

    registry = er.async_get(hass)
    wrapped_entity_id = entry.data[CONF_WRAPPED_ENTITY]
    entity_entry = registry.async_get(wrapped_entity_id)

    if entity_entry is None:
        return

    hide = {**entry.data, **entry.options}.get(
        CONF_HIDE_WRAPPED_ENTITY, DEFAULT_HIDE_WRAPPED_ENTITY
    )

    if hide and entity_entry.hidden_by is None:
        registry.async_update_entity(
            wrapped_entity_id, hidden_by=RegistryEntryHider.INTEGRATION
        )
    elif not hide and entity_entry.hidden_by == RegistryEntryHider.INTEGRATION:
        registry.async_update_entity(wrapped_entity_id, hidden_by=None)


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry update."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    registry = er.async_get(hass)
    wrapped_entity_id = entry.data[CONF_WRAPPED_ENTITY]
    entity_entry = registry.async_get(wrapped_entity_id)

    if entity_entry is not None and entity_entry.hidden_by == RegistryEntryHider.INTEGRATION:
        registry.async_update_entity(wrapped_entity_id, hidden_by=None)

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
