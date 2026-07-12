"""Config flow for the Advanced Cover integration."""

from __future__ import annotations

from typing import Any, Mapping

import voluptuous as vol

from homeassistant.components.cover import DOMAIN as COVER_DOMAIN, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import ATTR_FRIENDLY_NAME, ATTR_SUPPORTED_FEATURES, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_CLOSE_DURATION,
    CONF_CLOSE_TILT_DURATION,
    CONF_ENFORCE_BOUNDS,
    CONF_ENFORCE_TILT_BOUNDS,
    CONF_HIDE_WRAPPED_ENTITY,
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
    DEFAULT_HIDE_WRAPPED_ENTITY,
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
)


def _percent_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0, max=100, mode=NumberSelectorMode.BOX, unit_of_measurement="%"
        )
    )


def _duration_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=1, max=3600, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
        )
    )


def _own_entity_ids(hass: HomeAssistant) -> list[str]:
    """Return entity IDs already created by this integration.

    Excluded from the wrapped-entity selector so an Advanced Cover can't wrap
    another Advanced Cover.
    """

    registry = er.async_get(hass)
    return [
        entity.entity_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    ]


def _already_wrapped_entity_ids(hass: HomeAssistant) -> list[str]:
    """Return wrapped entity IDs already claimed by an existing config entry.

    Excluded from the wrapped-entity selector so the same cover can't be
    picked twice - each source cover can only be wrapped once, otherwise
    caught later (and less helpfully) by the unique_id abort.
    """

    return [
        entry.data[CONF_WRAPPED_ENTITY]
        for entry in hass.config_entries.async_entries(DOMAIN)
    ]


def _wrapped_entity_candidates(hass: HomeAssistant) -> list[str]:
    """Return cover entity IDs eligible to be wrapped.

    Eligible means: supports OPEN, CLOSE, and STOP (all three - the
    EntitySelector's declarative `filter.supported_features` can only
    express "has at least one of these features", since the frontend checks
    `supported_features & mask != 0`, an overlap test, not containment, so
    the required feature set is enforced here instead, as an explicit
    include list the picker filters its rendered options against); not
    already an Advanced Cover entity itself; and not already wrapped by an
    existing Advanced Cover entry.
    """

    excluded = set(_own_entity_ids(hass)) | set(_already_wrapped_entity_ids(hass))
    required = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    return [
        state.entity_id
        for state in hass.states.async_all(COVER_DOMAIN)
        if state.entity_id not in excluded
        and (
            CoverEntityFeature(state.attributes.get(
                ATTR_SUPPORTED_FEATURES, 0))
            & required
        )
        == required
    ]


def _wrapped_entity_field(hass: HomeAssistant) -> EntitySelector:
    return EntitySelector(
        EntitySelectorConfig(
            domain="cover",
            include_entities=_wrapped_entity_candidates(hass),
        )
    )


def _default_name(hass: HomeAssistant, wrapped_entity_id: str) -> str:
    state = hass.states.get(wrapped_entity_id)
    friendly_name = state.attributes.get(ATTR_FRIENDLY_NAME) if state else None
    return f"{friendly_name or wrapped_entity_id} (Advanced)"


