"""Cover platform for the Advanced Cover integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_CLOSE_COVER_TILT,
    SERVICE_OPEN_COVER,
    SERVICE_OPEN_COVER_TILT,
    SERVICE_SET_COVER_POSITION,
    SERVICE_SET_COVER_TILT_POSITION,
    SERVICE_STOP_COVER,
    SERVICE_STOP_COVER_TILT,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OPEN,
    STATE_OPENING,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    ATTR_ENFORCE,
    ATTR_VALUE,
    CONF_ENFORCE_BOUNDS,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_WRAPPED_ENTITY,
    DEFAULT_ENFORCE_BOUNDS,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    SERVICE_SET_ENFORCE_BOUNDS,
    SERVICE_SET_MAX_VALUE,
    SERVICE_SET_MIN_VALUE,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Advanced Cover from a config entry."""

    async_add_entities([AdvancedCoverEntity(hass, entry)])

    value_schema = {
        vol.Required(ATTR_VALUE): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Optional(ATTR_ENFORCE): bool,
    }

    platform = async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_MIN_VALUE, value_schema, "async_set_min_value"
    )
    platform.async_register_entity_service(
        SERVICE_SET_MAX_VALUE, value_schema, "async_set_max_value"
    )
    platform.async_register_entity_service(
        SERVICE_SET_ENFORCE_BOUNDS,
        {vol.Required(ATTR_ENFORCE): bool},
        "async_set_enforce_bounds",
    )


