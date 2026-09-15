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

    def test_rejects_unknown_token_like_pushover(self):
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

    def test_json_body_and_html_flag(self):
        self.matrix.calls.clear()
        status, body = self.post("/1/messages.json", {"token": TOKEN, "user": USER, "message": "<b>bold</b>", "html": 1}, as_json=True)
        self.assertEqual(status, 200)
        content = [c for c in self.matrix.calls if "/send/" in c[1]][0][2]
        self.assertIn("<b>bold</b>", content["formatted_body"])

    def test_blank_message_refused(self):
        status, body = self.post("/1/messages.json", {"token": TOKEN, "user": USER, "message": ""})
        self.assertEqual(body["errors"], ["message cannot be blank"])


if __name__ == "__main__":
    unittest.main()


class PrefixTest(unittest.TestCase):
    def test_prefix_from_namespace_regex(self):
        self.assertEqual(pb._prefix_from_registration({"namespaces": {"users": [{"regex": "@sh-apibridge_.+:beeper\\.local"}]}}), "sh-apibridge")
        self.assertEqual(pb._prefix_from_registration({"namespaces": {"users": [{"regex": "@sh-pushover_.+:beeper\\.local"}]}}), "sh-pushover")
        self.assertEqual(pb._prefix_from_registration({}), "")
