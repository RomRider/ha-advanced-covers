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
  (`DEFAULT_*`), and the three entity-service names/attributes. Keep `translations/en.json` and `services.yaml` in
  sync with any keys changed here.

- **`config_flow.py`** — two-step _config_ flow, plus a one-step _options_ flow, sharing field builders:
  - Step `user`: pick the cover to wrap only (`_wrapped_entity_field()`, an `EntitySelector` filtered via
    `_own_entity_ids()` to exclude entities already created by this integration, preventing an Advanced Cover from
    wrapping another Advanced Cover). The wrapped entity's ID is used as the config entry's `unique_id`, so each
    source cover can only be wrapped once (`_abort_if_unique_id_configured`).
  - Step `settings`: everything else, built by `_settings_fields()` — `name` (pre-filled via `suggested_value` with
    `_default_name()`, the wrapped entity's friendly name plus `" (Advanced)"`, which the user can accept or
    override), `min_value`/`max_value`/`enforce_bounds`/`hide_wrapped_entity`, and conditionally:
    `time_based_positioning` is only included in the schema when `_wrapped_supports_position()` finds the wrapped
    entity lacks `CoverEntityFeature.SET_POSITION`; `open_duration`/`close_duration` are only included when
    `time_based_positioning` is (already) enabled. Since a single form render can't react live to a checkbox, a
    submission that just turned `time_based_positioning` on but didn't include the duration fields (because they
    weren't in that render's schema yet) is detected (`revealing_durations`) and redisplays the same step with the
    duration fields added, instead of creating the entry — so enabling simulation takes two submits.
  - The options flow (`OptionsFlowHandler`, step `init`) reuses `_settings_fields(include_name=False)` and the same
    `revealing_durations` two-submit pattern, but never shows a wrapped-entity selector — the wrapped entity is
    fixed at creation time (both `__init__.py` and `cover.py` always read `entry.data[CONF_WRAPPED_ENTITY]`, never
    `entry.options`), so it can only be picked in the config flow's `user` step, not reconfigured afterwards. Only
    bounds/enforce/hide/simulation settings are mutable via options.

- **`__init__.py`** — sets up the `cover` platform and an options `update_listener` that reloads the entry on
  change. Also owns wrapped-entity visibility: if `hide_wrapped_entity` is set, it marks the wrapped entity's
  registry entry `hidden_by = RegistryEntryHider.INTEGRATION` on setup and clears it on unload — but only when the
  entity isn't already hidden by the user directly (it never overrides a manual `hidden_by = USER`).

- **`cover.py`** — `AdvancedCoverEntity(CoverEntity, RestoreEntity)`:
  - Mirrors the wrapped entity's reportable state (position, tilt, device class, supported features,
    open/closed/opening/closing) via `async_track_state_change_event`, so it tracks external changes to the wrapped
    cover in real time. This mirroring is skipped for position/opening/closing/closed when `time_based_positioning`
    is actively simulating (see below) — everything else (device class, tilt, availability) is still mirrored.
  - Clamps every commanded position/open/close to `[min_value, max_value]` before forwarding the corresponding
    service call to the wrapped entity (`_async_call_wrapped`); tilt commands pass through unclamped.
  - Registers three entity services for runtime reconfiguration without reloading the integration:
    `set_min_value`, `set_max_value`, `set_enforce_bounds` (schemas in `services.yaml`, descriptions in
    `translations/en.json`). These persist back into `entry.options` via `async_update_entry`.
  - `enforce_bounds`, when on, proactively re-commands the wrapped cover back within bounds
    (`_maybe_enforce_bounds`) whenever its position is observed outside them (e.g. after an external command), as
    long as it isn't currently opening/closing. `_maybe_enforce_bounds` branches on `_simulation_enabled()`: for a
    simulating cover it re-clamps the *simulated* position (via `_async_start_sim_move`) instead of reading the
    wrapped entity's real (nonexistent) position attribute.
  - **`time_based_positioning`** (`CONF_TIME_BASED_POSITIONING`/`CONF_OPEN_DURATION`/`CONF_CLOSE_DURATION`): opt-in
    simulated absolute positioning for a wrapped cover that lacks real `SET_POSITION` support. When active
    (`_simulation_enabled()`: toggle on, wrapped lacks `SET_POSITION`, wrapped has `STOP`), the entity synthetically
    ORs `SET_POSITION` into its own `_attr_supported_features` and drives moves itself: `_async_start_sim_move`
    calls `open_cover`/`close_cover` on the wrapped entity, ticks the estimated position every ~0.5s
    (`_sim_tick`/`async_track_time_interval`), and calls `stop_cover` once the computed travel time elapses
    (`_sim_finalize`/`async_call_later`). Retargeting mid-move reuses the same timers rather than restarting from
    scratch (same-direction: just reschedule; direction reversal: `stop_cover` then the opposite command first).
    The wrapped entity's own reported state is deliberately never trusted as a position source while simulating —
    only this integration's own tracked commands + elapsed time are — because such covers typically only report a
    binary open/closed state that can't distinguish "fully open" from "50% open". The simulated position persists
    across restarts via `RestoreEntity`, defaulting to closed (0%) the first time an entry is ever set up. One
    residual limitation: an out-of-band move (e.g. a physical remote) can't be detected while simulating, since the
    wrapped entity's real state is ignored — `enforce_bounds` still corrects the *simulated* position, just not in
    response to hardware it never observes.
  - **`skip_stop_at_limits`** (`CONF_SKIP_STOP_AT_LIMITS`, default `True`, only offered in the config/options flow
    while `time_based_positioning` is enabled): when a simulated move's `_sim_finalize` target is exactly 0 or 100
    — i.e. the move ran all the way to a bound that sits at the cover's physical travel limit, not to a
    bounds-clamped midpoint — the `stop_cover` call is skipped on the assumption the wrapped cover has its own
    hardware endstop that already halted it. A move finishing at any other bound value always still sends
    `stop_cover`, since there's no physical limit to rely on there. This only affects the finalize-time stop;
    the mid-move `stop_cover` sent on direction reversal (`_async_start_sim_move`) is unconditional.

## Gotchas worth remembering

- `manifest.json` has no `requirements` or version pin on Home Assistant itself — the entity-registry
  `RegistryEntryHider` API used in `__init__.py` requires HA 2021.11+, which is safe for any currently supported
  HA version but worth knowing if a very old HA is ever targeted.
- `test/configuration.yaml` defines `cover.fake_shade_no_position` (open/close/stop only, no `SET_POSITION`) and
  `cover.fake_shade_no_stop` (open/close only, no `STOP`) as `template` covers backed by an `input_boolean`, since
  the `demo:` platform's covers all support `SET_POSITION` and can't exercise `time_based_positioning`. `stop_cover`
  calls log via `system_log.write`, visible at Settings → System → Logs.