def _wrapped_can_simulate_position(hass: HomeAssistant, wrapped_entity_id: str) -> bool:
    """Return whether time-based simulated positioning could ever activate.

    Only relevant when the wrapped cover lacks real SET_POSITION support (no
    point simulating what it can already do for real), and only possible if
    it supports STOP - a simulated move has to be able to stop mid-travel.
    """

    state = hass.states.get(wrapped_entity_id)
    if state is None:
        return False

    features = CoverEntityFeature(
        state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
    return (
        CoverEntityFeature.SET_POSITION not in features
        and CoverEntityFeature.STOP in features
    )


def _settings_fields(
    hass: HomeAssistant, wrapped_entity_id: str, defaults: Mapping[str, Any]
) -> dict:
    schema: dict = {
        vol.Optional(
            CONF_HIDE_WRAPPED_ENTITY,
            default=defaults.get(
                CONF_HIDE_WRAPPED_ENTITY, DEFAULT_HIDE_WRAPPED_ENTITY
            ),
        ): BooleanSelector(),
        vol.Optional(
            CONF_MIN_VALUE,
            default=defaults.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE),
        ): _percent_selector(),
        vol.Optional(
            CONF_MAX_VALUE,
            default=defaults.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE),
        ): _percent_selector(),
        vol.Optional(
            CONF_ENFORCE_BOUNDS,
            default=defaults.get(CONF_ENFORCE_BOUNDS, DEFAULT_ENFORCE_BOUNDS),
        ): BooleanSelector(),
        vol.Optional(
            CONF_TREAT_MIN_AS_CLOSED,
            default=defaults.get(
                CONF_TREAT_MIN_AS_CLOSED, DEFAULT_TREAT_MIN_AS_CLOSED
            ),
        ): BooleanSelector(),
    }

    if _wrapped_can_simulate_position(hass, wrapped_entity_id):
        # The wrapped cover can't report a real position, so simulated
        # positioning is mandatory (not a user choice) - only its travel
        # times are configurable.
        schema[
            vol.Optional(
                CONF_OPEN_DURATION,
                default=defaults.get(CONF_OPEN_DURATION,
                                     DEFAULT_OPEN_DURATION),
            )
        ] = _duration_selector()
        schema[
            vol.Optional(
                CONF_CLOSE_DURATION,
                default=defaults.get(CONF_CLOSE_DURATION,
                                     DEFAULT_CLOSE_DURATION),
            )
        ] = _duration_selector()
        schema[
            vol.Optional(
                CONF_SKIP_STOP_AT_LIMITS,
                default=defaults.get(
                    CONF_SKIP_STOP_AT_LIMITS, DEFAULT_SKIP_STOP_AT_LIMITS
                ),
            )
        ] = BooleanSelector()

    return schema


def _validate_bounds(user_input: dict[str, Any]) -> dict[str, str]:
    if user_input[CONF_MIN_VALUE] > user_input[CONF_MAX_VALUE]:
        return {"base": "min_greater_than_max"}
    return {}


