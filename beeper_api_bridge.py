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

__version__ = "1.2.0-beta.1"

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

        # 1.2 additions. Every one is optional and off when absent, so a 1.1 config.json behaves exactly as before.
        self.webhook: bool = bool(raw.get("webhook", False))          # POST /webhook/<token>/<user_key> (text webhook)
        self.rules_file: str = raw.get("rules_file", "")               # mute rules + quiet hours (JSON, re-read on change)
        self.log_file: str = raw.get("log_file", "")                   # delivery log (JSON lines); empty = no log
        self.log_max: int = max(100, int(raw.get("log_max_entries", 5000)))

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

        # msg["plain"] is set only by the text webhook, whose `message` is already HTML; everything else is unchanged.
        plain_text = msg["plain"] if msg.get("plain") is not None else text
        plain = [f"{title}", plain_text] if title else [plain_text]
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


# ---- 1.2: delivery log ------------------------------------------------------------------------------------------------
class DeliveryLog:
    """What happened to every notification: sent, muted (and by what), rejected (and why) or failed. One JSON object per
    line in `log_file`, capped at `log_max_entries`. Off when no file is configured. Writing it can never stop a
    notification: any error here is logged once and ignored."""

    def __init__(self, path: str = "", max_entries: int = 5000):
        self.path, self.max = path, max_entries
        self._lock = threading.Lock()
        self._count = 0
        self._warned = False
        if path and os.path.exists(path):
            try:
                with open(path) as fh:
                    self._count = sum(1 for _ in fh)
            except OSError:
                pass

    def record(self, **entry) -> None:
        if not self.path:
            return
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
        try:
            with self._lock:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._count += 1
                if self._count > self.max * 1.2:           # trim in batches, not on every write
                    with open(self.path) as fh:
                        keep = fh.readlines()[-self.max:]
                    tmp = self.path + ".tmp"
                    with open(tmp, "w") as fh:
                        fh.writelines(keep)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, self.path)
                    self._count = len(keep)
        except OSError as exc:
            if not self._warned:
                log.warning("delivery log %s not writable (%s); notifications are unaffected", self.path, exc)
                self._warned = True

    @staticmethod
    def read(path: str, limit: int = 50, outcome: str = "") -> list:
        entries = []
        with open(path) as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not outcome or e.get("outcome") == outcome:
                    entries.append(e)
        return entries[-limit:]


# ---- 1.2: mute rules and quiet hours ----------------------------------------------------------------------------------
class Rules:
    """Mute rules and quiet hours from `rules_file` (JSON). The file is re-read when it changes, so edits need no
    restart. It fails open: no file, an unreadable or invalid file, or a broken rule all mean "send". An emergency
    (priority 2) is never muted unless a rule says so explicitly.

        {"timezone": "Europe/Berlin",
         "mute": [{"app": "Uptime Kuma", "match": "\\\\bUp\\\\b", "max_priority": 0,
                   "until": "2026-10-01T09:00", "reason": "noisy during a migration"}],
         "quiet_hours": {"start": "23:00", "end": "07:00", "max_priority": 0}}
    """

    def __init__(self, path: str = ""):
        self.path = path
        self._mtime = None
        self._data: dict = {}
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path:
            return {}
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            return {}                                          # no file: nothing muted
        with self._lock:
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    with open(self.path) as fh:
                        data = json.load(fh)
                    if not isinstance(data, dict):
                        raise ValueError("top level must be an object")
                    self._data = data
                    log.info("rules loaded from %s: %d mute rule(s), quiet hours %s", self.path,
                             len(data.get("mute") or []), "on" if data.get("quiet_hours") else "off")
                except (OSError, ValueError) as exc:
                    self._data = {}
                    log.warning("rules file %s ignored (%s): nothing will be muted until it is fixed", self.path, exc)
            return self._data

    def _tz(self, data: dict):
        name = data.get("timezone")
        if not name:
            return None                                        # local time of the host
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:                                      # noqa: BLE001 - unknown zone / no tz database
            log.warning("rules: unknown timezone %r, using the host's local time", name)
            return None

    @staticmethod
    def _now(tz):
        import datetime as dt
        return dt.datetime.now(tz) if tz else dt.datetime.now().astimezone()

    def decide(self, app_name: str, title: str, message: str, priority: int, now=None):
        """(muted, why). Never raises."""
        try:
            return self._decide(app_name, title or "", message or "", priority, now)
        except Exception as exc:                               # noqa: BLE001 - fail open, whatever went wrong
            log.warning("rules: error while deciding (%s); sending", exc)
            return False, ""

    def _decide(self, app_name, title, message, priority, now):
        import datetime as dt
        data = self._load()
        if not data:
            return False, ""
        tz = self._tz(data)
        now = now or self._now(tz)
        text = f"{title}\n{message}"
        for n, rule in enumerate(data.get("mute") or [], 1):
            if not isinstance(rule, dict):
                continue
            if priority >= 2 and not rule.get("include_emergency"):
                continue
            if priority > int(rule.get("max_priority", 0)):
                continue
            if rule.get("app") and str(rule["app"]).lower() != (app_name or "").lower():
                continue
            if rule.get("until"):
                until = dt.datetime.fromisoformat(str(rule["until"]).replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=now.tzinfo)
                if now >= until:
                    continue                                   # expired
            if rule.get("match"):
                try:
                    if not re.search(rule["match"], text, re.I):
                        continue
                except re.error as exc:
                    log.warning("rules: mute rule %d has a bad pattern (%s) and is ignored", n, exc)
                    continue
            return True, f"mute rule {n}" + (f" ({rule['reason']})" if rule.get("reason") else "")
        qh = data.get("quiet_hours")
        if isinstance(qh, dict) and qh.get("start") and qh.get("end"):
            if priority >= 2 and not qh.get("include_emergency"):
                return False, ""
            if priority <= int(qh.get("max_priority", 0)):
                start = dt.time.fromisoformat(qh["start"])
                end = dt.time.fromisoformat(qh["end"])
                t = now.time().replace(tzinfo=None)
                inside = (start <= t < end) if start < end else (t >= start or t < end)   # may wrap midnight
                if inside:
                    return True, f"quiet hours {qh['start']}-{qh['end']}"
        return False, ""


