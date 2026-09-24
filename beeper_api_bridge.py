#!/usr/bin/env python3
"""
beeper-api-bridge: a notification API for Beeper, run as a self-hosted Matrix appservice.

It speaks the same HTTP API as Pushover, so anything that can send Pushover notifications (Grafana, Uptime Kuma, Proxmox,
CrowdSec, scripts...) can be pointed at this instead and the messages land in your Beeper chats. It never talks to
Pushover and needs no account there; the compatibility is only so existing senders work unchanged.

Two listeners, deliberately separate:

  * appservice listener  - loopback only, receives transactions pushed by
    `bbctl proxy`. Never bind this anywhere but 127.0.0.1.
  * api listener         - the Pushover-compatible endpoint senders talk to
    (POST /1/messages.json), so its bind address is configurable.

One Beeper chat per application token; each chat is owned by its own ghost user
so notifications arrive from "Uptime Kuma" rather than from you.
"""

import html
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

__version__ = "1.1.0-beta.3"

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("BEEPER_API_BRIDGE_CONFIG", os.path.join(BASE, "config.json"))

log = logging.getLogger("beeper-api-bridge")

# Priority -2..2 -> human label. -2/-1 are quieter than normal, 1/2 louder.
PRIORITY_LABEL = {-2: "lowest", -1: "low", 0: None, 1: "HIGH", 2: "EMERGENCY"}


