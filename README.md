# beeper-api-bridge — a notification API for Beeper

A tiny, dependency-light [Matrix appservice](https://spec.matrix.org/latest/application-service-api/) that gives your
**Beeper** account an HTTP notification endpoint. It speaks the same API as [Pushover](https://pushover.net/api), so
anything that can already send Pushover notifications can be pointed at it instead of `api.pushover.net`, and the
messages arrive in your Beeper chats. It is its own bridge, not a Pushover client: nothing here talks to Pushover.

Almost every self-hosted tool has a Pushover integration built in — Grafana, Uptime Kuma, Proxmox VE / Backup Server,
CrowdSec, Home Assistant, Gotify-style scripts, plain `curl` — so you get chat notifications for all of them without
writing an adapter for each. No Pushover account, subscription or licence is involved: the tokens are minted by you.

```
Grafana / Uptime Kuma / Proxmox / cron …  ──POST /1/messages.json──▶  beeper-api-bridge  ──▶  Beeper (Matrix)
```

- One chat per application token, each posting as its own ghost user, so a message shows as coming from "Uptime Kuma"
  rather than from you. Or set `single_room` and everything lands in one chat (still labelled per sender).
- Priorities (-2…2, the Pushover scale) become a `[low]` / `[HIGH]` / `[EMERGENCY]` prefix; `url` / `url_title` become a link; `html=1` is honoured.
- Unknown tokens and bad user keys are refused with Pushover's own `{"status":0,"errors":[…]}` shape, so senders
  surface the error normally.
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

## Operations

- `--check` validates the config and the homeserver login without serving.
- `BEEPER_API_BRIDGE_LOGLEVEL=DEBUG` logs every request; `BEEPER_API_BRIDGE_CONFIG` points at the config (or use `--config`).
- `contrib/beeper-api-bridge.service` and `contrib/beeper-api-bridge-proxy.service`: hardened systemd units; the proxy is
  `BindsTo=` the bridge and waits for its `/healthz`, so the pair always comes up in the right order.
- `Dockerfile` builds a minimal image; mount `config.json`, `registration.yaml` and a writable `state.json` in `/data`.
- Rotate a token by changing it in `config.json` and restarting; the room mapping is keyed by token, so also move the
  entry in `state.json` if you want to keep the same chat.

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

## Licence

MIT — see `LICENSE`. Provided as-is; issues and pull requests are welcome, but there is no support commitment.