# ---- 1.2: text webhook (the incoming-webhook format many tools know from Slack) ---------------------------------------
# Common :shortcodes: only - an unknown one is left as written. Senders that use them are usually alerting tools.
_SHORTCODES = {
    "rotating_light": "🚨", "warning": "⚠️", "white_check_mark": "✅", "heavy_check_mark": "✔️", "x": "❌",
    "negative_squared_cross_mark": "❎", "red_circle": "🔴", "large_red_circle": "🔴", "green_circle": "🟢",
    "large_green_circle": "🟢", "yellow_circle": "🟡", "large_yellow_circle": "🟡", "orange_circle": "🟠",
    "blue_circle": "🔵", "large_blue_circle": "🔵", "white_circle": "⚪", "black_circle": "⚫", "fire": "🔥",
    "boom": "💥", "bell": "🔔", "no_bell": "🔕", "information_source": "ℹ️", "question": "❓", "exclamation": "❗",
    "heavy_exclamation_mark": "❗", "bangbang": "‼️", "zap": "⚡", "rocket": "🚀", "tada": "🎉", "sparkles": "✨",
    "package": "📦", "key": "🔑", "lock": "🔒", "unlock": "🔓", "wrench": "🔧", "hammer": "🔨", "gear": "⚙️",
    "hourglass": "⌛", "hourglass_flowing_sand": "⏳", "stopwatch": "⏱️", "alarm_clock": "⏰", "clock1": "🕐",
    "calendar": "📅", "chart_with_upwards_trend": "📈", "chart_with_downwards_trend": "📉", "bar_chart": "📊",
    "mag": "🔍", "link": "🔗", "memo": "📝", "clipboard": "📋", "email": "📧", "inbox_tray": "📥",
    "outbox_tray": "📤", "floppy_disk": "💾", "cd": "💿", "computer": "💻", "desktop_computer": "🖥️",
    "globe_with_meridians": "🌐", "cloud": "☁️", "thermometer": "🌡️", "battery": "🔋", "electric_plug": "🔌",
    "bulb": "💡", "shield": "🛡️", "no_entry": "⛔", "no_entry_sign": "🚫", "stop_sign": "🛑", "construction": "🚧",
    "skull": "💀", "ghost": "👻", "robot_face": "🤖", "eyes": "👀", "wave": "👋", "+1": "👍", "thumbsup": "👍",
    "-1": "👎", "thumbsdown": "👎", "ok_hand": "👌", "clap": "👏", "pray": "🙏", "muscle": "💪",
    "heart": "❤️", "broken_heart": "💔", "star": "⭐", "arrow_up": "⬆️", "arrow_down": "⬇️",
    "arrows_counterclockwise": "🔄", "repeat": "🔁", "recycle": "♻️", "heavy_plus_sign": "➕",
    "heavy_minus_sign": "➖", "small_red_triangle": "🔺", "small_red_triangle_down": "🔻",
}
_SHORTCODE_RE = re.compile(r":([a-z0-9_+\-]+):")
_ANGLE_RE = re.compile(r"<([^<>\n]+)>")
_INLINE = [   # (pattern over already-escaped text, replacement). Markers must hug their text: "*.example *x*" is safe.
    (re.compile(r"```\n?(.+?)```", re.S), r"<pre>\1</pre>"),
    (re.compile(r"`([^`\n]+)`"), r"<code>\1</code>"),
    (re.compile(r"(?<![\w*])\*(?=\S)([^*\n]*?\S)\*(?![\w*])"), r"<b>\1</b>"),
    (re.compile(r"(?<![\w_])_(?=\S)([^_\n]*?\S)_(?![\w_])"), r"<i>\1</i>"),
    (re.compile(r"(?<![\w~])~(?=\S)([^~\n]*?\S)~(?![\w~])"), r"<s>\1</s>"),
]
_PLAIN_MARKERS = [(p, r"\1") for p, _ in _INLINE]


