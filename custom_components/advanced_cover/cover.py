"""Cover platform for the Advanced Cover integration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
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
    ATTR_SIMULATED_TILT_POSITION,
    ATTR_TILT_MOVE_IN_PROGRESS,
    ATTR_VALUE,
    CONF_CLOSE_DURATION,
    CONF_CLOSE_TILT_DURATION,
    CONF_ENFORCE_BOUNDS,
    CONF_ENFORCE_TILT_BOUNDS,
    CONF_MAX_TILT_VALUE,
    CONF_MAX_VALUE,
    CONF_MIN_TILT_VALUE,
    CONF_MIN_VALUE,
    CONF_OPEN_DURATION,
    CONF_OPEN_TILT_DURATION,
    CONF_SKIP_STOP_AT_LIMITS,
    CONF_SKIP_STOP_AT_TILT_LIMITS,
    CONF_TREAT_MIN_AS_CLOSED,
    CONF_WRAPPED_ENTITY,
    DEFAULT_CLOSE_DURATION,
    DEFAULT_CLOSE_TILT_DURATION,
    DEFAULT_ENFORCE_BOUNDS,
    DEFAULT_ENFORCE_TILT_BOUNDS,
    DEFAULT_MAX_TILT_VALUE,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_TILT_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_OPEN_DURATION,
    DEFAULT_OPEN_TILT_DURATION,
    DEFAULT_SKIP_STOP_AT_LIMITS,
    DEFAULT_SKIP_STOP_AT_TILT_LIMITS,
    DEFAULT_TREAT_MIN_AS_CLOSED,
    DOMAIN,
    SERVICE_SET_ENFORCE_BOUNDS,
    SERVICE_SET_ENFORCE_TILT_BOUNDS,
    SERVICE_SET_MAX_TILT_VALUE,
    SERVICE_SET_MAX_VALUE,
    SERVICE_SET_MIN_TILT_VALUE,
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
        SERVICE_SET_MIN_VALUE, value_schema, "async_set_min_position"
    )
    platform.async_register_entity_service(
        SERVICE_SET_MAX_VALUE, value_schema, "async_set_max_position"
    )
    platform.async_register_entity_service(
        SERVICE_SET_ENFORCE_BOUNDS,
        {vol.Required(ATTR_ENFORCE): bool},
        "async_set_enforce_bounds",
    )
    platform.async_register_entity_service(
        SERVICE_SET_MIN_TILT_VALUE, value_schema, "async_set_min_tilt_position"
    )
    platform.async_register_entity_service(
        SERVICE_SET_MAX_TILT_VALUE, value_schema, "async_set_max_tilt_position"
    )
    platform.async_register_entity_service(
        SERVICE_SET_ENFORCE_TILT_BOUNDS,
        {vol.Required(ATTR_ENFORCE): bool},
        "async_set_enforce_tilt_bounds",
    )


@dataclass
class _SimAxis:
    """Mutable move state + fixed identity for one simulated axis.

    Shared by the position and tilt engines so the timer/rate-math/
    direction-reversal logic in `_sim_tick`/`_sim_finalize`/
    `_async_start_sim_move` etc. below isn't duplicated per axis.
    `drives_open_closing` is True only for the position axis: HA's
    `CoverEntity` has exactly one `is_opening`/`is_closing` pair for the
    whole entity's derived state, so a tilt-only simulated move must never
    touch it - otherwise a tilt move would falsely report the whole cover as
    opening/closing while only the slats move.
    """

    attr_name: str
    open_service: str
    close_service: str
    stop_service: str
    drives_open_closing: bool
    open_duration: float
    close_duration: float
    skip_stop_at_limits: bool
    position: float = 0.0
    target: float | None = None
    direction: str | None = None
    move_start_position: float | None = None
    move_start_time: float | None = None
    cancel_tick: CALLBACK_TYPE | None = None
    cancel_finalize: CALLBACK_TYPE | None = None


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
        self._min_tilt_value = float(
            config.get(CONF_MIN_TILT_VALUE, DEFAULT_MIN_TILT_VALUE)
        )
        self._max_tilt_value = float(
            config.get(CONF_MAX_TILT_VALUE, DEFAULT_MAX_TILT_VALUE)
        )
        self._enforce_tilt_bounds = bool(
            config.get(CONF_ENFORCE_TILT_BOUNDS, DEFAULT_ENFORCE_TILT_BOUNDS)
        )
        self._open_tilt_duration = max(
            float(config.get(CONF_OPEN_TILT_DURATION,
                  DEFAULT_OPEN_TILT_DURATION)), 0.1
        )
        self._close_tilt_duration = max(
            float(config.get(CONF_CLOSE_TILT_DURATION,
                  DEFAULT_CLOSE_TILT_DURATION)), 0.1
        )
        self._skip_stop_at_tilt_limits = bool(
            config.get(CONF_SKIP_STOP_AT_TILT_LIMITS,
                       DEFAULT_SKIP_STOP_AT_TILT_LIMITS)
        )

        # The wrapped entity's REAL supported-features bitmask, tracked
        # separately from self._attr_supported_features (which gets synthetic
        # SET_POSITION/SET_TILT_POSITION OR'd in while simulating).
        self._wrapped_supported_features = CoverEntityFeature(0)
        self._warned_missing_stop = False
        self._warned_missing_tilt_stop = False

        # Simulated-move state, one axis each (only meaningful while
        # _simulation_enabled()/_tilt_simulation_enabled() respectively).
        self._sim = _SimAxis(
            attr_name="current_cover_position",
            open_service=SERVICE_OPEN_COVER,
            close_service=SERVICE_CLOSE_COVER,
            stop_service=SERVICE_STOP_COVER,
            drives_open_closing=True,
            open_duration=self._open_duration,
            close_duration=self._close_duration,
            skip_stop_at_limits=self._skip_stop_at_limits,
        )
        self._sim_tilt = _SimAxis(
            attr_name="current_cover_tilt_position",
            open_service=SERVICE_OPEN_COVER_TILT,
            close_service=SERVICE_CLOSE_COVER_TILT,
            stop_service=SERVICE_STOP_COVER_TILT,
            drives_open_closing=False,
            open_duration=self._open_tilt_duration,
            close_duration=self._close_tilt_duration,
            skip_stop_at_limits=self._skip_stop_at_tilt_limits,
        )

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
                self._sim.position = float(restored)
            restored_tilt = last_state.attributes.get(ATTR_CURRENT_TILT_POSITION)
            if isinstance(restored_tilt, (int, float)):
                self._sim_tilt.position = float(restored_tilt)
        # else: no prior state at all -> stays at the __init__ default of 0.0
        # (closed) for both axes.

        if self._simulation_enabled():
            self._attr_current_cover_position = round(self._sim.position)
            self._attr_is_closed = self._sim_is_closed()
            await self._maybe_enforce_bounds()

        if self._tilt_simulation_enabled():
            self._attr_current_cover_tilt_position = round(self._sim_tilt.position)
            await self._maybe_enforce_tilt_bounds()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._wrapped_entity_id],
                self._handle_wrapped_state_change,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any in-progress simulated move when the entity is removed."""

        if self._sim_move_active(self._sim):
            self._sim.position = self._estimate_position(self._sim)
            self._attr_current_cover_position = round(self._sim.position)
        if self._sim_move_active(self._sim_tilt):
            self._sim_tilt.position = self._estimate_position(self._sim_tilt)
            self._attr_current_cover_tilt_position = round(self._sim_tilt.position)
        self._cancel_sim_timers(self._sim)
        self._cancel_sim_timers(self._sim_tilt)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_wrapped_state_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        self._apply_wrapped_state()
        self.async_write_ha_state()
        self.hass.async_create_task(self._maybe_enforce_bounds())
        self.hass.async_create_task(self._maybe_enforce_tilt_bounds())

    def _apply_wrapped_state(self) -> None:
        """Mirror the wrapped entity's reportable state and capabilities."""

        state = self.hass.states.get(self._wrapped_entity_id)

        if state is None or state.state == STATE_UNAVAILABLE:
            if self._sim_move_active(self._sim):
                self._freeze_sim_move(self._sim)
            if self._sim_move_active(self._sim_tilt):
                self._freeze_sim_move(self._sim_tilt)
            self._attr_available = False
            return

        self._attr_available = True
        self._attr_device_class = state.attributes.get(ATTR_DEVICE_CLASS)

        self._wrapped_supported_features = CoverEntityFeature(
            state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        )
        simulating = self._simulation_enabled()
        tilt_simulating = self._tilt_simulation_enabled()

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

        if (
            not tilt_simulating
            and CoverEntityFeature.SET_TILT_POSITION not in self._wrapped_supported_features
            and CoverEntityFeature.STOP_TILT not in self._wrapped_supported_features
            and (self._wrapped_supported_features & (
                CoverEntityFeature.OPEN_TILT | CoverEntityFeature.CLOSE_TILT
            ))
            and not self._warned_missing_tilt_stop
        ):
            _LOGGER.warning(
                "%s: %s reports neither SET_TILT_POSITION nor STOP_TILT support; "
                "simulated tilt positioning cannot activate for it",
                self.entity_id,
                self._wrapped_entity_id,
            )
            self._warned_missing_tilt_stop = True

        self._attr_supported_features = self._wrapped_supported_features
        if simulating:
            self._attr_supported_features |= CoverEntityFeature.SET_POSITION
        if tilt_simulating:
            self._attr_supported_features |= CoverEntityFeature.SET_TILT_POSITION

        if tilt_simulating:
            # The wrapped entity's real tilt state is ignored as a source
            # while simulating, same rationale as position below - unless a
            # move is in flight, in which case _sim_tick/_sim_finalize
            # already own this field.
            if not self._sim_move_active(self._sim_tilt):
                self._attr_current_cover_tilt_position = round(
                    self._sim_tilt.position)
        else:
            self._attr_current_cover_tilt_position = state.attributes.get(
                ATTR_CURRENT_TILT_POSITION
            )

        if simulating:
            # The wrapped entity's real state is ignored as a position/motion
            # source while simulating (it can't reliably distinguish "fully
            # open" from "partially open") — everything below is owned by
            # our own move-tracking instead, unless a move is in flight, in
            # which case _sim_tick/_sim_finalize already own these fields.
            if not self._sim_move_active(self._sim):
                self._attr_current_cover_position = round(self._sim.position)
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

    def _effective_min_tilt(self) -> float:
        return self._min_tilt_value

    def _effective_max_tilt(self) -> float:
        return self._max_tilt_value

    def _sim_is_closed(self) -> bool:
        """Whether the current simulated position should report as closed.

        Normally only an exact 0. When `treat_min_as_closed` is enabled, any
        position at or below the configured lower bound also counts.
        """

        if self._treat_min_as_closed:
            return self._sim.position <= self._effective_min()
        return self._sim.position <= 0

    def _is_at_position(self, target: float) -> bool:
        return (
            self._attr_current_cover_position is not None
            and self._attr_current_cover_position == target
        )

    def _is_at_tilt_position(self, target: float) -> bool:
        return (
            self._attr_current_cover_tilt_position is not None
            and self._attr_current_cover_tilt_position == target
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

    def _tilt_simulation_enabled(self) -> bool:
        """Whether simulated absolute tilt positioning should govern this entity.

        Tilt mirror of `_simulation_enabled`.
        """

        if CoverEntityFeature.SET_TILT_POSITION in self._wrapped_supported_features:
            return False
        if CoverEntityFeature.STOP_TILT not in self._wrapped_supported_features:
            return False
        return True

    def _sim_move_active(self, sim: _SimAxis) -> bool:
        return sim.target is not None

    def _cancel_sim_timers(self, sim: _SimAxis) -> None:
        if sim.cancel_tick is not None:
            sim.cancel_tick()
            sim.cancel_tick = None
        if sim.cancel_finalize is not None:
            sim.cancel_finalize()
            sim.cancel_finalize = None

    def _estimate_position(self, sim: _SimAxis) -> float:
        """Live estimated position (0-100) of an in-progress simulated move."""

        if sim.target is None or sim.move_start_time is None:
            return sim.position

        duration = sim.open_duration if sim.direction == "opening" else sim.close_duration
        rate = 100.0 / duration
        elapsed = max(0.0, time.monotonic() - sim.move_start_time)
        signed_delta = elapsed * rate * (1 if sim.direction == "opening" else -1)
        estimated = sim.move_start_position + signed_delta
        lo, hi = sorted((sim.move_start_position, sim.target))
        return min(max(estimated, lo), hi)

    def _freeze_sim_move(self, sim: _SimAxis) -> None:
        """Cancel an in-progress move and lock in the live estimate.

        Does not call stop_cover on the wrapped entity - used when it's no
        longer safe to assume the wrapped entity will respond (e.g. it just
        became unavailable).
        """

        if self._sim_move_active(sim):
            sim.position = self._estimate_position(sim)
        self._cancel_sim_timers(sim)
        sim.target = None
        sim.direction = None
        sim.move_start_position = None
        sim.move_start_time = None
        setattr(self, f"_attr_{sim.attr_name}", round(sim.position))
        if sim.drives_open_closing:
            self._attr_is_opening = False
            self._attr_is_closing = False
            self._attr_is_closed = self._sim_is_closed()

    @callback
    def _sim_tick(self, sim: _SimAxis, _now: datetime | None = None) -> None:
        """Periodic position update while a simulated move is in progress."""

        sim.position = self._estimate_position(sim)
        setattr(self, f"_attr_{sim.attr_name}", round(sim.position))
        self.async_write_ha_state()

    async def _sim_finalize(self, sim: _SimAxis, _now: datetime | None = None) -> None:
        """Called when a simulated move should have reached its target."""

        target = sim.target
        self._cancel_sim_timers(sim)
        if target is not None:
            sim.position = target
        sim.target = None
        sim.direction = None
        sim.move_start_position = None
        sim.move_start_time = None
        setattr(self, f"_attr_{sim.attr_name}", round(sim.position))
        if sim.drives_open_closing:
            self._attr_is_opening = False
            self._attr_is_closing = False
            self._attr_is_closed = self._sim_is_closed()
        self.async_write_ha_state()

        # A move that finished exactly at 0 or 100 ran the wrapped cover all
        # the way to its physical travel limit, not to a bounds-clamped
        # midpoint - if it has its own hardware endstop, skip our own stop
        # command and let the cover stop itself.
        at_hw_limit = target is not None and (target <= 0 or target >= 100)
        if not (sim.skip_stop_at_limits and at_hw_limit):
            await self._async_call_wrapped(sim.stop_service)

        if sim.drives_open_closing:
            await self._maybe_enforce_bounds()
        else:
            await self._maybe_enforce_tilt_bounds()

    async def _async_start_sim_move(self, sim: _SimAxis, target: float) -> None:
        """Begin, or retarget, a simulated move toward `target` (0-100)."""

        target = min(max(target, 0.0), 100.0)
        was_active = self._sim_move_active(sim)
        current = self._estimate_position(sim) if was_active else sim.position

        if current == target and not was_active:
            return

        if current == target:
            await self._sim_finalize(sim)
            return

        new_direction = "opening" if target > current else "closing"
        same_direction_retarget = was_active and sim.direction == new_direction
        reversed_retarget = was_active and sim.direction != new_direction

        self._cancel_sim_timers(sim)

        if reversed_retarget:
            # Reversing direction: stop the wrapped cover first rather than
            # trusting that issuing the opposite open/close command alone
            # will safely stop-then-reverse the motor.
            await self._async_call_wrapped(sim.stop_service)

        if not same_direction_retarget:
            service = sim.open_service if new_direction == "opening" else sim.close_service
            await self._async_call_wrapped(service)
        # else: already moving the right way, only the timers need updating.

        sim.position = current
        sim.move_start_position = current
        sim.move_start_time = time.monotonic()
        sim.target = target
        sim.direction = new_direction
        if sim.drives_open_closing:
            self._attr_is_opening = new_direction == "opening"
            self._attr_is_closing = new_direction == "closing"
        setattr(self, f"_attr_{sim.attr_name}", round(current))
        self.async_write_ha_state()

        duration = sim.open_duration if new_direction == "opening" else sim.close_duration
        delay = abs(target - current) / 100 * duration

        sim.cancel_tick = async_track_time_interval(
            self.hass, partial(self._sim_tick, sim), _SIM_TICK_INTERVAL
        )
        sim.cancel_finalize = async_call_later(
            self.hass, delay, partial(self._sim_finalize, sim)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the wrapped entity, bounds, enforcement, and simulation settings."""

        simulating = self._simulation_enabled()
        tilt_simulating = self._tilt_simulation_enabled()
        attrs = {
            CONF_MIN_VALUE: self._effective_min(),
            CONF_MAX_VALUE: self._effective_max(),
            CONF_ENFORCE_BOUNDS: self._enforce_bounds,
            CONF_TREAT_MIN_AS_CLOSED: self._treat_min_as_closed,
            CONF_WRAPPED_ENTITY: self._wrapped_entity_id,
            ATTR_SIMULATED_POSITION: simulating,
            CONF_MIN_TILT_VALUE: self._effective_min_tilt(),
            CONF_MAX_TILT_VALUE: self._effective_max_tilt(),
            CONF_ENFORCE_TILT_BOUNDS: self._enforce_tilt_bounds,
            ATTR_SIMULATED_TILT_POSITION: tilt_simulating,
        }
        if simulating:
            attrs[CONF_OPEN_DURATION] = self._open_duration
            attrs[CONF_CLOSE_DURATION] = self._close_duration
            attrs[CONF_SKIP_STOP_AT_LIMITS] = self._skip_stop_at_limits
            attrs[ATTR_MOVE_IN_PROGRESS] = self._sim_move_active(self._sim)
        if tilt_simulating:
            attrs[CONF_OPEN_TILT_DURATION] = self._open_tilt_duration
            attrs[CONF_CLOSE_TILT_DURATION] = self._close_tilt_duration
            attrs[CONF_SKIP_STOP_AT_TILT_LIMITS] = self._skip_stop_at_tilt_limits
            attrs[ATTR_TILT_MOVE_IN_PROGRESS] = self._sim_move_active(
                self._sim_tilt)
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
            if self._sim_move_active(self._sim):
                # Never fight an in-flight move: it was already targeted at a
                # bounds-clamped position by whoever started it.
                return

            clamped = min(
                max(self._sim.position, self._effective_min()
                    ), self._effective_max()
            )

            if clamped == self._sim.position:
                return

            await self._async_start_sim_move(self._sim, clamped)
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

    async def _maybe_enforce_tilt_bounds(self, enforce: bool | None = None) -> None:
        """Re-clamp the (real or simulated) tilt position if it's out of bounds."""

        should_enforce = self._enforce_tilt_bounds if enforce is None else enforce

        if not should_enforce:
            return

        if self._tilt_simulation_enabled():
            if self._sim_move_active(self._sim_tilt):
                return

            clamped = min(
                max(self._sim_tilt.position, self._effective_min_tilt()),
                self._effective_max_tilt(),
            )

            if clamped == self._sim_tilt.position:
                return

            await self._async_start_sim_move(self._sim_tilt, clamped)
            return

        state = self.hass.states.get(self._wrapped_entity_id)

        if state is None or state.state in (STATE_OPENING, STATE_CLOSING):
            return

        tilt_position = state.attributes.get(ATTR_CURRENT_TILT_POSITION)

        if tilt_position is None:
            return

        clamped = min(
            max(tilt_position, self._effective_min_tilt()),
            self._effective_max_tilt(),
        )

        if clamped == tilt_position:
            return

        await self._async_call_wrapped(
            SERVICE_SET_COVER_TILT_POSITION, {ATTR_TILT_POSITION: clamped}
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover, capped at the configured maximum."""

        max_value = self._effective_max()

        if self._simulation_enabled():
            await self._async_start_sim_move(self._sim, max_value)
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
            await self._async_start_sim_move(self._sim, min_value)
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

        if self._simulation_enabled() and self._sim_move_active(self._sim):
            self._freeze_sim_move(self._sim)
            self.async_write_ha_state()

        await self._async_call_wrapped(SERVICE_STOP_COVER)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a position, clamped to the configured bounds."""

        position = kwargs[ATTR_POSITION]
        clamped = min(max(position, self._effective_min()),
                      self._effective_max())

        if self._simulation_enabled():
            await self._async_start_sim_move(self._sim, clamped)
            return

        if self._is_at_position(clamped):
            return

        await self._async_call_wrapped(
            SERVICE_SET_COVER_POSITION, {ATTR_POSITION: clamped}
        )

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the cover tilt, capped at the configured maximum."""

        max_tilt_value = self._effective_max_tilt()

        if self._tilt_simulation_enabled():
            await self._async_start_sim_move(self._sim_tilt, max_tilt_value)
            return

        if max_tilt_value < 100 and CoverEntityFeature.SET_TILT_POSITION in (
            self._attr_supported_features
        ):
            if self._is_at_tilt_position(max_tilt_value):
                return

            await self._async_call_wrapped(
                SERVICE_SET_COVER_TILT_POSITION, {ATTR_TILT_POSITION: max_tilt_value}
            )
        else:
            if self._is_at_tilt_position(100):
                return

            await self._async_call_wrapped(SERVICE_OPEN_COVER_TILT)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the cover tilt, capped at the configured minimum."""

        min_tilt_value = self._effective_min_tilt()

        if self._tilt_simulation_enabled():
            await self._async_start_sim_move(self._sim_tilt, min_tilt_value)
            return

        if min_tilt_value > 0 and CoverEntityFeature.SET_TILT_POSITION in (
            self._attr_supported_features
        ):
            if self._is_at_tilt_position(min_tilt_value):
                return

            await self._async_call_wrapped(
                SERVICE_SET_COVER_TILT_POSITION, {ATTR_TILT_POSITION: min_tilt_value}
            )
        else:
            if self._is_at_tilt_position(0):
                return

            await self._async_call_wrapped(SERVICE_CLOSE_COVER_TILT)

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        """Stop the cover tilt."""

        if self._tilt_simulation_enabled() and self._sim_move_active(self._sim_tilt):
            self._freeze_sim_move(self._sim_tilt)
            self.async_write_ha_state()

        await self._async_call_wrapped(SERVICE_STOP_COVER_TILT)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the cover tilt to a position, clamped to the configured bounds."""

        tilt_position = kwargs[ATTR_TILT_POSITION]
        clamped = min(
            max(tilt_position, self._effective_min_tilt()),
            self._effective_max_tilt(),
        )

        if self._tilt_simulation_enabled():
            await self._async_start_sim_move(self._sim_tilt, clamped)
            return

        if self._is_at_tilt_position(clamped):
            return

        await self._async_call_wrapped(
            SERVICE_SET_COVER_TILT_POSITION, {ATTR_TILT_POSITION: clamped}
        )

    async def async_set_min_position(
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

    async def async_set_max_position(
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

    async def async_set_min_tilt_position(
        self, value: float, enforce: bool | None = None
    ) -> None:
        """Update the minimum tilt position bound at runtime."""

        self._min_tilt_value = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_MIN_TILT_VALUE: value},
        )
        self.async_write_ha_state()
        await self._maybe_enforce_tilt_bounds(enforce)

    async def async_set_max_tilt_position(
        self, value: float, enforce: bool | None = None
    ) -> None:
        """Update the maximum tilt position bound at runtime."""

        self._max_tilt_value = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_MAX_TILT_VALUE: value},
        )
        self.async_write_ha_state()
        await self._maybe_enforce_tilt_bounds(enforce)

    async def async_set_enforce_tilt_bounds(self, enforce: bool) -> None:
        """Update the proactive tilt-enforcement setting at runtime."""

        self._enforce_tilt_bounds = enforce
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_ENFORCE_TILT_BOUNDS: enforce},
        )
        self.async_write_ha_state()
        await self._maybe_enforce_tilt_bounds()
