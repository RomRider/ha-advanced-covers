# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (`custom_components/advanced_cover`, domain `advanced_cover`) that wraps an
existing `cover` entity and exposes a new cover entity whose usable position range is clamped to a configurable
`[min_value, max_value]` window. It is config-entry only (`config_flow: true` in `manifest.json`); there is no YAML
setup path.

## Development environment

There is no pytest suite, linter, or build step in this repo — development and verification happen by running a
real Home Assistant instance via the devcontainer.

- Open the repo in the devcontainer (`.devcontainer/devcontainer.json`), which uses the
  `romrider/hass-custom-devcontainer` image and bind-mounts:
  - `custom_components/advanced_cover` → `/config/custom_components/advanced_cover`
  - `test/configuration.yaml` → `/config/configuration.yaml`
  - `test/` → `/config/test`
- Run the VS Code task **"devcontainer: Start HA"** to start Home Assistant. It listens on `$HA_PORT` (default
  `8123`); debugpy listens on `$HA_DEBUGPY` (default `5678`), configured via `debugpy: start: true, wait: true` in
  `test/configuration.yaml` (HA waits for a debugger to attach before finishing startup).
- Log in at `http://127.0.0.1:8123` with the credentials in `test/.env` (`test` / `test`).
- Attach a debugger with the **"Python: Attach Local"** launch config (connects to port 5678, maps the workspace
  root to `/config`).
- To exercise a change: add/reconfigure an "Advanced Cover" integration instance from Settings → Devices & Services
  in the running HA UI, pick a `cover.*` entity to wrap, and drive it from Developer Tools → States/Services.
- `test/configuration.yaml` loads `default_config:` and `demo:`, so the Home Assistant demo platform's covers are
  available as wrap targets.

## Architecture

The integration has four files of real logic; the big picture only becomes clear from reading them together:

- **`const.py`** — single source of truth for the domain, all config/option keys (`CONF_*`), their defaults
  (`DEFAULT_*`), and the six entity-service names/attributes (position + tilt). Keep `translations/en.json` and
  `services.yaml` in sync with any keys changed here.