def _shortcodes(s: str) -> str:
    return _SHORTCODE_RE.sub(lambda m: _SHORTCODES.get(m.group(1), m.group(0)), s)


def _unescape(s: str) -> str:
    """The format escapes only these three; a sender writes &amp;lt; for a literal &lt;."""
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _format_text(s: str):
    """One piece of webhook text -> (plain, html). Handles <url|label> links, <@user>/<#channel>/<!here> tokens,
    *bold* _italic_ ~strike~ `code` ```blocks```, :shortcodes: and line breaks."""
    s = _shortcodes(s or "")
    plain, rich, pos = [], [], 0

    def add_text(chunk):
        chunk = _unescape(chunk)
        p, h = chunk, html.escape(chunk, quote=False)
        for pat, rep in _PLAIN_MARKERS:
            p = pat.sub(rep, p)
        for pat, rep in _INLINE:
            h = pat.sub(rep, h)
        plain.append(p)
        rich.append(h)

    for m in _ANGLE_RE.finditer(s):
        add_text(s[pos:m.start()])
        target, _, label = m.group(1).partition("|")
        target, label = _unescape(target), _unescape(label)
        if target.startswith(("http://", "https://", "mailto:")):
            shown = label or target
            plain.append(f"{label} ({target})" if label and label != target else target)
            rich.append(f'<a href="{html.escape(target, quote=True)}">{html.escape(shown)}</a>')
        elif target[:1] in "@#!":
            shown = label or {"!": "@"}.get(target[0], target[0]) + target[1:].split("^")[0]
            plain.append(shown)
            rich.append(html.escape(shown))
        else:
            add_text("<" + m.group(1) + ">")
        pos = m.end()
    add_text(s[pos:])
    return "".join(plain), "".join(rich).replace("\n", "<br/>")