class AdvancedCoverEntity(CoverEntity):
    """A cover that wraps another cover, clamping its usable position range."""

    _attr_should_poll = False
    _attr_available = False
    _attr_supported_features = CoverEntityFeature(0)
    _attr_current_cover_position: int | None = None
    _attr_current_cover_tilt_position: int | None = None
    _attr_is_closed: bool | None = None
    _attr_is_opening = False
    _attr_is_closing = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the entity."""

        self.hass = hass
        self._entry = entry
        self._wrapped_entity_id: str = entry.data[CONF_WRAPPED_ENTITY]

        config = {**entry.data, **entry.options}
        self._min_value = float(config.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE))
        self._max_value = float(config.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE))
        self._enforce_bounds = bool(
            config.get(CONF_ENFORCE_BOUNDS, DEFAULT_ENFORCE_BOUNDS)
        )

        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title
        self._apply_wrapped_state()

    async def async_added_to_hass(self) -> None:
        """Run when entity is about to be added to hass."""

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._wrapped_entity_id],
                self._handle_wrapped_state_change,
            )
        )

    @callback
    def _handle_wrapped_state_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        self._apply_wrapped_state()
        self.async_write_ha_state()
        self.hass.async_create_task(self._maybe_enforce_bounds())

    def _apply_wrapped_state(self) -> None:
        """Mirror the wrapped entity's reportable state and capabilities."""

        state = self.hass.states.get(self._wrapped_entity_id)

        if state is None or state.state == STATE_UNAVAILABLE:
            self._attr_available = False
            return

        self._attr_available = True
        self._attr_device_class = state.attributes.get(ATTR_DEVICE_CLASS)
        self._attr_supported_features = CoverEntityFeature(
            state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        )
        self._attr_current_cover_position = state.attributes.get(
            ATTR_CURRENT_POSITION
        )
        self._attr_current_cover_tilt_position = state.attributes.get(
            ATTR_CURRENT_TILT_POSITION
        )
        self._attr_is_opening = state.state == STATE_OPENING
        self._attr_is_closing = state.state == STATE_CLOSING

        if state.state == STATE_CLOSED:
            self._attr_is_closed = True
        elif state.state == STATE_OPEN:
            self._attr_is_closed = False
        elif self._attr_current_cover_position is not None:
            self._attr_is_closed = self._attr_current_cover_position == 0
        else:
            self._attr_is_closed = None

    def _effective_min(self) -> float:
        return self._min_value

    def _effective_max(self) -> float:
        return self._max_value

    def _is_at_position(self, target: float) -> bool:
        return (
            self._attr_current_cover_position is not None
            and self._attr_current_cover_position == target
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the current bounds and enforcement setting."""

        return {
            CONF_MIN_VALUE: self._effective_min(),
            CONF_MAX_VALUE: self._effective_max(),
            CONF_ENFORCE_BOUNDS: self._enforce_bounds,
            CONF_WRAPPED_ENTITY: self._wrapped_entity_id,
        }

    async def _async_call_wrapped(
        self, service: str, data: dict[str, Any] | None = None
    ) -> None:
        await self.hass.services.async_call(
            COVER_DOMAIN,
            service,
            {ATTR_ENTITY_ID: self._wrapped_entity_id, **(data or {})},
            blocking=True,
        )

    async def _maybe_enforce_bounds(self, enforce: bool | None = None) -> None:
        """Re-clamp the wrapped cover's position if it's currently out of bounds."""

        should_enforce = self._enforce_bounds if enforce is None else enforce

        if not should_enforce:
            return

        state = self.hass.states.get(self._wrapped_entity_id)

        if state is None or state.state in (STATE_OPENING, STATE_CLOSING):
            return

        position = state.attributes.get(ATTR_CURRENT_POSITION)

        if position is None:
            return

        clamped = min(max(position, self._effective_min()),
                      self._effective_max())

        if clamped == position:
            return

        await self._async_call_wrapped(
            SERVICE_SET_COVER_POSITION, {ATTR_POSITION: clamped}
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover, capped at the configured maximum."""

        max_value = self._effective_max()

        if max_value < 100 and CoverEntityFeature.SET_POSITION in (
            self._attr_supported_features
        ):
            if self._is_at_position(max_value):
                return

            await self._async_call_wrapped(
                SERVICE_SET_COVER_POSITION, {ATTR_POSITION: max_value}
            )
        else:
            if self._is_at_position(100):
                return

            await self._async_call_wrapped(SERVICE_OPEN_COVER)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover, capped at the configured minimum."""

        min_value = self._effective_min()

        if min_value > 0 and CoverEntityFeature.SET_POSITION in (
            self._attr_supported_features
        ):
            if self._is_at_position(min_value):
                return

            await self._async_call_wrapped(
                SERVICE_SET_COVER_POSITION, {ATTR_POSITION: min_value}
            )
        else:
            if self._is_at_position(0):
                return

            await self._async_call_wrapped(SERVICE_CLOSE_COVER)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""

        await self._async_call_wrapped(SERVICE_STOP_COVER)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a position, clamped to the configured bounds."""

        position = kwargs[ATTR_POSITION]
        clamped = min(max(position, self._effective_min()),
                      self._effective_max())

        if self._is_at_position(clamped):
            return

        await self._async_call_wrapped(
            SERVICE_SET_COVER_POSITION, {ATTR_POSITION: clamped}
        )

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the cover tilt (unclamped passthrough)."""

        await self._async_call_wrapped(SERVICE_OPEN_COVER_TILT)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the cover tilt (unclamped passthrough)."""

        await self._async_call_wrapped(SERVICE_CLOSE_COVER_TILT)

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        """Stop the cover tilt."""

        await self._async_call_wrapped(SERVICE_STOP_COVER_TILT)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the cover tilt to a position (unclamped passthrough)."""

        await self._async_call_wrapped(
            SERVICE_SET_COVER_TILT_POSITION,
            {ATTR_TILT_POSITION: kwargs[ATTR_TILT_POSITION]},
        )

    async def async_set_min_value(
        self, value: float, enforce: bool | None = None
    ) -> None:
        """Update the minimum position bound at runtime."""

        self._min_value = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_MIN_VALUE: value},
        )
        self.async_write_ha_state()
        await self._maybe_enforce_bounds(enforce)

    async def async_set_max_value(
        self, value: float, enforce: bool | None = None
    ) -> None:
        """Update the maximum position bound at runtime."""

        self._max_value = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_MAX_VALUE: value},
        )
        self.async_write_ha_state()
        await self._maybe_enforce_bounds(enforce)

    async def async_set_enforce_bounds(self, enforce: bool) -> None:
        """Update the proactive-enforcement setting at runtime."""

        self._enforce_bounds = enforce
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_ENFORCE_BOUNDS: enforce},
        )
        self.async_write_ha_state()
        await self._maybe_enforce_bounds()