def _wrapped_supports_tilt(hass: HomeAssistant, wrapped_entity_id: str) -> bool:
    """Return whether the wrapped cover qualifies for tilt bounds/simulation.

    Mirrors the OPEN+CLOSE+STOP requirement a cover must meet to be wrappable
    at all (`_wrapped_entity_candidates`), but for the tilt feature set -
    all three tilt actions are required so a simulated tilt move can always
    stop mid-travel if it ever needs to.
    """

    state = hass.states.get(wrapped_entity_id)
    if state is None:
        return False

    required = (
        CoverEntityFeature.OPEN_TILT
        | CoverEntityFeature.CLOSE_TILT
        | CoverEntityFeature.STOP_TILT
    )
    features = CoverEntityFeature(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
    return (features & required) == required


def _wrapped_can_simulate_tilt(hass: HomeAssistant, wrapped_entity_id: str) -> bool:
    """Return whether time-based simulated tilt positioning could ever activate.

    Tilt mirror of `_wrapped_can_simulate_position`.
    """

    state = hass.states.get(wrapped_entity_id)
    if state is None:
        return False

    features = CoverEntityFeature(
        state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
    return (
        CoverEntityFeature.SET_TILT_POSITION not in features
        and CoverEntityFeature.STOP_TILT in features
    )


def _tilt_settings_fields(
    hass: HomeAssistant, wrapped_entity_id: str, defaults: Mapping[str, Any]
) -> dict:
    schema: dict = {
        vol.Optional(
            CONF_MIN_TILT_VALUE,
            default=defaults.get(CONF_MIN_TILT_VALUE, DEFAULT_MIN_TILT_VALUE),
        ): _percent_selector(),
        vol.Optional(
            CONF_MAX_TILT_VALUE,
            default=defaults.get(CONF_MAX_TILT_VALUE, DEFAULT_MAX_TILT_VALUE),
        ): _percent_selector(),
        vol.Optional(
            CONF_ENFORCE_TILT_BOUNDS,
            default=defaults.get(
                CONF_ENFORCE_TILT_BOUNDS, DEFAULT_ENFORCE_TILT_BOUNDS
            ),
        ): BooleanSelector(),
    }

    if _wrapped_can_simulate_tilt(hass, wrapped_entity_id):
        # The wrapped cover can't report a real tilt position, so simulated
        # tilt positioning is mandatory (not a user choice) - only its
        # travel times are configurable.
        schema[
            vol.Optional(
                CONF_OPEN_TILT_DURATION,
                default=defaults.get(
                    CONF_OPEN_TILT_DURATION, DEFAULT_OPEN_TILT_DURATION
                ),
            )
        ] = _duration_selector()
        schema[
            vol.Optional(
                CONF_CLOSE_TILT_DURATION,
                default=defaults.get(
                    CONF_CLOSE_TILT_DURATION, DEFAULT_CLOSE_TILT_DURATION
                ),
            )
        ] = _duration_selector()
        schema[
            vol.Optional(
                CONF_SKIP_STOP_AT_TILT_LIMITS,
                default=defaults.get(
                    CONF_SKIP_STOP_AT_TILT_LIMITS, DEFAULT_SKIP_STOP_AT_TILT_LIMITS
                ),
            )
        ] = BooleanSelector()

    return schema


def _validate_tilt_bounds(user_input: dict[str, Any]) -> dict[str, str]:
    if user_input[CONF_MIN_TILT_VALUE] > user_input[CONF_MAX_TILT_VALUE]:
        return {"base": "min_tilt_greater_than_max_tilt"}
    return {}


class AdvancedCoverConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Advanced Cover."""

    VERSION = 1

    _wrapped_entity_id: str
    _settings_data: dict[str, Any]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the wrapped-entity selection step."""

        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_WRAPPED_ENTITY])
            self._abort_if_unique_id_configured()

            self._wrapped_entity_id = user_input[CONF_WRAPPED_ENTITY]
            return await self.async_step_settings()

        schema = vol.Schema(
            {
                vol.Required(CONF_WRAPPED_ENTITY): _wrapped_entity_field(self.hass),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the settings step."""

        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_bounds(user_input)

            if not errors:
                merged_data = {
                    CONF_WRAPPED_ENTITY: self._wrapped_entity_id,
                    **user_input,
                }
                if _wrapped_supports_tilt(self.hass, self._wrapped_entity_id):
                    self._settings_data = merged_data
                    return await self.async_step_tilt_settings()

                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=merged_data
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=(user_input or {}).get(
                        CONF_NAME, _default_name(
                            self.hass, self._wrapped_entity_id)
                    ),
                ): str,
                **_settings_fields(
                    self.hass, self._wrapped_entity_id, user_input or {}
                ),
            }
        )

        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors
        )

    async def async_step_tilt_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the tilt bounds/enforcement/simulation step.

        Only reached when the wrapped entity supports tilt (see
        `_wrapped_supports_tilt`); the entry is created here instead of in
        `async_step_settings` for those entries.
        """

        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_tilt_bounds(user_input)

            if not errors:
                return self.async_create_entry(
                    title=self._settings_data[CONF_NAME],
                    data={**self._settings_data, **user_input},
                )

        schema = vol.Schema(
            _tilt_settings_fields(
                self.hass, self._wrapped_entity_id, user_input or {}
            )
        )

        return self.async_show_form(
            step_id="tilt_settings", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""

        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    """Handle an options flow for Advanced Cover."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""

        self._config_entry = config_entry
        self._init_data: dict[str, Any] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""

        errors: dict[str, str] = {}
        current: Mapping[str, Any] = {
            **self._config_entry.data,
            **self._config_entry.options,
        }
        wrapped_entity_id = self._config_entry.data[CONF_WRAPPED_ENTITY]

        if user_input is not None:
            errors = _validate_bounds(user_input)

            if not errors:
                if _wrapped_supports_tilt(self.hass, wrapped_entity_id):
                    self._init_data = user_input
                    return await self.async_step_tilt_settings()

                return self.async_create_entry(title="", data=user_input)

            current = user_input

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                _settings_fields(self.hass, wrapped_entity_id, current)
            ),
            errors=errors,
        )

    async def async_step_tilt_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the tilt bounds/enforcement/simulation step.

        Only reached when the wrapped entity supports tilt; nothing is
        persisted to `entry.options` until this step also completes.
        """

        errors: dict[str, str] = {}
        wrapped_entity_id = self._config_entry.data[CONF_WRAPPED_ENTITY]
        current: Mapping[str, Any] = {
            **self._config_entry.data,
            **self._config_entry.options,
            **(self._init_data or {}),
        }

        if user_input is not None:
            errors = _validate_tilt_bounds(user_input)

            if not errors:
                return self.async_create_entry(
                    title="", data={**(self._init_data or {}), **user_input}
                )

            current = user_input

        return self.async_show_form(
            step_id="tilt_settings",
            data_schema=vol.Schema(
                _tilt_settings_fields(self.hass, wrapped_entity_id, current)
            ),
            errors=errors,
        )
