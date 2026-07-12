"""Constants for the Advanced Cover integration."""

DOMAIN = "advanced_cover"

CONF_WRAPPED_ENTITY = "wrapped_entity"
CONF_MIN_VALUE = "min_value"
CONF_MAX_VALUE = "max_value"
CONF_ENFORCE_BOUNDS = "enforce_bounds"
CONF_HIDE_WRAPPED_ENTITY = "hide_wrapped_entity"
CONF_OPEN_DURATION = "open_duration"
CONF_CLOSE_DURATION = "close_duration"
CONF_SKIP_STOP_AT_LIMITS = "skip_stop_at_limits"
CONF_TREAT_MIN_AS_CLOSED = "treat_min_as_closed"
CONF_MIN_TILT_VALUE = "min_tilt_value"
CONF_MAX_TILT_VALUE = "max_tilt_value"
CONF_ENFORCE_TILT_BOUNDS = "enforce_tilt_bounds"
CONF_OPEN_TILT_DURATION = "open_tilt_duration"
CONF_CLOSE_TILT_DURATION = "close_tilt_duration"
CONF_SKIP_STOP_AT_TILT_LIMITS = "skip_stop_at_tilt_limits"

DEFAULT_MIN_VALUE = 0
DEFAULT_MAX_VALUE = 100
DEFAULT_ENFORCE_BOUNDS = False
DEFAULT_HIDE_WRAPPED_ENTITY = False
DEFAULT_OPEN_DURATION = 20
DEFAULT_CLOSE_DURATION = 20
DEFAULT_SKIP_STOP_AT_LIMITS = True
DEFAULT_TREAT_MIN_AS_CLOSED = False
DEFAULT_MIN_TILT_VALUE = 0
DEFAULT_MAX_TILT_VALUE = 100
DEFAULT_ENFORCE_TILT_BOUNDS = False
DEFAULT_OPEN_TILT_DURATION = 5
DEFAULT_CLOSE_TILT_DURATION = 5
DEFAULT_SKIP_STOP_AT_TILT_LIMITS = True

SERVICE_SET_MIN_VALUE = "set_min_position"
SERVICE_SET_MAX_VALUE = "set_max_position"
SERVICE_SET_ENFORCE_BOUNDS = "set_enforce_bounds"
SERVICE_SET_MIN_TILT_VALUE = "set_min_tilt_position"
SERVICE_SET_MAX_TILT_VALUE = "set_max_tilt_position"
SERVICE_SET_ENFORCE_TILT_BOUNDS = "set_enforce_tilt_bounds"

ATTR_VALUE = "value"
ATTR_ENFORCE = "enforce"
ATTR_SIMULATED_POSITION = "simulated_position"
ATTR_MOVE_IN_PROGRESS = "move_in_progress"
ATTR_SIMULATED_TILT_POSITION = "simulated_tilt_position"
ATTR_TILT_MOVE_IN_PROGRESS = "tilt_move_in_progress"
