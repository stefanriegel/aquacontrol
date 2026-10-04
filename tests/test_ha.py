import http.client
import json
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aquacontrol.ha import HAClient, HAError

TOKEN = "s3cr3t-token-value"


class FakeHA:
    """A local HTTP server standing in for Home Assistant; records every request."""

    def __init__(self):
        self.requests = []
        self.reply = (200, b'{"state": "off"}')
        self.delay = 0.0
        self.location = None
        self.truncate = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                outer.requests.append((self.command, self.path, self.headers.get("Authorization"),
                                       self.headers.get("Content-Type"), body))
                if outer.delay:
                    time.sleep(outer.delay)
                code, payload = outer.reply
                self.send_response(code)
                if outer.location:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Length", str(len(payload) + (50 if outer.truncate else 0)))
                self.end_headers()
                self.wfile.write(payload)
                if outer.truncate:
                    self.close_connection = True

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HAClientTest(unittest.TestCase):
    def setUp(self):
        self.ha = FakeHA()
        self.addCleanup(self.ha.close)
        self.client = HAClient(self.ha.url, TOKEN)

    def test_get_state_sends_bearer_header_and_path(self):
        self.ha.reply = (200, json.dumps({"state": "cool", "attributes": {"temperature": 20}}).encode())
        state = self.client.get_state("climate.panasonic_ac")
        self.assertEqual(state["state"], "cool")
        self.assertEqual(state["attributes"]["temperature"], 20)
        method, path, auth, _, _ = self.ha.requests[0]
        self.assertEqual((method, path, auth), ("GET", "/api/states/climate.panasonic_ac", f"Bearer {TOKEN}"))

    def test_trailing_slash_in_base_url(self):
        HAClient(self.ha.url + "/", TOKEN).get_state("climate.x")
        self.assertEqual(self.ha.requests[0][1], "/api/states/climate.x")

    def test_call_posts_json_body(self):
        self.ha.reply = (200, b"[]")
        self.assertIsNone(self.client.call("climate", "set_temperature",
                                           {"entity_id": "climate.x", "temperature": 20.0}))
        method, path, auth, ctype, body = self.ha.requests[0]
        self.assertEqual((method, path, auth), ("POST", "/api/services/climate/set_temperature", f"Bearer {TOKEN}"))
        self.assertEqual(ctype, "application/json")
        self.assertEqual(json.loads(body), {"entity_id": "climate.x", "temperature": 20.0})

    def test_path_segments_are_quoted(self):
        self.client.get_state("climate.x/../../etc")
        self.assertNotIn("/../", self.ha.requests[0][1])

    def test_http_errors_map_to_german_messages(self):
        for code, fragment in ((401, "Token"), (403, "Token"), (404, "nicht gefunden"), (500, "500")):
            with self.subTest(code=code):
                self.ha.reply = (code, b"nope")
                with self.assertRaises(HAError) as cm:
                    self.client.get_state("climate.x")
                self.assertIn(fragment, str(cm.exception))
                self.assertNotIn(TOKEN, str(cm.exception))
        self.ha.reply = (500, b"boom")
        with self.assertRaises(HAError) as cm:
            self.client.call("climate", "turn_off", {})
        self.assertIn("500", str(cm.exception))

    def test_bad_json_is_an_error(self):
        for payload in (b"<html>", b"[1, 2]", b"null"):
            with self.subTest(payload=payload):
                self.ha.reply = (200, payload)
                with self.assertRaises(HAError):
                    self.client.get_state("climate.x")

    def test_timeout(self):
        self.ha.delay = 1.0
        slow = HAClient(self.ha.url, TOKEN, timeout=0.2)
        with self.assertRaises(HAError) as cm:
            slow.get_state("climate.x")
        self.assertIn("Zeit", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_default_timeout_is_ten_seconds(self):
        self.assertEqual(self.client.timeout, 10)

    def test_connection_refused(self):
        dead = FakeHA()
        url = dead.url
        dead.close()
        with self.assertRaises(HAError) as cm:
            HAClient(url, TOKEN).get_state("climate.x")
        self.assertIn("nicht erreichbar", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_redirects_are_not_followed(self):
        # urllib would resend the Authorization header to the redirect target
        other = FakeHA()
        self.addCleanup(other.close)
        self.ha.reply = (302, b"")
        self.ha.location = other.url + "/api/states/climate.x"
        with self.assertRaises(HAError):
            self.client.get_state("climate.x")
        self.assertEqual(other.requests, [])

    def test_header_breaking_token_gives_a_fixed_message_without_the_token(self):
        for token in ("SECRET\nTOKEN", "SECRET\r\nX-Evil: 1", "SECRETtökən"):
            with self.subTest(token=token):
                with self.assertRaises(HAError) as cm:
                    HAClient(self.ha.url, token).get_state("climate.x")
                self.assertNotIn("SECRET", str(cm.exception))
                self.assertNotIn("Bearer", str(cm.exception))
                self.assertIn("Home Assistant", str(cm.exception))
        self.assertEqual(self.ha.requests, [])

    def test_truncated_response_is_an_ha_error(self):
        self.ha.truncate = True  # http.client.IncompleteRead is not an OSError
        with self.assertRaises(HAError) as cm:
            self.client.get_state("climate.x")
        self.assertIn("Home Assistant", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_other_http_protocol_errors_are_ha_errors(self):
        for exc in (http.client.BadStatusLine("garbage"), http.client.LineTooLong("status line"),
                    http.client.RemoteDisconnected("closed")):
            with self.subTest(exc=type(exc).__name__), mock.patch("aquacontrol.ha._OPENER.open", side_effect=exc):
                with self.assertRaises(HAError) as cm:
                    self.client.call("climate", "turn_off", {})
                self.assertNotIn(TOKEN, str(cm.exception))

    def test_invalid_url_scheme(self):
        with self.assertRaises(HAError):
            HAClient("file:///etc/passwd", TOKEN).get_state("climate.x")


if __name__ == "__main__":
    unittest.main()
