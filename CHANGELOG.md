# Changelog

## 1.2.0-beta.1 — 2026-09-24
Three optional features. **All are off unless you add their keys to `config.json`**; a 1.1 config behaves exactly as
before, and the Pushover-compatible API is unchanged.
- **Text webhook** (`"webhook": true`): `POST /webhook/<token>/<user_key>` accepts the incoming-webhook format many
  tools know from Slack - `text`, `blocks`, `attachments`, or a form field `payload` - and converts its formatting,
  links and common `:shortcodes:`. For senders whose only generic output is that kind of webhook.
- **Mute rules and quiet hours** (`"rules_file"`): mute by application, text pattern, priority and expiry; quiet hours
  that may span midnight. Emergencies always get through unless a rule says otherwise. The file is re-read when it
  changes, and any problem with it means nothing is muted.
- **Delivery log** (`"log_file"`): every notification recorded as sent, muted (and by which rule), rejected (and why)
  or failed; read it with `--log`. Capped, private, and it can never block a notification.
- `--check` also reports the webhook, rules and log settings.
- README: the three features, and *Relationship to Slack*.

## 1.1.0 — 2026-09-24
First stable 1.1 release: the 1.1.0 betas below, unchanged. The installer installs this by default.

## 1.1.0-beta.4 — 2026-09-24
- Tests run on every push and pull request, on Python 3.9 (the oldest supported) and 3.12.
- `python3 tests/test_bridge.py` now runs all 12 tests - the `unittest.main()` call sat in the middle of the file, so
  running it directly silently skipped the last two groups. `python3 -m unittest discover -s tests` was unaffected.
- README: *Running the tests* section. No change to the bridge itself.

## 1.1.0-beta.3 — 2026-09-24
- **Line breaks in plain-text messages are kept.** The HTML body (what Beeper and other clients display) left newlines as
  newlines, which HTML treats as spaces, so every multi-line message arrived as one run-on paragraph. They now become
  `<br/>`; the text is still escaped, the plain `body` is unchanged, and `html=1` messages are passed through as before.
- **No more `ConnectionResetError` tracebacks in the journal.** A client dropping an idle keep-alive connection (bbctl's
  appservice proxy does it after almost every event - hundreds a week) is routine, and is now logged at DEBUG only.
  Any other error still gets its full traceback.
- README: new section *How a message looks: plain text and HTML* - which field shows where, escaping, `html=1`,
  emoji vs `:shortcodes:` and Markdown.

## 1.1.0-beta.2 — 2026-09-15
- `install.sh`: one-line installer/updater for Debian/Ubuntu (reuses an existing bbctl login, `--no-systemd` for tmux users).
- `token`/`user` accepted on the query string; Uptime Kuma's stock webhook body is understood (title from the monitor and status).
- README: recipes for Uptime Kuma, Grafana, Proxmox VE/PBS, CrowdSec, Home Assistant and curl.

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
