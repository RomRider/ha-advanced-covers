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

- **`config_flow.py`** — two-step _config_ flow, plus a one-step _options_ flow:
  - Step `user`: pick the cover to wrap (`EntitySelector` filtered via `_own_entity_ids()` to exclude entities
    already created by this integration, preventing an Advanced Cover from wrapping another Advanced Cover), plus
    `min_value`/`max_value`/`enforce_bounds`/`hide_wrapped_entity`. The wrapped entity's ID is used as the config
    entry's `unique_id`, so each source cover can only be wrapped once (`_abort_if_unique_id_configured`).
  - Step `name`: a name field pre-filled (via `suggested_value`) with `_default_name()` — the wrapped entity's
    friendly name plus `" (Advanced)"` — which the user can accept or override.
  - The options flow (`OptionsFlowHandler`, step `init`) reuses `_bounds_fields()`, so it _shows_ a wrapped-entity
    selector too — but note that changing it there has no effect: both `__init__.py` and `cover.py` always read
    `entry.data[CONF_WRAPPED_ENTITY]` (never `entry.options`), so the wrapped entity is effectively fixed at
    creation time. Only bounds/enforce/hide are actually mutable via options.

- **`__init__.py`** — sets up the `cover` platform and an options `update_listener` that reloads the entry on
  change. Also owns wrapped-entity visibility: if `hide_wrapped_entity` is set, it marks the wrapped entity's
  registry entry `hidden_by = RegistryEntryHider.INTEGRATION` on setup and clears it on unload — but only when the
  entity isn't already hidden by the user directly (it never overrides a manual `hidden_by = USER`).

- **`cover.py`** — `AdvancedCoverEntity(CoverEntity)`:
  - Mirrors the wrapped entity's reportable state (position, tilt, device class, supported features,
    open/closed/opening/closing) via `async_track_state_change_event`, so it tracks external changes to the wrapped
    cover in real time.
  - Clamps every commanded position/open/close to `[min_value, max_value]` before forwarding the corresponding
    service call to the wrapped entity (`_async_call_wrapped`); tilt commands pass through unclamped.
  - Registers three entity services for runtime reconfiguration without reloading the integration:
    `set_min_value`, `set_max_value`, `set_enforce_bounds` (schemas in `services.yaml`, descriptions in
    `translations/en.json`). These persist back into `entry.options` via `async_update_entry`.
  - `enforce_bounds`, when on, proactively re-commands the wrapped cover back within bounds
    (`_maybe_enforce_bounds`) whenever its position is observed outside them (e.g. after an external command), as
    long as it isn't currently opening/closing.

## Gotchas worth remembering

- `manifest.json` has no `requirements` or version pin on Home Assistant itself — the entity-registry
  `RegistryEntryHider` API used in `__init__.py` requires HA 2021.11+, which is safe for any currently supported
  HA version but worth knowing if a very old HA is ever targeted.