def _slug(value: str) -> str:
    """Reduce an arbitrary label to something valid in a Matrix localpart."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "app"


def _prefix_from_registration(reg: dict) -> str:
    """'@sh-apibridge_.+:example' -> 'sh-apibridge'."""
    for ns in (reg.get("namespaces") or {}).get("users") or []:
        m = re.match(r"^@?\\?([A-Za-z0-9.=\-]+)_", str(ns.get("regex", "")))
        if m:
            return m.group(1)
    return ""


class Config:
    def __init__(self, path: str):
        with open(path) as fh:
            raw = json.load(fh)

        with open(raw["registration_file"]) as fh:
            reg = yaml.safe_load(fh)

        self.as_token: str = reg["as_token"]
        self.hs_token: str = reg["hs_token"]
        self.bot_localpart: str = reg["sender_localpart"]
        # Ghost users must live in the user namespace Beeper granted this bridge, which bbctl derives from the name you
        # registered: `bbctl register sh-apibridge` -> regex '@sh-apibridge_.+:…' -> ghosts @sh-apibridge_<app>. Read it
        # from the registration so the name is yours to choose; "ghost_prefix" in config.json overrides.
        self.ghost_prefix: str = raw.get("ghost_prefix") or _prefix_from_registration(reg) or self.bot_localpart.removesuffix("bot")

        self.homeserver: str = raw["homeserver"].rstrip("/")
        self.domain: str = raw["domain"]
        self.owner: str = raw["owner"]

        self.as_bind = (raw.get("appservice_bind", "127.0.0.1"), int(raw.get("appservice_port", 29337)))
        # (pushover_bind/pushover_port: the 1.0.x names of these keys, still accepted)
        self.api_bind = (raw.get("api_bind", raw.get("pushover_bind", "127.0.0.1")), int(raw.get("api_port", raw.get("pushover_port", 29338))))

        # user key senders must present (the API's `user` parameter)
        self.user_key: str = raw["user_key"]
        # token -> {"name": ..., "avatar": optional mxc://}
        self.apps: dict = raw["applications"]

        self.state_path: str = raw.get("state_file", os.path.join(BASE, "state.json"))
        # Optional: one chat for everything. Every application posts into this room, each still as its own ghost so the
        # sender name says where it came from. Unset = one room per application.
        self.single_room: str = raw.get("single_room", "")

    @property
    def bot(self) -> str:
        return f"@{self.bot_localpart}:{self.domain}"


class Matrix:
    """Minimal appservice client. Masquerades as ghosts via ?user_id=."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._state = self._load_state()

    # ---- state ---------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(self.cfg.state_path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"rooms": {}, "ghosts": []}

    def _save_state(self) -> None:
        tmp = self.cfg.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._state, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.cfg.state_path)

    # ---- transport -----------------------------------------------------
    def _request(self, method: str, path: str, body=None, user_id=None):
        url = self.cfg.homeserver + path
        if user_id:
            sep = "&" if "?" in path else "?"
            url += sep + urllib.parse.urlencode({"user_id": user_id})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.cfg.as_token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, OSError) as exc:
            return 0, {"error": str(exc)}

    # ---- ghosts and rooms ----------------------------------------------
    def ensure_ghost(self, localpart: str, displayname: str) -> str:
        mxid = f"@{localpart}:{self.cfg.domain}"
        if mxid in self._state["ghosts"]:
            return mxid
        status, body = self._request(
            "POST", "/_matrix/client/v3/register",
            {"type": "m.login.application_service", "username": localpart},
        )
        # M_USER_IN_USE just means we already made it on a previous run.
        if status != 200 and body.get("errcode") != "M_USER_IN_USE":
            raise RuntimeError(f"ghost register failed: {status} {body}")
        self._request(
            "PUT", f"/_matrix/client/v3/profile/{urllib.parse.quote(mxid)}/displayname",
            {"displayname": displayname}, user_id=mxid,
        )
        with self._lock:
            self._state["ghosts"].append(mxid)
            self._save_state()
        return mxid

    def ensure_room(self, token: str, app: dict) -> tuple:
        name = app.get("name", "Notifications")
        ghost = self.ensure_ghost(f"{self.cfg.ghost_prefix}_{_slug(name)}", name)

        # an application with "own_room": true keeps its own chat even in single-room mode (mute the noisy ones)
        room_id = (self.cfg.single_room if not app.get("own_room") else None) or self._state["rooms"].get(token)
        if room_id:
            self._ensure_member(room_id, ghost)
            return room_id, ghost

        status, body = self._request(
            "POST", "/_matrix/client/v3/createRoom",
            {
                "name": name,
                "topic": f"Notifications from {name}",
                "invite": [self.cfg.owner],
                "is_direct": True,
                "initial_state": [{
                    "type": "m.bridge",
                    "state_key": f"notify://{_slug(name)}",
                    "content": {
                        "bridgebot": self.cfg.bot,
                        "protocol": {"id": "notifications", "displayname": "Notifications"},
                        "channel": {"id": _slug(name), "displayname": name},
                    },
                }],
            },
            user_id=ghost,
        )
        if status != 200:
            raise RuntimeError(f"createRoom failed: {status} {body}")
        room_id = body["room_id"]
        # Beeper records the owner's "accept message request" but never completes the join, so messages would sit in a
        # room the owner was only invited to. Beeper's homeserver lets an appservice act as the owner: join them now.
        status, jbody = self._request("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join", {}, user_id=self.cfg.owner)
        if status != 200:
            log.warning("could not join owner to %s: %s %s", room_id, status, jbody)
        with self._lock:
            self._state["rooms"][token] = room_id
            self._state.setdefault("creators", {})[room_id] = [ghost]
            self._state.setdefault("members", {})[room_id] = [ghost]
            self._save_state()
        log.info("created room %s for application %r (owner joined: %s)", room_id, name, status == 200)
        return room_id, ghost

    def _ensure_member(self, room_id: str, ghost: str) -> None:
        """Ghosts other than the room's creator must be invited (by the bot, which owns the room) and then join."""
        joined = self._state.setdefault("members", {}).setdefault(room_id, [])
        if ghost in joined:
            return
        creator = next(iter(self._state.get("creators", {}).get(room_id, [])), None) or self.cfg.bot
        self._request("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite", {"user_id": ghost}, user_id=creator)
        status, body = self._request("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join", {}, user_id=ghost)
        if status != 200:
            raise RuntimeError(f"ghost {ghost} could not join {room_id}: {status} {body}")
        with self._lock:
            joined.append(ghost)
            self._save_state()

    def send_notification(self, token: str, app: dict, msg: dict) -> str:
        room_id, ghost = self.ensure_room(token, app)

        title = msg.get("title") or app.get("name", "Notification")
        text = msg.get("message", "")
        priority = msg.get("priority", 0)
        url = msg.get("url")
        url_title = msg.get("url_title") or url

        plain = [f"{title}", text] if title else [text]
        label = PRIORITY_LABEL.get(priority)
        if label:
            plain.insert(0, f"[{label}]")
        if url:
            plain.append(url)

        # msg["html"]=1 means the sender supplied HTML
        # Plain text: escape it and turn its line breaks into <br/>. In HTML a newline is only whitespace, and clients
        # render formatted_body, so multi-line messages used to arrive as one run-on paragraph.
        body_html = text if msg.get("html") else html.escape(text).replace("\n", "<br/>")
        parts = [f"<strong>{html.escape(title)}</strong>"]
        if label:
            parts[0] = f"<strong>[{label}] {html.escape(title)}</strong>"
        parts.append(body_html)
        if url:
            parts.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(url_title)}</a>')

        content = {
            "msgtype": "m.text",
            "body": "\n".join(p for p in plain if p),
            "format": "org.matrix.custom.html",
            "formatted_body": "<br/>".join(parts),
        }
        txn = uuid.uuid4().hex
        status, body = self._request(
            "PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.room.message/{txn}",
            content, user_id=ghost,
        )
        if status != 200:
            raise RuntimeError(f"send failed: {status} {body}")
        return body["event_id"]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg: Config = None
    matrix: Matrix = None
    role: str = "api"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _reply(self, status: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---- request parsing ------------------------------------------------
    def _parse_params(self) -> dict:
        """Senders post form-encoded (the API's convention); accept JSON too. `token` and `user` may also come on the
        query string (?token=…&user=…) for senders whose body you cannot shape (Grafana's and Uptime Kuma's stock
        webhooks). Uptime Kuma's webhook body (msg / monitor / heartbeat) is mapped onto message / title."""
        raw = self._read_body()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        params = {}
        if ctype == "application/json":
            try:
                params = json.loads(raw or b"{}")
            except ValueError:
                params = {}
            if not isinstance(params, dict):
                params = {}
        else:
            parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            params = {k: v[0] for k, v in parsed.items()}
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        for k in ("token", "user", "title", "priority"):
            if k in qs and not params.get(k):
                params[k] = qs[k][0]
        if not params.get("message") and params.get("msg"):            # Uptime Kuma webhook shape
            mon = params.get("monitor") or {}
            hb = params.get("heartbeat") or {}
            name = mon.get("name") if isinstance(mon, dict) else None
            status = {0: "🔴 Down", 1: "✅ Up", 2: "⏸ Paused", 3: "🟡 Maintenance"}.get(hb.get("status") if isinstance(hb, dict) else None, "")
            params["message"] = str(params["msg"])
            params.setdefault("title", " ".join(x for x in ("Uptime Kuma:", name, status) if x))
        return params

    # ---- routing --------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self._reply(200, {"status": "ok", "role": self.role})
        if self.role == "appservice":
            # Beeper queries these for users/aliases in our namespace.
            if path.startswith("/_matrix/app/v1/users/"):
                return self._appservice_guard() or self._reply(200, {})
            if path.startswith("/_matrix/app/v1/rooms/"):
                return self._appservice_guard() or self._reply(404, {"errcode": "M_NOT_FOUND"})
        self._reply(404, {"errcode": "M_NOT_FOUND"})

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if self.role == "appservice" and "/_matrix/app/v1/transactions/" in path:
            if self._appservice_guard():
                return
            self._read_body()  # send-only bridge: acknowledge and discard
            return self._reply(200, {})
        self._reply(404, {"errcode": "M_NOT_FOUND"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self.role != "api":
            return self._reply(404, {"errcode": "M_NOT_FOUND"})
        if path == "/1/messages.json":
            return self._handle_message()
        if path in ("/1/users/validate.json", "/1/users/validate"):
            return self._handle_validate()
        self._reply(404, {"status": 0, "errors": ["not found"], "request": uuid.uuid4().hex})

    # ---- appservice auth -------------------------------------------------
    def _appservice_guard(self):
        """Returns a response on failure, None when the hs_token checks out."""
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else \
            urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("access_token", [""])[0]
        if not secrets.compare_digest(token, self.cfg.hs_token):
            self._reply(403, {"errcode": "M_FORBIDDEN"})
            return True
        return None

    # ---- notification API ---------------------------------------------------
    def _api_error(self, errors):
        """Error reply in the shape senders expect: {"status": 0, "errors": [...], "request": id}."""
        self._reply(400, {"status": 0, "errors": errors, "request": uuid.uuid4().hex})

    def _handle_validate(self):
        params = self._parse_params()
        if params.get("token") not in self.cfg.apps:
            return self._api_error(["application token is invalid"])
        if not secrets.compare_digest(params.get("user", ""), self.cfg.user_key):
            return self._api_error(["user identifier is not a valid user key"])
        self._reply(200, {"status": 1, "request": uuid.uuid4().hex, "devices": ["beeper"]})

    def _handle_message(self):
        params = self._parse_params()
        token = params.get("token", "")
        app = self.cfg.apps.get(token)
        if app is None:
            log.warning("rejected unknown application token from %s", self.address_string())
            return self._api_error(["application token is invalid"])
        if not secrets.compare_digest(params.get("user", ""), self.cfg.user_key):
            log.warning("rejected bad user key from %s", self.address_string())
            return self._api_error(["user identifier is not a valid user key"])
        if not params.get("message"):
            return self._api_error(["message cannot be blank"])

        try:
            priority = int(params.get("priority", 0))
        except ValueError:
            priority = 0

        msg = {
            "message": params.get("message", ""),
            "title": params.get("title"),
            "priority": max(-2, min(2, priority)),
            "url": params.get("url"),
            "url_title": params.get("url_title"),
            "html": params.get("html") in ("1", 1, True),
        }
        try:
            event_id = self.matrix.send_notification(token, app, msg)
        except Exception:
            log.exception("failed to relay notification for %r", app.get("name"))
            return self._reply(500, {"status": 0, "errors": ["delivery to matrix failed"],
                                     "request": uuid.uuid4().hex})
        log.info("relayed %r -> %s (%s)", app.get("name"), event_id, msg["title"] or "-")
        self._reply(200, {"status": 1, "request": uuid.uuid4().hex})


class _Server(ThreadingHTTPServer):
    """A client hanging up is routine, not an error. The handler speaks HTTP/1.1, so after answering a request it waits
    on the kept-alive connection for the next one; bbctl's appservice proxy (and some senders) then drop that idle
    connection. socketserver's default handle_error prints a full traceback for every such hang-up - hundreds a week,
    burying any real error in the journal. Those are logged at debug level; anything else still gets the traceback."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            log.debug("client %s:%s closed the connection (%s)", *client_address[:2], exc.__class__.__name__)
            return
        super().handle_error(request, client_address)


def _serve(bind, role, cfg, matrix):
    handler = type(f"{role}Handler", (_Handler,), {"cfg": cfg, "matrix": matrix, "role": role})
    server = _Server(bind, handler)
    server.daemon_threads = True
    log.info("%s listener on http://%s:%d", role, bind[0], bind[1])
    threading.Thread(target=server.serve_forever, daemon=True, name=role).start()
    return server


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="beeper_api_bridge", description="beeper-api-bridge: a Pushover-compatible notification API that delivers into Beeper (Matrix).")
    ap.add_argument("-c", "--config", default=CONFIG_PATH, help="config.json path (default: $BEEPER_API_BRIDGE_CONFIG or next to the script)")
    ap.add_argument("--check", action="store_true", help="load the config, authenticate to the homeserver, then exit")
    ap.add_argument("--version", action="version", version=f"beeper-api-bridge {__version__}")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("BEEPER_API_BRIDGE_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        cfg = Config(args.config)
    except (OSError, KeyError, ValueError) as exc:
        log.error("bad config %s: %s", args.config, exc)
        return 2
    matrix = Matrix(cfg)

    status, body = matrix._request("GET", "/_matrix/client/v3/account/whoami", user_id=cfg.bot)
    if status != 200:
        log.error("cannot reach homeserver as %s: %s %s", cfg.bot, status, body)
        return 1
    log.info("authenticated as %s", body.get("user_id"))
    if args.check:
        log.info("config OK: %d application token(s), api listener %s:%d", len(cfg.apps), *cfg.api_bind)
        return 0

    _serve(cfg.as_bind, "appservice", cfg, matrix)
    _serve(cfg.api_bind, "api", cfg, matrix)
    log.info("beeper-api-bridge %s ready; %d application token(s) configured", __version__, len(cfg.apps))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
