"""Config flow for the Advanced Cover integration."""

from __future__ import annotations

from typing import Any, Mapping

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_ENFORCE_BOUNDS,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_WRAPPED_ENTITY,
    DEFAULT_ENFORCE_BOUNDS,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DOMAIN,
)


def _percent_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0, max=100, mode=NumberSelectorMode.BOX, unit_of_measurement="%"
        )
    )


def _bounds_fields(defaults: Mapping[str, Any]) -> dict:
    return {
        vol.Required(
            CONF_WRAPPED_ENTITY,
            description={"suggested_value": defaults.get(CONF_WRAPPED_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain="cover")),
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
    }


def _validate_bounds(user_input: dict[str, Any]) -> dict[str, str]:
    if user_input[CONF_MIN_VALUE] > user_input[CONF_MAX_VALUE]:
        return {"base": "min_greater_than_max"}
    return {}


class AdvancedCoverConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Advanced Cover."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""

        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_bounds(user_input)

            if not errors:
                await self.async_set_unique_id(user_input[CONF_WRAPPED_ENTITY])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=user_input
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=(user_input or {}).get(CONF_NAME, ""),
                ): str,
                **_bounds_fields(user_input or {}),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

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

        if user_input is not None:
            errors = _validate_bounds(user_input)

            if not errors:
                return self.async_create_entry(title="", data=user_input)

            current = user_input

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_bounds_fields(current)),
            errors=errors,
        )