- **`config_flow.py`** — a 2-or-3-step _config_ flow, plus a 1-or-2-step _options_ flow, sharing field builders.
  Field visibility is purely capability-gated on the wrapped entity's currently reported `supported_features` (no
  same-render reveal-on-checkbox logic exists anywhere in this file — a field is either always in a given step's
  schema or never, depending on what the wrapped entity supports at the time the step is shown):
  - Step `user`: pick the cover to wrap only (`_wrapped_entity_field()`, an `EntitySelector` filtered via
    `_own_entity_ids()` to exclude entities already created by this integration, preventing an Advanced Cover from
    wrapping another Advanced Cover). The wrapped entity's ID is used as the config entry's `unique_id`, so each
    source cover can only be wrapped once (`_abort_if_unique_id_configured`).
  - Step `settings`: position bounds/enforcement/simulation, built by `_settings_fields()` — `name` (pre-filled via
    `suggested_value` with `_default_name()`, the wrapped entity's friendly name plus `" (Advanced)"`, which the
    user can accept or override), `min_value`/`max_value`/`enforce_bounds`/`treat_min_as_closed`/
    `hide_wrapped_entity` always; `open_duration`/`close_duration`/`skip_stop_at_limits` only when
    `_wrapped_can_simulate_position()` finds the wrapped entity lacks `CoverEntityFeature.SET_POSITION` but has
    `STOP` (simulated positioning is then mandatory, not a user choice — only its travel times are configurable).
  - Step `tilt_settings`: the tilt mirror of `settings`, built by `_tilt_settings_fields()` — reached only when
    `_wrapped_supports_tilt()` finds the wrapped entity has `OPEN_TILT + CLOSE_TILT + STOP_TILT` (mirroring the
    `OPEN + CLOSE + STOP` bar a cover must clear to be wrappable at all); skipped entirely otherwise, in which case
    tilt stays an unclamped passthrough. Always shows `min_tilt_value`/`max_tilt_value`/`enforce_tilt_bounds`;
    conditionally shows `open_tilt_duration`/`close_tilt_duration`/`skip_stop_at_tilt_limits` when
    `_wrapped_can_simulate_tilt()` (lacks `SET_TILT_POSITION`, has `STOP_TILT`). This is the config flow's final
    step, so `async_create_entry` happens here (or in `settings` directly, for wraps that don't qualify for tilt).
  - The options flow (`OptionsFlowHandler`) mirrors this shape as steps `init` (position, reusing
    `_settings_fields(include_name=False)`) and `tilt_settings` (tilt, gated the same way), but never shows a
    wrapped-entity selector — the wrapped entity is fixed at creation time (both `__init__.py` and `cover.py` always
    read `entry.data[CONF_WRAPPED_ENTITY]`, never `entry.options`), so it can only be picked in the config flow's
    `user` step, not reconfigured afterwards. Nothing is persisted to `entry.options` until whichever step is last
    for a given wrapped entity (`init` alone, or `init` then `tilt_settings`) completes.

- **`__init__.py`** — sets up the `cover` platform and an options `update_listener` that reloads the entry on
  change. Also owns wrapped-entity visibility: if `hide_wrapped_entity` is set, it marks the wrapped entity's
  registry entry `hidden_by = RegistryEntryHider.INTEGRATION` on setup and clears it on unload — but only when the
  entity isn't already hidden by the user directly (it never overrides a manual `hidden_by = USER`).

- **`cover.py`** — `AdvancedCoverEntity(CoverEntity, RestoreEntity)`. Position and tilt each get their own
  independent bounds, enforcement setting, and simulation engine — a wrapped cover can simulate one, both, or
  neither axis depending on its own real feature support:
  - Mirrors the wrapped entity's reportable state (position, tilt, device class, supported features,
    open/closed/opening/closing) via `async_track_state_change_event`, so it tracks external changes to the wrapped
    cover in real time. Position mirroring (position/opening/closing/closed) is skipped while position is
    simulating; tilt mirroring (tilt position only) is skipped independently while tilt is simulating — everything
    else (device class, availability) is always mirrored.
  - Clamps every commanded position/open/close to `[min_value, max_value]`, and every commanded tilt
    position/open/close-tilt to `[min_tilt_value, max_tilt_value]`, before forwarding the corresponding service
    call to the wrapped entity (`_async_call_wrapped`).
  - Registers six entity services for runtime reconfiguration without reloading the integration: `set_min_position`,
    `set_max_position`, `set_enforce_bounds`, `set_min_tilt_position`, `set_max_tilt_position`,
    `set_enforce_tilt_bounds` (schemas in `services.yaml`, descriptions in `translations/en.json`). These persist
    back into `entry.options` via `async_update_entry`.
  - `enforce_bounds`/`enforce_tilt_bounds`, when on, proactively re-command the wrapped cover back within their
    respective bounds (`_maybe_enforce_bounds`/`_maybe_enforce_tilt_bounds`) whenever position/tilt is observed
    outside them (e.g. after an external command), as long as the cover isn't currently opening/closing. Each
    branches on its own `_simulation_enabled()`/`_tilt_simulation_enabled()`: for a simulating axis it re-clamps the
    *simulated* position (via `_async_start_sim_move`) instead of reading the wrapped entity's real (nonexistent)
    position attribute.
  - **Time-based simulated positioning** (position: always available when the wrapped cover qualifies; tilt: same,
    independently, when the wrapped cover's tilt qualifies): automatic, not a user toggle — activates whenever an
    axis lacks its real `SET_POSITION`/`SET_TILT_POSITION` support but the wrapped entity has the matching `STOP`/
    `STOP_TILT`. Both axes share one engine, parameterized by a small `_SimAxis` dataclass (`self._sim` for
    position, `self._sim_tilt` for tilt) holding per-axis identity (which `_attr_*` field it drives, which
    open/close/stop service to call, travel durations) and move state (target, direction, timers). When an axis is
    simulating, the entity synthetically ORs the matching `SET_POSITION`/`SET_TILT_POSITION` into its own
    `_attr_supported_features` and drives moves itself: `_async_start_sim_move` calls open/close on the wrapped
    entity, ticks the estimated position every ~0.5s (`_sim_tick`/`async_track_time_interval`), and calls stop once
    the computed travel time elapses (`_sim_finalize`/`async_call_later`). Retargeting mid-move reuses the same
    timers rather than restarting from scratch (same-direction: just reschedule; direction reversal: stop then the
    opposite command first). The wrapped entity's own reported state is deliberately never trusted as a position
    source while an axis is simulating — only this integration's own tracked commands + elapsed time are — because
    such covers typically only report a binary open/closed state that can't distinguish "fully open" from "50%
    open". Both simulated positions persist across restarts via `RestoreEntity`, each defaulting to closed (0%) the
    first time an entry is ever set up. One residual limitation: an out-of-band move (e.g. a physical remote) can't
    be detected while simulating, since the wrapped entity's real state is ignored — enforcement still corrects the
    *simulated* position, just not in response to hardware it never observes.
  - **Critical invariant**: HA's `CoverEntity` has exactly one `is_opening`/`is_closing` pair for the whole entity's
    derived `.state`. `_SimAxis.drives_open_closing` is `True` only for the position axis, so a tilt-only simulated
    move never touches `is_opening`/`is_closing`/`is_closed` — otherwise a tilt move would falsely report the whole
    cover as opening/closing while only the slats move. This is also why there's no tilt equivalent of
    `treat_min_as_closed`/`_sim_is_closed()` — tilt never drives the entity's closed state.
  - **`skip_stop_at_limits`/`skip_stop_at_tilt_limits`** (`CONF_SKIP_STOP_AT_LIMITS`/`CONF_SKIP_STOP_AT_TILT_LIMITS`,
    default `True` for both, only offered in the config/options flow while the corresponding axis is simulating):
    when a simulated move's `_sim_finalize` target is exactly 0 or 100 — i.e. the move ran all the way to a bound
    that sits at the cover's physical travel limit, not to a bounds-clamped midpoint — the stop call is skipped on
    the assumption the wrapped cover has its own hardware endstop that already halted it. A move finishing at any
    other bound value always still sends the stop call, since there's no physical limit to rely on there. This only
    affects the finalize-time stop; the mid-move stop sent on direction reversal (`_async_start_sim_move`) is
    unconditional.

## Gotchas worth remembering

- `manifest.json` has no `requirements` or version pin on Home Assistant itself — the entity-registry
  `RegistryEntryHider` API used in `__init__.py` requires HA 2021.11+, which is safe for any currently supported
  HA version but worth knowing if a very old HA is ever targeted.
- `test/configuration.yaml` defines `cover.fake_shade_no_position` (open/close/stop only, no `SET_POSITION`),
  `cover.fake_shade_no_stop` (open/close only, no `STOP`), and `cover.fake_shade_no_tilt_position` (real
  open/close/stop for position, but open/close/stop-only tilt with no `SET_TILT_POSITION`) as `template` covers
  backed by `input_boolean`s, since the `demo:` platform's covers either fully support `SET_POSITION`/
  `SET_TILT_POSITION` or don't expose tilt at all, and so can't exercise simulated positioning/tilt positioning.
  `stop_cover`/`stop_cover_tilt` calls log via `system_log.write`, visible at Settings → System → Logs.
