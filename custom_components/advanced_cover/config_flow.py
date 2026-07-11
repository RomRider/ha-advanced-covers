"""Config flow for the Advanced Cover integration."""

from __future__ import annotations

from typing import Any, Mapping

import voluptuous as vol

from homeassistant.components.cover import CoverEntityFeature
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
    CONF_ENFORCE_BOUNDS,
    CONF_HIDE_WRAPPED_ENTITY,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_OPEN_DURATION,
    CONF_SKIP_STOP_AT_LIMITS,
    CONF_TIME_BASED_POSITIONING,
    CONF_WRAPPED_ENTITY,
    DEFAULT_CLOSE_DURATION,
    DEFAULT_ENFORCE_BOUNDS,
    DEFAULT_HIDE_WRAPPED_ENTITY,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_OPEN_DURATION,
    DEFAULT_SKIP_STOP_AT_LIMITS,
    DEFAULT_TIME_BASED_POSITIONING,
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


def _wrapped_entity_field(hass: HomeAssistant) -> EntitySelector:
    return EntitySelector(
        EntitySelectorConfig(domain="cover", exclude_entities=_own_entity_ids(hass))
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

    features = CoverEntityFeature(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
    return (
        CoverEntityFeature.SET_POSITION not in features
        and CoverEntityFeature.STOP in features
    )


def _settings_fields(
    hass: HomeAssistant, wrapped_entity_id: str, defaults: Mapping[str, Any]
) -> dict:
    schema: dict = {
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
            CONF_HIDE_WRAPPED_ENTITY,
            default=defaults.get(
                CONF_HIDE_WRAPPED_ENTITY, DEFAULT_HIDE_WRAPPED_ENTITY
            ),
        ): BooleanSelector(),
    }

    if _wrapped_can_simulate_position(hass, wrapped_entity_id):
        simulating = defaults.get(
            CONF_TIME_BASED_POSITIONING, DEFAULT_TIME_BASED_POSITIONING
        )

        schema[
            vol.Optional(CONF_TIME_BASED_POSITIONING, default=simulating)
        ] = BooleanSelector()

        if simulating:
            schema[
                vol.Optional(
                    CONF_OPEN_DURATION,
                    default=defaults.get(CONF_OPEN_DURATION, DEFAULT_OPEN_DURATION),
                )
            ] = _duration_selector()
            schema[
                vol.Optional(
                    CONF_CLOSE_DURATION,
                    default=defaults.get(CONF_CLOSE_DURATION, DEFAULT_CLOSE_DURATION),
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


class AdvancedCoverConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Advanced Cover."""

    VERSION = 1

    _wrapped_entity_id: str

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

            # If the user just enabled simulated positioning, the duration
            # fields weren't part of this submission's schema yet — redisplay
            # the form with them added instead of creating the entry.
            revealing_durations = (
                user_input.get(CONF_TIME_BASED_POSITIONING)
                and CONF_OPEN_DURATION not in user_input
            )

            if not errors and not revealing_durations:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_WRAPPED_ENTITY: self._wrapped_entity_id,
                        **user_input,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=(user_input or {}).get(
                        CONF_NAME, _default_name(self.hass, self._wrapped_entity_id)
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

            revealing_durations = (
                user_input.get(CONF_TIME_BASED_POSITIONING)
                and CONF_OPEN_DURATION not in user_input
            )

            if not errors and not revealing_durations:
                return self.async_create_entry(title="", data=user_input)

            current = user_input

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                _settings_fields(self.hass, wrapped_entity_id, current)
            ),
            errors=errors,
        )
