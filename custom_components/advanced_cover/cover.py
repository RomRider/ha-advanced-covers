"""Cover platform for the Advanced Cover integration."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
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
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.device_registry import (
    DeviceEntryType,
    DeviceInfo,
    async_get as async_get_device_registry,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    ATTR_ENFORCE,
    ATTR_MOVE_IN_PROGRESS,
    ATTR_SIMULATED_POSITION,
    ATTR_VALUE,
    CONF_CLOSE_DURATION,
    CONF_ENFORCE_BOUNDS,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_OPEN_DURATION,
    CONF_SKIP_STOP_AT_LIMITS,
    CONF_TREAT_MIN_AS_CLOSED,
    CONF_WRAPPED_ENTITY,
    DEFAULT_CLOSE_DURATION,
    DEFAULT_ENFORCE_BOUNDS,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_OPEN_DURATION,
    DEFAULT_SKIP_STOP_AT_LIMITS,
    DEFAULT_TREAT_MIN_AS_CLOSED,
    DOMAIN,
    SERVICE_SET_ENFORCE_BOUNDS,
    SERVICE_SET_MAX_VALUE,
    SERVICE_SET_MIN_VALUE,
)

_LOGGER = logging.getLogger(__name__)

_SIM_TICK_INTERVAL = timedelta(seconds=0.5)


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


def _resolve_wrapped_area_name(hass: HomeAssistant, wrapped_entity_id: str) -> str | None:
    """Return the wrapped entity's area name, falling back to its device's area.

    Used as a one-time suggestion for the Advanced Cover's own device, so it
    lands in the same area as the cover it wraps by default.
    """

    entity_entry = er.async_get(hass).async_get(wrapped_entity_id)
    if entity_entry is None:
        return None

    area_id = entity_entry.area_id
    if area_id is None and entity_entry.device_id is not None:
        device_entry = async_get_device_registry(
            hass).async_get(entity_entry.device_id)
        area_id = device_entry.area_id if device_entry is not None else None

    if area_id is None:
        return None

    area_entry = ar.async_get(hass).async_get_area(area_id)
    return area_entry.name if area_entry is not None else None


def _resolve_wrapped_via_device(
    hass: HomeAssistant, wrapped_entity_id: str
) -> tuple[str, str] | None:
    """Return a device identifier for the wrapped entity's device, if any.

    Links this entity's own device to the wrapped entity's device via
    `via_device`, so the device page shows a "Connected via" cross-link.
    """

    entity_entry = er.async_get(hass).async_get(wrapped_entity_id)
    if entity_entry is None or entity_entry.device_id is None:
        return None

    device_entry = async_get_device_registry(
        hass).async_get(entity_entry.device_id)
    if device_entry is None or not device_entry.identifiers:
        return None

    return next(iter(device_entry.identifiers))


class AdvancedCoverEntity(CoverEntity, RestoreEntity):
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
        # Floor at 0.1s defensively: the config flow's NumberSelector(min=1)
        # keeps this away from 0 through normal UX, but a 0-second duration
        # would divide-by-zero in the tick math below.
        self._open_duration = max(
            float(config.get(CONF_OPEN_DURATION, DEFAULT_OPEN_DURATION)), 0.1
        )
        self._close_duration = max(
            float(config.get(CONF_CLOSE_DURATION, DEFAULT_CLOSE_DURATION)), 0.1
        )
        self._skip_stop_at_limits = bool(
            config.get(CONF_SKIP_STOP_AT_LIMITS, DEFAULT_SKIP_STOP_AT_LIMITS)
        )
        self._treat_min_as_closed = bool(
            config.get(CONF_TREAT_MIN_AS_CLOSED, DEFAULT_TREAT_MIN_AS_CLOSED)
        )

        # The wrapped entity's REAL supported-features bitmask, tracked
        # separately from self._attr_supported_features (which gets a
        # synthetic SET_POSITION OR'd in while simulating).
        self._wrapped_supported_features = CoverEntityFeature(0)
        self._warned_missing_stop = False

        # Simulated-move state (only meaningful while _simulation_enabled()).
        self._sim_position: float = 0.0
        self._sim_target: float | None = None
        self._sim_direction: str | None = None  # "opening" | "closing" | None
        self._sim_move_start_position: float | None = None
        self._sim_move_start_time: float | None = None  # time.monotonic()
        self._sim_cancel_tick: CALLBACK_TYPE | None = None
        self._sim_cancel_finalize: CALLBACK_TYPE | None = None

        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            model="Advanced Cover",
            suggested_area=_resolve_wrapped_area_name(
                hass, self._wrapped_entity_id),
            via_device=_resolve_wrapped_via_device(
                hass, self._wrapped_entity_id),
        )
        self._apply_wrapped_state()

    async def async_added_to_hass(self) -> None:
        """Run when entity is about to be added to hass."""

        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            restored = last_state.attributes.get(ATTR_CURRENT_POSITION)
            if isinstance(restored, (int, float)):
                self._sim_position = float(restored)
        # else: no prior state at all -> stays at the __init__ default of 0.0
        # (closed).

        if self._simulation_enabled():
            self._attr_current_cover_position = round(self._sim_position)
            self._attr_is_closed = self._sim_is_closed()
            await self._maybe_enforce_bounds()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._wrapped_entity_id],
                self._handle_wrapped_state_change,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any in-progress simulated move when the entity is removed."""

        if self._sim_move_active():
            self._sim_position = self._estimate_position()
            self._attr_current_cover_position = round(self._sim_position)
        self._cancel_sim_timers()
        await super().async_will_remove_from_hass()

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
            if self._sim_move_active():
                self._freeze_sim_move()
            self._attr_available = False
            return

        self._attr_available = True
        self._attr_device_class = state.attributes.get(ATTR_DEVICE_CLASS)

        self._wrapped_supported_features = CoverEntityFeature(
            state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        )
        simulating = self._simulation_enabled()

        if (
            not simulating
            and CoverEntityFeature.SET_POSITION not in self._wrapped_supported_features
            and CoverEntityFeature.STOP not in self._wrapped_supported_features
            and not self._warned_missing_stop
        ):
            _LOGGER.warning(
                "%s: %s reports neither SET_POSITION nor STOP support; "
                "simulated positioning cannot activate for it",
                self.entity_id,
                self._wrapped_entity_id,
            )
            self._warned_missing_stop = True

        self._attr_supported_features = self._wrapped_supported_features
        if simulating:
            self._attr_supported_features |= CoverEntityFeature.SET_POSITION

        self._attr_current_cover_tilt_position = state.attributes.get(
            ATTR_CURRENT_TILT_POSITION
        )

        if simulating:
            # The wrapped entity's real state is ignored as a position/motion
            # source while simulating (it can't reliably distinguish "fully
            # open" from "partially open") — everything below is owned by
            # our own move-tracking instead, unless a move is in flight, in
            # which case _sim_tick/_sim_finalize already own these fields.
            if not self._sim_move_active():
                self._attr_current_cover_position = round(self._sim_position)
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = self._sim_is_closed()
            return

        self._attr_current_cover_position = state.attributes.get(
            ATTR_CURRENT_POSITION
        )
        self._attr_is_opening = state.state == STATE_OPENING
        self._attr_is_closing = state.state == STATE_CLOSING

        if (
            self._treat_min_as_closed
            and self._attr_current_cover_position is not None
            and self._attr_current_cover_position <= self._effective_min()
        ):
            self._attr_is_closed = True
        elif state.state == STATE_CLOSED:
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

    def _sim_is_closed(self) -> bool:
        """Whether the current simulated position should report as closed.

        Normally only an exact 0. When `treat_min_as_closed` is enabled, any
        position at or below the configured lower bound also counts.
        """

        if self._treat_min_as_closed:
            return self._sim_position <= self._effective_min()
        return self._sim_position <= 0

    def _is_at_position(self, target: float) -> bool:
        return (
            self._attr_current_cover_position is not None
            and self._attr_current_cover_position == target
        )

    def _simulation_enabled(self) -> bool:
        """Whether simulated absolute positioning should govern this entity.

        Automatic, not a user setting: it activates whenever the wrapped
        cover can't report a real position but can be stopped mid-travel.
        """

        if CoverEntityFeature.SET_POSITION in self._wrapped_supported_features:
            return False  # real positioning always wins
        if CoverEntityFeature.STOP not in self._wrapped_supported_features:
            return False  # simulation requires being able to stop mid-travel
        return True

    def _sim_move_active(self) -> bool:
        return self._sim_target is not None

    def _cancel_sim_timers(self) -> None:
        if self._sim_cancel_tick is not None:
            self._sim_cancel_tick()
            self._sim_cancel_tick = None
        if self._sim_cancel_finalize is not None:
            self._sim_cancel_finalize()
            self._sim_cancel_finalize = None

    def _estimate_position(self) -> float:
        """Live estimated position (0-100) of an in-progress simulated move."""

        if self._sim_target is None or self._sim_move_start_time is None:
            return self._sim_position

        duration = (
            self._open_duration
            if self._sim_direction == "opening"
            else self._close_duration
        )
        rate = 100.0 / duration
        elapsed = max(0.0, time.monotonic() - self._sim_move_start_time)
        signed_delta = elapsed * rate * \
            (1 if self._sim_direction == "opening" else -1)
        estimated = self._sim_move_start_position + signed_delta
        lo, hi = sorted((self._sim_move_start_position, self._sim_target))
        return min(max(estimated, lo), hi)

    def _freeze_sim_move(self) -> None:
        """Cancel an in-progress move and lock in the live estimate.

        Does not call stop_cover on the wrapped entity - used when it's no
        longer safe to assume the wrapped entity will respond (e.g. it just
        became unavailable).
        """

        if self._sim_move_active():
            self._sim_position = self._estimate_position()
        self._cancel_sim_timers()
        self._sim_target = None
        self._sim_direction = None
        self._sim_move_start_position = None
        self._sim_move_start_time = None
        self._attr_current_cover_position = round(self._sim_position)
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = self._sim_is_closed()

    @callback
    def _sim_tick(self, _now: datetime | None = None) -> None:
        """Periodic position update while a simulated move is in progress."""

        self._sim_position = self._estimate_position()
        self._attr_current_cover_position = round(self._sim_position)
        self.async_write_ha_state()

    async def _sim_finalize(self, _now: datetime | None = None) -> None:
        """Called when a simulated move should have reached its target."""

        target = self._sim_target
        self._cancel_sim_timers()
        if target is not None:
            self._sim_position = target
        self._sim_target = None
        self._sim_direction = None
        self._sim_move_start_position = None
        self._sim_move_start_time = None
        self._attr_current_cover_position = round(self._sim_position)
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = self._sim_is_closed()
        self.async_write_ha_state()

        # A move that finished exactly at 0 or 100 ran the wrapped cover all
        # the way to its physical travel limit, not to a bounds-clamped
        # midpoint - if it has its own hardware endstop, skip our own stop
        # command and let the cover stop itself.
        at_hw_limit = target is not None and (target <= 0 or target >= 100)
        if not (self._skip_stop_at_limits and at_hw_limit):
            await self._async_call_wrapped(SERVICE_STOP_COVER)

        await self._maybe_enforce_bounds()

    async def _async_start_sim_move(self, target: float) -> None:
        """Begin, or retarget, a simulated move toward `target` (0-100)."""

        target = min(max(target, 0.0), 100.0)
        was_active = self._sim_move_active()
        current = self._estimate_position() if was_active else self._sim_position

        if current == target and not was_active:
            return

        if current == target:
            await self._sim_finalize()
            return

        new_direction = "opening" if target > current else "closing"
        same_direction_retarget = was_active and self._sim_direction == new_direction
        reversed_retarget = was_active and self._sim_direction != new_direction

        self._cancel_sim_timers()

        if reversed_retarget:
            # Reversing direction: stop the wrapped cover first rather than
            # trusting that issuing the opposite open/close command alone
            # will safely stop-then-reverse the motor.
            await self._async_call_wrapped(SERVICE_STOP_COVER)

        if not same_direction_retarget:
            service = (
                SERVICE_OPEN_COVER
                if new_direction == "opening"
                else SERVICE_CLOSE_COVER
            )
            await self._async_call_wrapped(service)
        # else: already moving the right way, only the timers need updating.

        self._sim_position = current
        self._sim_move_start_position = current
        self._sim_move_start_time = time.monotonic()
        self._sim_target = target
        self._sim_direction = new_direction
        self._attr_is_opening = new_direction == "opening"
        self._attr_is_closing = new_direction == "closing"
        self._attr_current_cover_position = round(current)
        self.async_write_ha_state()

        duration = (
            self._open_duration
            if new_direction == "opening"
            else self._close_duration
        )
        delay = abs(target - current) / 100 * duration

        self._sim_cancel_tick = async_track_time_interval(
            self.hass, self._sim_tick, _SIM_TICK_INTERVAL
        )
        self._sim_cancel_finalize = async_call_later(
            self.hass, delay, self._sim_finalize
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the wrapped entity, bounds, enforcement, and simulation settings."""

        simulating = self._simulation_enabled()
        attrs = {
            CONF_MIN_VALUE: self._effective_min(),
            CONF_MAX_VALUE: self._effective_max(),
            CONF_ENFORCE_BOUNDS: self._enforce_bounds,
            CONF_TREAT_MIN_AS_CLOSED: self._treat_min_as_closed,
            CONF_WRAPPED_ENTITY: self._wrapped_entity_id,
            ATTR_SIMULATED_POSITION: simulating,
        }
        if simulating:
            attrs[CONF_OPEN_DURATION] = self._open_duration
            attrs[CONF_CLOSE_DURATION] = self._close_duration
            attrs[CONF_SKIP_STOP_AT_LIMITS] = self._skip_stop_at_limits
            attrs[ATTR_MOVE_IN_PROGRESS] = self._sim_move_active()
        return attrs

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
        """Re-clamp the (real or simulated) position if it's out of bounds."""

        should_enforce = self._enforce_bounds if enforce is None else enforce

        if not should_enforce:
            return

        if self._simulation_enabled():
            if self._sim_move_active():
                # Never fight an in-flight move: it was already targeted at a
                # bounds-clamped position by whoever started it.
                return

            clamped = min(
                max(self._sim_position, self._effective_min()
                    ), self._effective_max()
            )

            if clamped == self._sim_position:
                return

            await self._async_start_sim_move(clamped)
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

        if self._simulation_enabled():
            await self._async_start_sim_move(max_value)
            return

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

        if self._simulation_enabled():
            await self._async_start_sim_move(min_value)
            return

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

        if self._simulation_enabled() and self._sim_move_active():
            self._freeze_sim_move()
            self.async_write_ha_state()

        await self._async_call_wrapped(SERVICE_STOP_COVER)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a position, clamped to the configured bounds."""

        position = kwargs[ATTR_POSITION]
        clamped = min(max(position, self._effective_min()),
                      self._effective_max())

        if self._simulation_enabled():
            await self._async_start_sim_move(clamped)
            return

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
