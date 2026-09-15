# Changelog

## 1.1.0-beta.1 — 2026-09-15
First public pre-release (beta: one homelab has run it for a few weeks; expect rough edges).
Renamed to **beeper-api-bridge**. It is its own thing that happens to speak Pushover's API, and the old name suggested
otherwise. Script `beeper_api_bridge.py`, env `BEEPER_API_BRIDGE_CONFIG` / `_LOGLEVEL`, config keys `api_bind` / `api_port`
(the old `pushover_*` keys are still read), systemd units `beeper-api-bridge*.service`. No wire-protocol change.

## 1.0.1 — 2026-09-15
- Ghost user prefix is read from the registration's user namespace (whatever name you gave `bbctl register`) instead of
  being hard-coded; `ghost_prefix` in config.json overrides. Room topic and bridge metadata no longer say "Pushover".

## 1.0.0 — 2026-09-14
First public release.
- Pushover-compatible `/1/messages.json` and `/1/users/validate.json`, form-encoded or JSON.
- One chat per application token, posting as a per-application ghost; optional `single_room` and per-application `own_room`.
- Owner is joined to new chats automatically (Beeper leaves message requests half-accepted otherwise).
- `--check`, `--config`, `--version`; systemd units and a Dockerfile in `contrib/`.
