"""Exercise the notification API against a fake Matrix (no network). Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import beeper_api_bridge as pb  # noqa: E402

USER = "u" + "x" * 29
TOKEN = "a" + "y" * 29


class FakeMatrix(pb.Matrix):
    """Records what would be sent instead of talking to a homeserver."""

    def __init__(self, cfg):
        self.calls = []
        super().__init__(cfg)

    def _request(self, method, path, body=None, user_id=None):
        self.calls.append((method, path.split("?")[0], body, user_id))
        if path.endswith("/register"):
            return 200, {}
        if path.endswith("/createRoom"):
            return 200, {"room_id": "!room:example"}
        if "/send/m.room.message/" in path:
            return 200, {"event_id": "$evt"}
        return 200, {}


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        reg = os.path.join(cls.tmp.name, "registration.yaml")
        open(reg, "w").write("as_token: as\nhs_token: hs\nsender_localpart: sh-apibridgebot\nnamespaces:\n  users:\n    - regex: '@sh-apibridge_.+:example'\n      exclusive: true\n")
        cfgp = os.path.join(cls.tmp.name, "config.json")
        json.dump({"registration_file": reg, "homeserver": "http://hs.invalid", "domain": "example", "owner": "@me:example",
                   "state_file": os.path.join(cls.tmp.name, "state.json"), "api_port": 0, "appservice_port": 0,
                   "user_key": USER, "applications": {TOKEN: {"name": "Uptime Kuma"}}}, open(cfgp, "w"))
        cls.cfg = pb.Config(cfgp)
        cls.matrix = FakeMatrix(cls.cfg)
        handler = type("H", (pb._Handler,), {"cfg": cls.cfg, "matrix": cls.matrix, "role": "api"})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def post(self, path, params, as_json=False):
        data = json.dumps(params).encode() if as_json else urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_rejects_unknown_token_with_api_error_shape(self):
        status, body = self.post("/1/messages.json", {"token": "nope", "user": USER, "message": "x"})
        self.assertEqual((status, body["status"], body["errors"]), (400, 0, ["application token is invalid"]))

    def test_rejects_bad_user_key(self):
        status, body = self.post("/1/messages.json", {"token": TOKEN, "user": "wrong", "message": "x"})
        self.assertEqual((status, body["errors"]), (400, ["user identifier is not a valid user key"]))

    def test_validate_endpoint(self):
        status, body = self.post("/1/users/validate.json", {"token": TOKEN, "user": USER})
        self.assertEqual((status, body["status"]), (200, 1))

    def test_message_creates_room_and_posts_with_priority_label(self):
        self.matrix.calls.clear()
        for k in ("rooms", "creators", "members"):          # forget any room an earlier test created
            self.matrix._state.get(k, {}).clear()
        status, body = self.post("/1/messages.json", {"token": TOKEN, "user": USER, "title": "Disk", "message": "91% full", "priority": "1", "url": "https://x.example/d"})
        self.assertEqual((status, body["status"]), (200, 1))
        sends = [c for c in self.matrix.calls if "/send/m.room.message/" in c[1]]
        self.assertEqual(len(sends), 1)
        content = sends[0][2]
        self.assertEqual(content["body"], "[HIGH]\nDisk\n91% full\nhttps://x.example/d")
        self.assertIn("<strong>[HIGH] Disk</strong>", content["formatted_body"])
        self.assertEqual(sends[0][3], "@sh-apibridge_uptime-kuma:example")          # ghost prefix comes from the registration namespace
        self.assertTrue(any(c[1].endswith("/join") and c[3] == "@me:example" for c in self.matrix.calls))  # owner joined

    def test_plain_line_breaks_survive_in_the_html(self):
        # Clients render formatted_body, and in HTML a newline is only whitespace: a multi-line message used to arrive
        # as one run-on paragraph. Line breaks become <br/>, the text is still escaped, and the plain body is unchanged.
        self.matrix.calls.clear()
        msg = "a <b> line\nsecond line\nthird"
        status, _ = self.post("/1/messages.json", {"token": TOKEN, "user": USER, "title": "T", "message": msg})
        self.assertEqual(status, 200)
        content = [c for c in self.matrix.calls if "/send/" in c[1]][0][2]
        self.assertIn("a &lt;b&gt; line<br/>second line<br/>third", content["formatted_body"])
        self.assertTrue(content["body"].endswith(msg))

    def test_json_body_and_html_flag(self):
        self.matrix.calls.clear()
        status, body = self.post("/1/messages.json", {"token": TOKEN, "user": USER, "message": "<b>bold</b>", "html": 1}, as_json=True)
        self.assertEqual(status, 200)
        content = [c for c in self.matrix.calls if "/send/" in c[1]][0][2]
        self.assertIn("<b>bold</b>", content["formatted_body"])

    def test_query_string_credentials_and_kuma_webhook_shape(self):
        self.matrix.calls.clear()
        req = urllib.request.Request(self.url + f"/1/messages.json?token={TOKEN}&user={USER}",
                                     data=json.dumps({"msg": "[Web] [🔴 Down] timeout", "monitor": {"name": "Web"}, "heartbeat": {"status": 0}}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(json.loads(r.read())["status"], 1)
        content = [c for c in self.matrix.calls if "/send/" in c[1]][0][2]
        self.assertTrue(content["body"].startswith("Uptime Kuma: Web 🔴 Down\n[Web] [🔴 Down] timeout"))

    def test_grafana_stock_webhook_shape(self):
        self.matrix.calls.clear()
        req = urllib.request.Request(self.url + f"/1/messages.json?token={TOKEN}&user={USER}",
                                     data=json.dumps({"title": "[FIRING:1] Disk full", "message": "/ is at 95%", "state": "alerting"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(json.loads(r.read())["status"], 1)
        content = [c for c in self.matrix.calls if "/send/" in c[1]][0][2]
        self.assertEqual(content["body"], "[FIRING:1] Disk full\n/ is at 95%")

    def test_webhook_route_absent_without_the_key(self):
        # 1.2: with a 1.1 config (no "webhook" key) the route does not exist - the exact 1.1 reply
        req = urllib.request.Request(self.url + f"/webhook/{TOKEN}/{USER}", data=b'{"text":"x"}',
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("the webhook route should not exist when not enabled")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            self.assertEqual(json.loads(e.read())["errors"], ["not found"])     # the exact 1.1 reply

    def test_blank_message_refused(self):
        status, body = self.post("/1/messages.json", {"token": TOKEN, "user": USER, "message": ""})
        self.assertEqual(body["errors"], ["message cannot be blank"])


class ServerErrorTest(unittest.TestCase):
    """A client dropping an idle keep-alive connection is routine: no traceback. Anything else keeps its traceback."""

    def _stderr_of(self, exc):
        import io
        server = pb._Server.__new__(pb._Server)          # handle_error needs no socket
        buf, old = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            try:
                raise exc
            except Exception:
                server.handle_error(None, ("127.0.0.1", 42978))
        finally:
            sys.stderr = old
        return buf.getvalue()

    def test_client_hang_ups_are_quiet(self):
        for exc in (ConnectionResetError(104, "Connection reset by peer"), BrokenPipeError(), ConnectionAbortedError()):
            self.assertEqual(self._stderr_of(exc), "", exc)

    def test_real_errors_still_get_a_traceback(self):
        self.assertIn("Traceback", self._stderr_of(ValueError("something actually broke")))


class PrefixTest(unittest.TestCase):
    def test_prefix_from_namespace_regex(self):
        self.assertEqual(pb._prefix_from_registration({"namespaces": {"users": [{"regex": "@sh-apibridge_.+:beeper\\.local"}]}}), "sh-apibridge")
        self.assertEqual(pb._prefix_from_registration({"namespaces": {"users": [{"regex": "@sh-notify_.+:beeper\\.local"}]}}), "sh-notify")
        self.assertEqual(pb._prefix_from_registration({}), "")



# ---- 1.2: everything below is optional and off unless configured ----------------------------------------------------
import datetime as dt  # noqa: E402


class WebhookTest(unittest.TestCase):
    """webhook, rules_file and log_file all switched on."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        reg = os.path.join(d, "registration.yaml")
        with open(reg, "w") as fh:
            fh.write("as_token: as\nhs_token: hs\nsender_localpart: sh-apibridgebot\nnamespaces:\n  users:\n    - regex: '@sh-apibridge_.+:example'\n      exclusive: true\n")
        cls.rules_path = os.path.join(d, "rules.json")
        cls.log_path = os.path.join(d, "delivery.log")
        cfgp = os.path.join(d, "config.json")
        json.dump({"registration_file": reg, "homeserver": "http://hs.invalid", "domain": "example", "owner": "@me:example",
                   "state_file": os.path.join(d, "state.json"), "user_key": USER,
                   "applications": {TOKEN: {"name": "Monitor"}, "b" + "z" * 29: {"name": "Backups"}},
                   "webhook": True, "rules_file": cls.rules_path, "log_file": cls.log_path, "log_max_entries": 100},
                  open(cfgp, "w"))  # noqa: SIM115 - same pattern as BridgeTest
        cls.cfg = pb.Config(cfgp)
        cls.matrix = FakeMatrix(cls.cfg)
        cls.rules = pb.Rules(cls.cfg.rules_file)
        cls.dlog = pb.DeliveryLog(cls.cfg.log_file, cls.cfg.log_max)
        handler = type("H", (pb._Handler,), {"cfg": cls.cfg, "matrix": cls.matrix, "role": "api",
                                             "rules": cls.rules, "dlog": cls.dlog})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.matrix.calls.clear()
        self.write_rules(None)

    def write_rules(self, data):
        if data is None:
            if os.path.exists(self.rules_path):
                os.remove(self.rules_path)
            return
        with open(self.rules_path, "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        st = os.stat(self.rules_path)          # make sure the reload sees a new mtime even within one second
        os.utime(self.rules_path, (st.st_atime, st.st_mtime + len(self.matrix.calls) + 1 + time_bump()))

    def hook(self, body, path=None, ctype="application/json"):
        req = urllib.request.Request(self.url + (path or f"/webhook/{TOKEN}/{USER}"),
                                     data=body if isinstance(body, bytes) else json.dumps(body).encode(),
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def sent(self):
        return [c[2] for c in self.matrix.calls if "/send/m.room.message/" in c[1]]

    def last_log(self):
        return pb.DeliveryLog.read(self.log_path, 1)[-1]

    # ---- the webhook -----------------------------------------------------------------------------------------------
    def test_text_formatting_links_and_shortcodes(self):
        status, body = self.hook({"text": "Disk *full* on _db1_ :rotating_light: <https://x.example/d|details>"})
        self.assertEqual((status, body), (200, "ok"))
        c = self.sent()[0]
        self.assertIn("Disk <b>full</b> on <i>db1</i> 🚨 <a href=\"https://x.example/d\">details</a>", c["formatted_body"])
        self.assertTrue(c["body"].endswith("Disk full on db1 🚨 details (https://x.example/d)"))
        self.assertIn("<strong>Monitor</strong>", c["formatted_body"])           # no title -> the application's name

    def test_a_wildcard_domain_is_not_mistaken_for_bold(self):
        self.hook({"text": "renewals for *.example.com stall *silently*"})
        html_body = self.sent()[0]["formatted_body"]
        self.assertIn("*.example.com stall <b>silently</b>", html_body)

    def test_escaped_entities_and_html_injection(self):
        self.hook({"text": "a &lt;b&gt; &amp; c <script>"})
        c = self.sent()[0]
        self.assertTrue(c["body"].endswith("a <b> & c <script>"))
        self.assertIn("a &lt;b&gt; &amp; c &lt;script&gt;", c["formatted_body"])
        self.assertNotIn("<script>", c["formatted_body"])

    def test_line_breaks_code_and_unknown_shortcodes(self):
        self.hook({"text": "one\ntwo `x=1` :not_a_real_code:\n```\nblock\n```"})
        h = self.sent()[0]["formatted_body"]
        self.assertIn("one<br/>two <code>x=1</code> :not_a_real_code:<br/><pre>block<br/></pre>", h)

    def test_attachments_like_alertmanager_sends(self):
        self.hook({"attachments": [{"color": "danger", "title": "[FIRING:1] DiskFull", "title_link": "https://am.example/x",
                                    "text": "*Alert:* disk 91% full", "fallback": "[FIRING:1] DiskFull",
                                    "fields": [{"title": "Severity", "value": "critical"}]}]})
        c = self.sent()[0]
        self.assertIn('<a href="https://am.example/x">[FIRING:1] DiskFull</a>', c["formatted_body"])
        self.assertIn("<b>Alert:</b> disk 91% full", c["formatted_body"])
        self.assertIn("<b>Severity</b>: critical", c["formatted_body"])

    def test_attachment_fallback_and_header_block_title(self):
        self.hook({"attachments": [{"fallback": "only the fallback"}]})
        self.assertIn("only the fallback", self.sent()[0]["body"])
        self.matrix.calls.clear()
        self.hook({"blocks": [{"type": "header", "text": {"type": "plain_text", "text": "Backup :white_check_mark:"}},
                              {"type": "section", "text": {"type": "mrkdwn", "text": "12 GB in *4m*"}}]})
        c = self.sent()[0]
        self.assertIn("<strong>Backup ✅</strong>", c["formatted_body"])
        self.assertIn("12 GB in <b>4m</b>", c["formatted_body"])

    def test_text_is_only_the_fallback_when_blocks_render(self):
        self.hook({"text": "notification preview", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "the real body"}}]})
        self.assertNotIn("notification preview", self.sent()[0]["body"])
        self.matrix.calls.clear()
        self.hook({"text": "used when blocks are empty", "blocks": [{"type": "divider"}]})
        self.assertIn("used when blocks are empty", self.sent()[0]["body"])

    def test_form_encoded_payload_and_priority(self):
        form = urllib.parse.urlencode({"payload": json.dumps({"text": "via form"})}).encode()
        status, _ = self.hook(form, path=f"/webhook/{TOKEN}/{USER}?priority=1", ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 200)
        c = self.sent()[0]
        self.assertTrue(c["body"].startswith("[HIGH]\nMonitor\nvia form"))

    def test_refusals_use_the_webhook_reply_shapes(self):
        self.assertEqual(self.hook({"text": "x"}, path=f"/webhook/nope/{USER}"), (403, "invalid_token"))
        self.assertEqual(self.hook({"text": "x"}, path=f"/webhook/{TOKEN}/wrong"), (403, "invalid_token"))
        self.assertEqual(self.hook({"text": "x"}, path=f"/webhook/{TOKEN}"), (403, "invalid_token"))
        self.assertEqual(self.hook(b"{not json"), (400, "invalid_payload"))
        self.assertEqual(self.hook({"text": "   "}), (400, "no_text"))
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.last_log()["outcome"], "rejected")

    # ---- mute rules and quiet hours --------------------------------------------------------------------------------
    def test_mute_rule_by_app_and_pattern_still_answers_success(self):
        self.write_rules({"mute": [{"app": "monitor", "match": r"\bUp\b", "reason": "flapping"}]})
        status, body = self.hook({"text": "db1 is Up"})
        self.assertEqual((status, body), (200, "ok"))
        self.assertEqual(self.sent(), [])
        e = self.last_log()
        self.assertEqual((e["outcome"], e["why"], e["app"]), ("muted", "mute rule 1 (flapping)", "Monitor"))
        self.hook({"text": "db1 is Down"})                                     # the pattern does not match
        self.assertEqual(len(self.sent()), 1)

    def test_the_pushover_api_is_muted_by_the_same_rules(self):
        self.write_rules({"mute": [{"app": "Monitor"}]})
        req = urllib.request.Request(self.url + "/1/messages.json",
                                     data=urllib.parse.urlencode({"token": TOKEN, "user": USER, "message": "x"}).encode())
        with urllib.request.urlopen(req) as r:
            self.assertEqual(json.loads(r.read())["status"], 1)               # unchanged reply shape
        self.assertEqual(self.sent(), [])

    def test_emergencies_break_through_unless_a_rule_says_otherwise(self):
        self.write_rules({"mute": [{"app": "Monitor", "max_priority": 2}]})
        self.hook({"text": "x"}, path=f"/webhook/{TOKEN}/{USER}?priority=2")
        self.assertEqual(len(self.sent()), 1)
        self.matrix.calls.clear()
        self.write_rules({"mute": [{"app": "Monitor", "max_priority": 2, "include_emergency": True}]})
        self.hook({"text": "x"}, path=f"/webhook/{TOKEN}/{USER}?priority=2")
        self.assertEqual(self.sent(), [])

    def test_high_priority_passes_a_default_rule(self):
        self.write_rules({"mute": [{"app": "Monitor"}]})                        # max_priority defaults to 0
        self.hook({"text": "x"}, path=f"/webhook/{TOKEN}/{USER}?priority=1")
        self.assertEqual(len(self.sent()), 1)

    def test_expired_rules_are_ignored(self):
        self.write_rules({"mute": [{"app": "Monitor", "until": "2000-01-01T00:00:00Z"}]})
        self.hook({"text": "x"})
        self.assertEqual(len(self.sent()), 1)

    def test_fails_open(self):
        for broken in ("{not json", "[1, 2]", json.dumps({"mute": [{"match": "("}]})):
            self.matrix.calls.clear()
            self.write_rules(broken)
            self.assertEqual(self.hook({"text": "still delivered"}), (200, "ok"), broken)
            self.assertEqual(len(self.sent()), 1, broken)

    def test_rules_reload_when_the_file_changes(self):
        self.write_rules({"mute": []})
        self.hook({"text": "a"})
        self.write_rules({"mute": [{"app": "Monitor"}]})
        self.hook({"text": "b"})
        self.assertEqual(len(self.sent()), 1)

    def test_quiet_hours_wrap_midnight(self):
        r = pb.Rules(self.rules_path)
        self.write_rules({"timezone": "UTC", "quiet_hours": {"start": "23:00", "end": "07:00"}})
        at = lambda h, m=0: dt.datetime(2026, 1, 1, h, m, tzinfo=dt.timezone.utc)  # noqa: E731
        self.assertEqual(r.decide("Monitor", "t", "m", 0, now=at(23, 30)), (True, "quiet hours 23:00-07:00"))
        self.assertEqual(r.decide("Monitor", "t", "m", 0, now=at(3)), (True, "quiet hours 23:00-07:00"))
        self.assertEqual(r.decide("Monitor", "t", "m", 0, now=at(7)), (False, ""))
        self.assertEqual(r.decide("Monitor", "t", "m", 0, now=at(12)), (False, ""))
        self.assertEqual(r.decide("Monitor", "t", "m", 1, now=at(3)), (False, ""))     # above max_priority (0)
        self.assertEqual(r.decide("Monitor", "t", "m", 2, now=at(3)), (False, ""))     # emergency

    # ---- delivery log ----------------------------------------------------------------------------------------------
    def test_log_records_each_outcome_and_is_private(self):
        self.hook({"text": "hello"})
        e = self.last_log()
        self.assertEqual((e["outcome"], e["via"], e["app"], e["message"]), ("sent", "webhook", "Monitor", "hello"))
        self.assertEqual(e["event_id"], "$evt")
        self.assertEqual(os.stat(self.log_path).st_mode & 0o777, 0o600)

    def test_log_is_capped(self):
        for i in range(130):
            self.dlog.record(outcome="sent", app="Monitor", title=f"n{i}")
        with open(self.log_path) as fh:
            lines = fh.readlines()
        self.assertLessEqual(len(lines), 120)                                      # trimmed back to max at 1.2 x max
        self.assertEqual(json.loads(lines[-1])["title"], "n129")

    def test_an_unwritable_log_never_blocks_delivery(self):
        broken = pb.DeliveryLog(os.path.join(self.tmp.name, "no-such-dir", "x.log"))
        old = WebhookTest.dlog
        try:
            type(self).server.RequestHandlerClass.dlog = broken
            self.assertEqual(self.hook({"text": "x"}), (200, "ok"))
            self.assertEqual(len(self.sent()), 1)
        finally:
            type(self).server.RequestHandlerClass.dlog = old

    def test_cli_prints_the_log(self):
        import contextlib, io
        self.hook({"text": "cli check"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pb._print_log(self.cfg, 5, "sent", False)
        self.assertEqual(rc, 0)
        self.assertIn("sent", out.getvalue())
        self.assertIn("cli check", out.getvalue())


_bump = [0]


def time_bump():
    _bump[0] += 1
    return _bump[0]


if __name__ == "__main__":
    unittest.main()
