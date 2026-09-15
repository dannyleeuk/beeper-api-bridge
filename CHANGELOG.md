# Changelog

## 1.0.0 — 2026-09-14
First public release.
- Pushover-compatible `/1/messages.json` and `/1/users/validate.json`, form-encoded or JSON.
- One chat per application token, posting as a per-application ghost; optional `single_room` and per-application `own_room`.
- Owner is joined to new chats automatically (Beeper leaves message requests half-accepted otherwise).
- `--check`, `--config`, `--version`; systemd units and a Dockerfile in `contrib/`.