def _webhook_message(payload: dict):
    """A webhook body -> (title or None, plain, html). Reads `blocks` (header / section / context) or, when they render
    nothing, `text` (as in Slack, `text` is only the fallback once blocks are present), then the legacy `attachments`
    (pretext, title + title_link, text, fields, footer; `fallback` when nothing else). Styling fields (colour, icons,
    username, unfurling) have no equivalent in a Beeper chat and are ignored."""
    title, plain, rich = None, [], []

    def add(s):
        if s and str(s).strip():
            p, h = _format_text(str(s))
            plain.append(p)
            rich.append(h)

    for b in payload.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        kind, t = b.get("type"), b.get("text")
        if kind == "header" and isinstance(t, dict):
            title = title or t.get("text")
        elif kind == "section":
            if isinstance(t, dict):
                add(t.get("text"))
            for f in b.get("fields") or []:
                if isinstance(f, dict):
                    add(f.get("text"))
        elif kind == "context":
            for el in b.get("elements") or []:
                if isinstance(el, dict) and el.get("text"):
                    add(el["text"])
    if not plain and not title:
        add(payload.get("text"))
    for a in payload.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        before = len(plain)
        add(a.get("pretext"))
        if a.get("title"):
            if a.get("title_link"):
                add(f"<{a['title_link']}|{a['title']}>")
            elif not title and not payload.get("text"):
                title = a["title"]
            else:
                add(f"*{a['title']}*")
        add(a.get("text"))
        for f in a.get("fields") or []:
            if isinstance(f, dict) and (f.get("title") or f.get("value")):
                add(f"*{f.get('title', '')}*: {f.get('value', '')}" if f.get("title") else f.get("value"))
        add(a.get("footer"))
        if len(plain) == before:
            add(a.get("fallback"))
    if title:
        title = _unescape(_shortcodes(str(title)))
    return title, "\n".join(plain), "<br/>".join(rich)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg: Config = None
    matrix: Matrix = None
    role: str = "api"
    rules: "Rules" = Rules()               # 1.2: disabled unless rules_file is set
    dlog: "DeliveryLog" = DeliveryLog()    # 1.2: disabled unless log_file is set

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _reply(self, status: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _reply_text(self, status: int, text: str):
        raw = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
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
        if self.cfg.webhook and path.startswith("/webhook/"):
            return self._handle_webhook(path)
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
            self.dlog.record(via="api", app=None, outcome="rejected", why="unknown application token",
                             client=self.address_string())
            return self._api_error(["application token is invalid"])
        if not secrets.compare_digest(params.get("user", ""), self.cfg.user_key):
            log.warning("rejected bad user key from %s", self.address_string())
            self.dlog.record(via="api", app=app.get("name"), outcome="rejected", why="wrong user key",
                             client=self.address_string())
            return self._api_error(["user identifier is not a valid user key"])
        if not params.get("message"):
            self.dlog.record(via="api", app=app.get("name"), outcome="rejected", why="blank message",
                             client=self.address_string())
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
        outcome = self._deliver(token, app, msg, "api")
        if outcome == "failed":
            return self._reply(500, {"status": 0, "errors": ["delivery to matrix failed"],
                                     "request": uuid.uuid4().hex})
        # A muted message is still a success to the sender: it was received and handled on purpose.
        self._reply(200, {"status": 1, "request": uuid.uuid4().hex})

    def _deliver(self, token: str, app: dict, msg: dict, via: str) -> str:
        """Mute rules, then Matrix, then the delivery log. Returns sent | muted | failed."""
        name = app.get("name")
        entry = {"via": via, "app": name, "title": msg.get("title") or name, "priority": msg.get("priority", 0),
                 "message": (msg["plain"] if msg.get("plain") is not None else msg.get("message", ""))[:300]}
        muted, why = self.rules.decide(name, msg.get("title") or "", entry["message"], msg.get("priority", 0))
        if muted:
            log.info("muted %r (%s): %s", name, why, entry["title"])
            self.dlog.record(**entry, outcome="muted", why=why)
            return "muted"
        try:
            event_id = self.matrix.send_notification(token, app, msg)
        except Exception as exc:
            log.exception("failed to relay notification for %r", name)
            self.dlog.record(**entry, outcome="failed", why=str(exc)[:300])
            return "failed"
        log.info("relayed %r -> %s (%s)", name, event_id, msg["title"] or "-")
        self.dlog.record(**entry, outcome="sent", event_id=event_id)
        return "sent"

    def _handle_webhook(self, path: str):
        """POST /webhook/<application token>/<user key> with the incoming-webhook body many tools know from Slack:
        JSON with `text` (and optionally `blocks` / `attachments`), or a form field `payload` holding that JSON.
        Both secrets travel in the URL - the same two the API checks, so this is no weaker a way in. Replies mirror
        that format: 200 "ok", 403 "invalid_token", 400 "invalid_payload" / "no_text". ?priority=-2..2 is honoured."""
        parts = path.split("/")
        token = parts[2] if len(parts) > 2 else ""
        user = parts[3] if len(parts) > 3 else ""
        app = self.cfg.apps.get(token)
        if app is None or not secrets.compare_digest(user, self.cfg.user_key):
            log.warning("rejected webhook with a bad token or user key from %s", self.address_string())
            self.dlog.record(via="webhook", app=app.get("name") if app else None, outcome="rejected",
                             why="unknown application token" if app is None else "wrong user key",
                             client=self.address_string())
            return self._reply_text(403, "invalid_token")
        raw = self._read_body()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        try:
            if ctype == "application/x-www-form-urlencoded":
                form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
                payload = json.loads((form.get("payload") or ["{}"])[0])
            else:
                payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except ValueError:
            self.dlog.record(via="webhook", app=app.get("name"), outcome="rejected", why="invalid payload",
                             client=self.address_string())
            return self._reply_text(400, "invalid_payload")
        title, plain, rich = _webhook_message(payload)
        if not plain.strip():
            self.dlog.record(via="webhook", app=app.get("name"), outcome="rejected", why="no text",
                             client=self.address_string())
            return self._reply_text(400, "no_text")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            priority = max(-2, min(2, int((qs.get("priority") or ["0"])[0])))
        except ValueError:
            priority = 0
        msg = {"message": rich, "plain": plain, "html": True, "title": title, "priority": priority,
               "url": None, "url_title": None}
        outcome = self._deliver(token, app, msg, "webhook")
        if outcome == "failed":
            return self._reply_text(500, "delivery_failed")
        self._reply_text(200, "ok")


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


def _serve(bind, role, cfg, matrix, rules=None, dlog=None):
    handler = type(f"{role}Handler", (_Handler,), {"cfg": cfg, "matrix": matrix, "role": role,
                                                    "rules": rules or Rules(), "dlog": dlog or DeliveryLog()})
    server = _Server(bind, handler)
    server.daemon_threads = True
    log.info("%s listener on http://%s:%d", role, bind[0], bind[1])
    threading.Thread(target=server.serve_forever, daemon=True, name=role).start()
    return server


def _print_log(cfg, limit: int, outcome: str, as_json: bool) -> int:
    if not cfg.log_file:
        print("The delivery log is off: set \"log_file\" in config.json.", file=sys.stderr)
        return 1
    try:
        entries = DeliveryLog.read(cfg.log_file, limit, outcome)
    except OSError as exc:
        print(f"Cannot read {cfg.log_file}: {exc}", file=sys.stderr)
        return 1
    for e in entries:
        if as_json:
            print(json.dumps(e, ensure_ascii=False))
            continue
        label = PRIORITY_LABEL.get(e.get("priority") or 0)
        what = f"[{label}] " if label else ""
        what += e.get("title") or ""
        text = (e.get("message") or "").replace("\n", " / ")
        if text:
            what += f" - {text[:100]}"
        why = f"   ({e['why']})" if e.get("why") else ""
        print(f"{e.get('at', '')}  {e.get('outcome', ''):<8}  {(e.get('app') or '-')[:18]:<18}  {what}{why}")
    if not entries:
        print("No entries" + (f" with outcome {outcome}" if outcome else "") + ".")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="beeper_api_bridge", description="beeper-api-bridge: a Pushover-compatible notification API that delivers into Beeper (Matrix).")
    ap.add_argument("-c", "--config", default=CONFIG_PATH, help="config.json path (default: $BEEPER_API_BRIDGE_CONFIG or next to the script)")
    ap.add_argument("--check", action="store_true", help="load the config, authenticate to the homeserver, then exit")
    ap.add_argument("--log", nargs="?", const=50, type=int, metavar="N",
                    help="print the last N entries of the delivery log (default 50) and exit; needs log_file in config")
    ap.add_argument("--outcome", choices=("sent", "muted", "rejected", "failed"),
                    help="with --log: only entries with this outcome")
    ap.add_argument("--json", action="store_true", help="with --log: print the raw JSON lines")
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
    if args.log is not None:
        return _print_log(cfg, args.log, args.outcome or "", args.json)
    matrix = Matrix(cfg)
    rules, dlog = Rules(cfg.rules_file), DeliveryLog(cfg.log_file, cfg.log_max)

    status, body = matrix._request("GET", "/_matrix/client/v3/account/whoami", user_id=cfg.bot)
    if status != 200:
        log.error("cannot reach homeserver as %s: %s %s", cfg.bot, status, body)
        return 1
    log.info("authenticated as %s", body.get("user_id"))
    if args.check:
        log.info("config OK: %d application token(s), api listener %s:%d", len(cfg.apps), *cfg.api_bind)
        log.info("webhook endpoint: %s", "on (POST /webhook/<token>/<user_key>)" if cfg.webhook else "off")
        if cfg.rules_file:
            if not rules._load():               # a good file is reported by the loader itself
                log.info("rules: none in effect (see any warning above)")
        else:
            log.info("rules: off")
        log.info("delivery log: %s", cfg.log_file or "off")
        return 0

    _serve(cfg.as_bind, "appservice", cfg, matrix)
    _serve(cfg.api_bind, "api", cfg, matrix, rules, dlog)
    log.info("beeper-api-bridge %s ready; %d application token(s) configured", __version__, len(cfg.apps))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
