# beeper-api-bridge — a notification API for Beeper

> **New in 1.2.0-beta.1 (beta):** apps whose only notification option is **Slack**, such as Prometheus Alertmanager,
> can now send to Beeper too. Point their Slack webhook URL at the bridge's
> [Slack-compatible text webhook](#text-webhook-slack-compatible). The release also adds
> [mute rules and quiet hours](#mute-rules-and-quiet-hours) and a [delivery log](#delivery-log). All three are off
> until you configure them, so an existing setup behaves exactly as before. This is a pre-release, so the installer
> doesn't pick it up by default: use `--ref v1.2.0-beta.1` ([Quick install](#quick-install-debian--ubuntu)).

A tiny, dependency-light [Matrix appservice](https://spec.matrix.org/latest/application-service-api/) that gives your
**Beeper** account an HTTP notification endpoint for two kinds of sender:

- **Apps that can send Pushover notifications.** The bridge speaks the same API as [Pushover](https://pushover.net/api),
  so these apps can be pointed at it instead of `api.pushover.net`.
- **Apps that can only send Slack notifications** *(new, beta)*. The bridge accepts the incoming-webhook format those
  apps send to Slack, so Prometheus Alertmanager and similar senders need nothing more than a different webhook URL.

Either way, the messages arrive in your Beeper chats. It is its own bridge: nothing here talks to Pushover or to Slack.

Almost every self-hosted tool has a Pushover integration built in: Grafana, Uptime Kuma, Proxmox VE / Backup Server,
CrowdSec, Home Assistant, Gotify-style scripts and plain `curl`. The ones that don't usually have a Slack one. Either
way, you get chat notifications from all of them without writing an adapter for each. No Pushover or Slack account,
subscription or licence is involved: you mint the tokens yourself.

```
Grafana / Uptime Kuma / Proxmox / cron …  ──POST /1/messages.json───────────────▶┐
                                                                                 ├─▶ beeper-api-bridge ──▶ Beeper (Matrix)
Alertmanager / other Slack-only senders  ──POST /webhook/<token>/<user_key>─────▶┘
```

- One chat per application token, each posting as its own ghost user, so a message shows as coming from "Uptime Kuma"
  rather than from you. Or set `single_room` and everything lands in one chat (still labelled per sender).
- Priorities (-2…2, the Pushover scale) become a `[low]` / `[HIGH]` / `[EMERGENCY]` prefix; `url` / `url_title` become a link; `html=1` is honoured.
- Unknown tokens and bad user keys are refused with Pushover's own `{"status":0,"errors":[…]}` shape, so senders
  surface the error normally.
- Optional, off unless you configure them: a [text webhook](#text-webhook-slack-compatible) endpoint,
  [mute rules and quiet hours](#mute-rules-and-quiet-hours), and a [delivery log](#delivery-log).
- Single Python file. Standard library plus PyYAML (to read the registration file bbctl writes).

## How it works

Beeper lets you run your own bridges through [`bbctl`](https://github.com/beeper/bridge-manager) (the *bridge manager*).
`bbctl register` creates an appservice registration on your Beeper account; `bbctl proxy` keeps an outbound websocket
open to Beeper and forwards appservice traffic to a local HTTP listener — no inbound port, no public hostname needed.

beeper-api-bridge runs two listeners, deliberately separate:

| Listener | Default bind | Purpose |
|---|---|---|
| appservice | `127.0.0.1:29337` | receives transactions from `bbctl proxy` — **keep it on loopback** |
| api | `127.0.0.1:29338` | `POST /1/messages.json`, `POST /1/users/validate.json` — the one senders talk to |

Both answer `GET /healthz`.

## Requirements

- A Beeper account with self-hosted bridges enabled (`bbctl` logged in).
- Python 3.9+ and PyYAML (installed in step 0 below).
- Somewhere always-on to run it: a small VPS or a LAN box. Only outbound connectivity to Beeper is required.

## Quick install (Debian / Ubuntu)

The installer does everything in the manual steps below: prerequisites, bbctl if you don't have it, the bridge under
`/opt/beeper-api-bridge`, the Beeper registration, `config.json` with freshly minted tokens, two systemd units and a test
notification. Re-run it later to **update**, **add an application** or **show the tokens**.

```sh
sudo apt install -y curl
bash <(curl -fsSL https://raw.githubusercontent.com/dannyleeuk/beeper-api-bridge/main/install.sh)
```

Prefer to read it first: `curl -fsSLO https://raw.githubusercontent.com/dannyleeuk/beeper-api-bridge/main/install.sh`,
then `sudo bash install.sh`. Options: `--no-systemd` (you run the two processes yourself, e.g. in tmux; the script prints
the commands), `--user NAME`, `--dir DIR`, `--ref main` (development branch), `--update`, `--yes`.

**Already running bbctl?** If the user you `sudo` from has a bbctl login (`~/.config/bbctl`), the installer offers to run
the bridge as that user and reuse the login, so there is no second `bbctl login`. bbctl and the bridge do not need to share
a directory, only a host and a user: bbctl is one binary (the installer looks on `PATH`, `~/bbctl/`, `~/.local/bin`, and
otherwise downloads the latest release to `/usr/local/bin`), and its login lives in that user's `~/.config/bbctl`. The
`bbctl proxy` for this bridge is separate from any `bbctl run …` sessions you already have; it can run in its own tmux
window just as well as under systemd.

## Manual setup

0. **Get the code** and the one dependency. Either clone the repo or grab a release archive from the Releases page:

   ```sh
   sudo apt install python3 python3-yaml           # Debian/Ubuntu; elsewhere: pip install pyyaml
   git clone https://github.com/dannyleeuk/beeper-api-bridge.git
   cd beeper-api-bridge
   ```

   The systemd units in `contrib/` expect the files under `/opt/beeper-api-bridge` owned by a `bridge` user; running from
   your home directory works just as well for trying it out. Update later with `git pull` and a restart.

1. **Register the appservice** with Beeper (once). The name becomes the bridge's identity on your account:

   ```sh
   bbctl login
   bbctl register -o registration.yaml sh-apibridge
   ```

   The name you register becomes the bridge's user namespace (`@sh-apibridge_<app>` ghosts and the `sh-apibridgebot`
   bot); the bridge reads it from the registration file, so pick any name. Without `--address`, Beeper expects the bridge to come to it over a websocket — that is what `bbctl proxy` does, and it
   forwards events to the `url:` in the registration file. Make sure that line reads `url: http://127.0.0.1:29337`
   (the appservice listener below); edit it if bbctl wrote something else. `bbctl register` also prints your homeserver
   URL and server name for `config.json`. Keep `registration.yaml` private (`chmod 600`): it holds the appservice tokens.

2. **Write `config.json`** from the example:

   ```sh
   cp config.example.json config.json && chmod 600 config.json
   ```

   | Key | Meaning |
   |---|---|
   | `registration_file` | path to the file from step 1 |
   | `homeserver` | your Beeper homeserver URL as printed by `bbctl` (e.g. `https://matrix.beeper.com`) |
   | `domain` | the Matrix server name for your account (e.g. `beeper.local`) |
   | `owner` | your own Matrix ID, the user the ghosts will chat with |
   | `user_key` | the value senders must pass as `user`. Any secret string works; the example shape (`u` + 29 letters/digits) matches Pushover's, which some senders validate before sending. Mint one: `python3 -c "import secrets,string; print('u'+''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(29)))"` |
   | `applications` | map of application token → `{"name": "…"}`; mint tokens the same way with `'a'` (the `X`s in the example are placeholders) |
   | `single_room` | optional room ID: post everything into this one chat (see *Rooms*) |
   | `ghost_prefix` | optional; normally derived from the registration's user namespace |
   | `appservice_bind` / `appservice_port`, `api_bind` / `api_port` | listeners; defaults are loopback |
   | `state_file` | where room IDs and ghost users are remembered (default `state.json` next to the script) |
   | `webhook` | optional, default `false`: enable the [text webhook](#text-webhook-slack-compatible) endpoint |
   | `rules_file` | optional: [mute rules and quiet hours](#mute-rules-and-quiet-hours) (JSON; edits apply without a restart) |
   | `log_file` / `log_max_entries` | optional: the [delivery log](#delivery-log) and how many entries it keeps (default 5000) |

3. **Start the bridge, then the proxy** (see `contrib/` for systemd units that do this in the right order):

   ```sh
   python3 beeper_api_bridge.py --check    # loads config, authenticates to Beeper, exits
   python3 beeper_api_bridge.py            # foreground
   bbctl proxy -r registration.yaml        # in another shell / unit
   ```

4. **Send a test**:

   ```sh
   curl -sS -X POST http://127.0.0.1:29338/1/messages.json \
     --data-urlencode "token=<application token>" \
     --data-urlencode "user=<user_key>" \
     --data-urlencode "title=Hello" \
     --data-urlencode "message=first notification" \
     --data-urlencode "priority=1"
   ```

   The first message for an application creates its chat and invites you; Beeper shows it as a message request and the
   bridge joins you automatically.

## Pointing senders at it

Change only the base URL; the request format is exactly Pushover's. Supported parameters: `token`, `user`, `message`, `title`,
`priority` (-2…2), `url`, `url_title`, `html`. Form-encoded (what real Pushover clients send) and JSON bodies are accepted.
`token` and `user` (and `title`, `priority`) may also be given on the **query string**, for senders whose body you cannot
shape: `http://bridge:29338/1/messages.json?token=<token>&user=<user_key>`. Tools that refuse a custom Pushover host (some
only accept `api.pushover.net`) need a webhook / HTTP action instead — every recipe below uses one.

In the recipes, `BRIDGE` is the address the api listener is bound to (see *Reaching it from other machines*), `TOKEN` the
application's token and `USER` the user key from `config.json`.

### Uptime Kuma
Settings → Notifications → *Setup Notification* → type **Webhook**:
- Post URL: `http://BRIDGE:29338/1/messages.json?token=TOKEN&user=USER`
- Request Body: *Preset - application/json*

That's all: the bridge understands Kuma's own webhook body and posts "Uptime Kuma: *monitor* 🔴 Down / ✅ Up" with Kuma's
message. (Kuma's built-in *Pushover* type cannot be pointed at another host.)

### Grafana (unified alerting)
Alerting → Contact points → *Add contact point* → integration **Webhook**:
- URL: `http://BRIDGE:29338/1/messages.json?token=TOKEN&user=USER`
- HTTP method: POST. Leave the payload as the default: Grafana's webhook JSON already carries `title` and `message`.

For nicer text, set *Title* and *Message* on the contact point (Optional Webhook settings) — they are what appears in Beeper.
Grafana's *Pushover* integration has the API host hard-coded, so use Webhook.

### Proxmox VE / Proxmox Backup Server
Datacenter (or Configuration) → Notifications → *Notification Targets* → Add → **Webhook**:
- Method: POST, URL: `http://BRIDGE:29338/1/messages.json?token=TOKEN&user=USER`
- Header: `Content-Type: application/json`
- Body:
  ```
  {"title": "{{ title }}", "message": "{{ escape message }}", "priority": {{#if (eq severity "error")}}1{{else}}0{{/if}}}
  ```
  If your version rejects the `#if` helper, use `"priority": 0`. Then add a *Notification Matcher* that routes to this target.

### CrowdSec
`/etc/crowdsec/notifications/http.yaml` (the `http` plugin):
```yaml
type: http
name: http_default
log_level: info
url: http://BRIDGE:29338/1/messages.json
method: POST
headers: {"Content-Type": "application/json"}
format: |
  {"token": "TOKEN", "user": "USER", "title": "CrowdSec: ban", "priority": 1,
   "message": "{{ range . }}{{ .Source.Scope }} {{ .Source.Value }}{{ if .Source.Cn }} ({{ .Source.Cn }}){{ end }} - {{ .Scenario }}\n{{ end }}"}
group_wait: 60s
group_threshold: 10
```
Then add `http_default` under `notifications:` in `profiles.yaml` and `systemctl reload crowdsec`. Test with
`cscli notifications test http_default`.

### Home Assistant
`configuration.yaml`:
```yaml
rest_command:
  beeper_notify:
    url: http://BRIDGE:29338/1/messages.json
    method: POST
    content_type: application/x-www-form-urlencoded
    payload: "token=TOKEN&user=USER&title={{ title | urlencode }}&message={{ message | urlencode }}"
```
Call `rest_command.beeper_notify` from an automation with `title` and `message` fields.

### Shell, cron, anything
```sh
curl -sS -X POST http://BRIDGE:29338/1/messages.json \
  --data-urlencode "token=TOKEN" --data-urlencode "user=USER" \
  --data-urlencode "title=Backup finished" --data-urlencode "message=$(hostname): 12.3 GB in 4m" --data-urlencode "priority=0"
```
Python: `urllib.request.urlopen("http://BRIDGE:29338/1/messages.json", urllib.parse.urlencode({...}).encode())`.

### Anything with a Pushover integration that allows a custom host
Set the host/base URL to `http://BRIDGE:29338` and fill in token and user key as usual.

### Reaching it from other machines

Both listeners are loopback-bound by default; nothing needs an inbound firewall rule. To accept notifications from
elsewhere, set `api_bind` to a VPN or tunnel interface address rather than `0.0.0.0`, or put it behind a reverse
proxy that adds authentication (a Cloudflare Access service token, Tailscale, WireGuard…). The application token and
user key are the only credentials the API checks, and they travel in clear text unless the transport is encrypted.

## Rooms

- Default: one chat per application, created on its first message. The chat is owned by that application's ghost.
- `single_room`: set it to a room ID (copy it from the first chat the bridge created, or any room you own) and every
  application posts there, each as its own ghost. Handy when you want one searchable feed.
- `"own_room": true` on an application keeps its own chat even in single-room mode — for the noisy sender you want to mute.
- `state.json` remembers token → room. Delete an entry (or the file) to make the bridge create a fresh chat.

## How a message looks: plain text and HTML

Every notification is sent to Matrix twice over, in one event: a plain-text `body` and an HTML `formatted_body`.
Beeper (like most Matrix clients) **displays the HTML one**; the plain one is the fallback for clients that cannot.

| Field you send | In the chat |
|---|---|
| `title` | **bold**, first line |
| `priority` 1 / 2 | `[HIGH]` / `[EMERGENCY]` in front of the title (-1 and -2 are marked `[low]` / `[lowest]`) |
| `message` | below the title. **Line breaks are kept** (1.1.0-beta.3+) |
| `url`, `url_title` | a link on its own line |

- **Plain text is the default.** It is HTML-escaped, so `<`, `>` and `&` show literally and a sender cannot inject
  markup by accident; each newline becomes a `<br/>`. Before 1.1.0-beta.3 newlines were left as-is, which HTML treats
  as spaces, so every multi-line message arrived as one run-on paragraph.
- **`html=1`** (Pushover's flag) passes `message` through as HTML: use `<b>`, `<i>`, `<a href>` and `<br/>` yourself;
  newlines are not converted. Only send HTML you control.
- **Emoji**: send real Unicode emoji (🚨 ✅ ⚠️ 🔧). Through the API, `:shortcodes:` and Markdown (`*bold*`,
  backticks) are shown literally - neither is interpreted. The [text webhook](#text-webhook-slack-compatible) is the
  exception: it converts both, because that is the format its senders write.
- Pushover's other formatting options (`monospace=1`, `sound`, `ttl`…) are accepted and ignored.

## Text webhook (Slack-compatible)

Off unless `"webhook": true` is set. Some tools can only send notifications as a Slack incoming webhook (Prometheus
Alertmanager's Slack receiver, many CI systems, anything built on a "Slack webhook" option). Point them at:

```
http://BRIDGE:29338/webhook/<application token>/<user_key>
```

- The body is the incoming-webhook format: JSON with `text`, optionally `blocks` (header, section, context) and
  `attachments` (pretext, title and title link, text, fields, footer, fallback), or the same JSON in a form field named
  `payload`. When `blocks` are present, `text` is treated as the notification fallback and not shown, as senders
  expect. Colours, icons and user names have no equivalent in a Beeper chat and are ignored.
- Formatting is converted: `*bold*`, `_italic_`, `~strike~`, `` `code` ``, code blocks, `<url|label>` links, and
  common `:shortcodes:` become emoji (an unknown shortcode is left as written). A header block, or an attachment's
  title, becomes the message title; otherwise the application's name is used.
- There is no priority in that format; add `?priority=-2..2` to the URL if you want one.
- Replies follow the format too: `200 ok`, `403 invalid_token`, `400 invalid_payload` or `400 no_text`.
- **The URL contains both secrets** (the application token and the user key the API also checks), so treat it like a
  password and keep it off public places.

## Mute rules and quiet hours

Off unless `rules_file` points at a JSON file. It applies to both the API and the webhook, is re-read whenever it
changes (no restart), and **fails open**: a missing, unreadable or invalid file, or a rule with a broken pattern, means
nothing is muted - a notification is never dropped because of a mistake here. A muted message is still acknowledged
as a success to its sender, so senders do not retry.

```json
{
  "timezone": "Europe/Berlin",
  "mute": [
    {"app": "Uptime Kuma", "match": "\\bUp\\b", "reason": "recoveries are noise"},
    {"app": "Backups", "until": "2026-10-01T09:00", "reason": "migration in progress"}
  ],
  "quiet_hours": {"start": "23:00", "end": "07:00", "max_priority": 0}
}
```

- A **mute rule** matches when every field it has matches: `app` (the application's name, any case), `match` (a regular
  expression over the title and message, any case), `max_priority` (the rule only covers messages at or below this;
  default `0`, so `[HIGH]` still gets through) and `until` (the rule expires then; ISO date/time, in `timezone`).
- **Quiet hours** mute everything at or below `max_priority` (default `0`) between `start` and `end`, which may span
  midnight.
- **Emergencies (priority 2) are never muted** unless a rule or the quiet hours say `"include_emergency": true`.
- `timezone` is an IANA name; without it the host's local time is used. `reason` is what the delivery log shows.

## Delivery log

Off unless `log_file` is set. Every notification is recorded as one JSON line: when, which application, whether it
came through the API or the webhook, title, priority, the first 300 characters of the text, and the outcome - `sent`,
`muted` (with the rule that muted it), `rejected` (unknown token, wrong user key, empty or unreadable body) or `failed`
(with the error). The file is private (mode 600) and keeps the last `log_max_entries` entries. A log that cannot be
written is reported once and never stops a notification.

```sh
python3 beeper_api_bridge.py --log               # the last 50 entries
python3 beeper_api_bridge.py --log 200 --outcome muted
python3 beeper_api_bridge.py --log --json        # raw JSON lines, e.g. for jq
```

`--log` only reads the file; it does not contact Beeper or disturb a running bridge.

## Operations

- `--check` validates the config and the homeserver login without serving.
- A client dropping an idle keep-alive connection (bbctl's proxy does this constantly) is logged at DEBUG only, since
  1.1.0-beta.3; before, each one printed a `ConnectionResetError` traceback to the journal.
- `BEEPER_API_BRIDGE_LOGLEVEL=DEBUG` logs every request; `BEEPER_API_BRIDGE_CONFIG` points at the config (or use `--config`).
- `contrib/beeper-api-bridge.service` and `contrib/beeper-api-bridge-proxy.service`: hardened systemd units; the proxy is
  `BindsTo=` the bridge and waits for its `/healthz`, so the pair always comes up in the right order.
- `Dockerfile` builds a minimal image; mount `config.json`, `registration.yaml` and a writable `state.json` in `/data`.
- Rotate a token by changing it in `config.json` and restarting; the room mapping is keyed by token, so also move the
  entry in `state.json` if you want to keep the same chat.

## Running the tests

`tests/test_bridge.py` starts the bridge's API against a fake Matrix homeserver (built into the test, so no network,
no Beeper account and no `bbctl` are needed) and checks exactly what would be posted: token and user-key checks, the
validate endpoint, room creation and the priority label, plain-text escaping and line breaks, `html=1`, the Uptime Kuma
and Grafana webhook bodies, query-string credentials, and the server's handling of dropped connections.

```sh
pip install pyyaml                       # or: sudo apt install python3-yaml
python3 -m unittest discover -s tests    # from the repository root; add -v to list each test
python3 tests/test_bridge.py             # the same, run directly
```

They also run on every push and pull request, on Python 3.9 and 3.12.

## What it does not do

- No receipt / emergency-retry semantics (`priority=2` is labelled EMERGENCY but not re-sent), no `sound`, `device`,
  `attachment` or `timestamp` handling; those parameters are accepted and ignored.
- Send-only: messages you type in the chat go nowhere. The appservice acknowledges and discards transactions.
- One Beeper account per instance.

## Security notes

- Keep `registration.yaml`, `config.json` and `state.json` mode 600; they hold the appservice tokens and your API credentials.
- The appservice listener must never be reachable from outside the host: it trusts the `hs_token` only.
- Minted tokens should be long and random (the examples above use 30+ characters of `secrets` output).

## Relationship to Pushover

None. beeper-api-bridge never contacts pushover.net and needs no Pushover account; it implements the request and
response shapes of Pushover's public message API so that existing clients work unchanged, the way an S3-compatible object store
implements Amazon's API. "Pushover" is a trademark of its owner; it is used here only to describe compatibility. This
project is not affiliated with, endorsed by or supported by Pushover. If you want push notifications on your phone from
Pushover's own apps, buy Pushover — it is excellent and this is not a replacement for it.

## Relationship to Slack

None. beeper-api-bridge never contacts slack.com and needs no Slack workspace or app; it implements the request and response shapes of Slack's incoming webhooks so that existing senders work unchanged, the way an S3-compatible object store implements Amazon's API. "Slack" is a trademark of its owner; it is used here only to describe compatibility. This project is not affiliated with, endorsed by or supported by Slack. If you want a workspace for your team's conversations, use Slack itself — this is not a replacement for it.

## Licence

MIT — see `LICENSE`. Provided as-is; issues and pull requests are welcome, but there is no support commitment.
