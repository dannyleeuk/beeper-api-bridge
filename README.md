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
- Python 3.9+ and PyYAML (`apt install python3-yaml` or `pip install pyyaml`).
- Somewhere always-on to run it: a small VPS or a LAN box. Only outbound connectivity to Beeper is required.

## Setup

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
   | `user_key` | the value senders must pass as `user` — make one up: `python3 -c "import secrets; print('u'+secrets.token_urlsafe(22))"` |
   | `applications` | map of application token → `{"name": "…"}`; mint tokens the same way (`'a'+…`) |
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

- **Grafana**: contact point type *Webhook* is easier than the Pushover type here, because Grafana's Pushover contact point
  has the API host hard-coded. POST JSON `{"token":…,"user":…,"title":…,"message":…}` to `/1/messages.json`.
- **Uptime Kuma**, **Proxmox**, **CrowdSec** (`notification-http` plugin), **Home Assistant** (`rest_command`) and most
  tools with a Pushover or generic webhook target work by changing the URL.
- Tools that refuse a custom Pushover host (some only accept `api.pushover.net`) need a webhook / HTTP action instead.

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
