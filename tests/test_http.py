import copy
import http.client
import http.cookiejar
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from site_contract_api.server import Server, Source
from .test_engine import HTML, RULES


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.source_path = Path(self.directory.name) / "source.html"
        self.source_path.write_text(HTML, encoding="utf-8")
        self.server = Server(("127.0.0.1", 0), Source(RULES, self.source_path), HTML, RULES)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, method="GET", path="/api/data", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        if isinstance(body, dict):
            body = json.dumps(body)
        defaults = {"Host": self.host}
        if method == "POST":
            defaults.update({"Origin": "http://" + self.host, "Content-Type": "application/json",
                             "X-CSRF-Token": self.server.token, "Cookie": self.server.cookie_name + "=" + self.server.token})
        connection.request(method, path, body, headers={**defaults, **(headers or {})})
        response = connection.getresponse()
        status, result_headers, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, result_headers, data

    def test_second_server_cannot_share_an_active_listener_port(self):
        duplicate = None
        try:
            duplicate = Server(("127.0.0.1", self.server.server_port), self.server.source, HTML, RULES)
        except OSError:
            pass
        else:
            self.fail("An active listener port must be exclusive")
        finally:
            if duplicate is not None:
                duplicate.server_close()

    @unittest.skipUnless(hasattr(socket, "SO_EXCLUSIVEADDRUSE"), "Windows exclusive socket option")
    def test_windows_exclusive_listener_rejects_reuse_address_binding(self):
        self.assertEqual(self.server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
        with socket.socket() as contender:
            contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                contender.bind(("127.0.0.1", self.server.server_port))

    def test_offline_default_endpoint_and_security_headers(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["items"]), 3)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(str(self.source_path), body.decode())
        status, headers, body = self.request(path="/")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertTrue(headers["Set-Cookie"].startswith(f"sca_csrf_{self.server.server_port}="))
        self.assertNotIn(b"{{CSRF}}", body)

    def test_cached_endpoint_keeps_origin_and_processing_admission_checks(self):
        self.assertEqual(self.request()[0], 200)
        with mock.patch.object(self.server.source, "result", side_effect=AssertionError("A rejected request must not read cached data")):
            self.assertEqual(self.request(headers={"Origin": "https://elsewhere.invalid"})[0], 403)
            self.assertEqual(self.request(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
            self.server.jobs.acquire()
            self.server.jobs.acquire()
            try:
                status, _, body = self.request()
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(body)["error"], "busy")
            finally:
                self.server.jobs.release()
                self.server.jobs.release()

    def test_two_instances_share_a_browser_cookie_jar_without_overwriting_tokens(self):
        second = Server(("127.0.0.1", 0), Source(RULES, self.source_path), HTML, RULES)
        thread = threading.Thread(target=second.serve_forever, daemon=True)
        thread.start()
        jar = http.cookiejar.CookieJar()
        browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar))
        try:
            for instance in (self.server, second):
                with browser.open(f"http://127.0.0.1:{instance.server_port}/", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
            self.assertEqual({cookie.name for cookie in jar}, {self.server.cookie_name, second.cookie_name})
            body = json.dumps({"html": HTML, "rules": RULES}).encode()
            for instance in (self.server, second, self.server):
                origin = f"http://127.0.0.1:{instance.server_port}"
                request = Request(origin + "/api/preview", data=body,
                                  headers={"Origin": origin, "Content-Type": "application/json", "X-CSRF-Token": instance.token})
                with browser.open(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(json.loads(response.read())["ok"])
            self.assertEqual(self.request("POST", "/api/preview", {"html": HTML, "rules": RULES},
                                          {"Cookie": f"{self.server.cookie_name}={second.token}"})[0], 403)
        finally:
            second.shutdown()
            second.server_close()
            thread.join(timeout=2)

    def test_preview_is_real_and_does_not_reconfigure_endpoint(self):
        changed = HTML.replace("Precision driver kit", "Different preview title")
        status, _, body = self.request("POST", "/api/preview", {"html": changed, "rules": RULES})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"][0]["name"], "Different preview title")
        self.assertEqual(json.loads(self.request()[2])["items"][0]["name"], "Precision driver kit")

    def test_real_source_drift_returns_failure_not_stale_success(self):
        self.source_path.write_text(HTML.replace('data-price="24.50"', 'data-cost="24.50"'), encoding="utf-8")
        status, _, body = self.request()
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "schema_drift")
        self.assertNotIn("items", json.loads(body))

    def test_invalid_unicode_config_returns_fixed_json_and_can_recover(self):
        self.server.source.rules = {**RULES, "base_url": "https://example.invalid/\ud800"}
        status, headers, body = self.request()
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"], "rules")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn(b"Traceback", body)
        self.assertNotIn(str(self.source_path).encode(), body)
        self.server.source.rules = RULES
        self.assertEqual(self.request()[0], 200)

    def test_large_integer_and_surrogate_preview_rules_are_controlled(self):
        fixtures = ['{"item_selector":"article","min_items":' + "1" * 5000 + ',"fields":[{"name":"title","selector":":scope"}]}',
                    '{"item_selector":"article","base_url":"https://example.invalid/\\ud800","fields":[{"name":"title","selector":":scope"}]}']
        for rules in fixtures:
            with self.subTest(length=len(rules)):
                status, _, body = self.request("POST", "/api/preview", {"html": HTML, "rules": rules})
                self.assertEqual(status, 422)
                self.assertEqual(json.loads(body)["error"], "rules")
                self.assertNotIn("items", json.loads(body))

    def test_missing_or_invalid_origin_cookie_and_token_fail(self):
        body = {"html": HTML, "rules": RULES}
        cases = [{"Origin": ""}, {"Origin": "https://" + self.host}, {"Origin": "http://" + self.host + "/"},
                 {"Origin": "http://" + self.host + "#x"}, {"Origin": "http://" + self.host + "?x"},
                 {"X-CSRF-Token": "a" * 43}, {"Cookie": ""}, {"Sec-Fetch-Site": "cross-site"}]
        for headers in cases:
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.request("POST", "/api/preview", body, headers)[0], 403)

    def test_cross_origin_signals_are_rejected_for_every_method_before_source_reads(self):
        port = self.server.server_port
        foreign = [{"Origin": "null"}, {"Origin": "https://attacker.invalid"}, {"Origin": "http://127.0.0.1:1"},
                   {"Origin": f"http://localhost:{port}"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}]
        with mock.patch.object(self.server.source, "result") as result:
            for method, path in (("GET", "/api/data"), ("GET", "/"), ("GET", "/static/app.js"), ("GET", "/api/demo"), ("OPTIONS", "/api/data")):
                for headers in foreign:
                    with self.subTest(method=method, path=path, headers=headers):
                        status, response_headers, body = self.request(method, path, headers=headers)
                        self.assertEqual(status, 403)
                        self.assertEqual(json.loads(body)["error"], "origin")
                        self.assertNotIn("Set-Cookie", response_headers)
            result.assert_not_called()
        allowed = [{}, {"Sec-Fetch-Site": "none"}, {"Sec-Fetch-Site": "same-origin", "Origin": "http://" + self.host},
                   {"Host": f"localhost:{port}", "Origin": f"http://LOCALHOST:{port}", "Sec-Fetch-Site": "same-origin"}]
        for headers in allowed:
            with self.subTest(allowed=headers):
                self.assertEqual(self.request(path="/api/data", headers=headers)[0], 200)
                self.assertEqual(self.request(path="/", headers=headers)[0], 200)

    def test_unknown_hosts_and_arbitrary_fetch_parameters_fail(self):
        with mock.patch("site_contract_api.remote.bounded_get") as fetch:
            for host in ("evil.example", "localhost", "127.0.0.1.evil.example", self.host + ".example"):
                self.assertEqual(self.request(headers={"Host": host})[0], 400)
            for path in ("/api/data?url=https://example.org", "/api/preview?url=x"):
                self.assertEqual(self.request(path=path)[0], 400)
            self.assertEqual(self.request("POST", "/api/preview", {"url": "https://example.org"})[0], 400)
            fetch.assert_not_called()

    def test_duplicate_headers_and_transfer_encoding_fail(self):
        for name, value in (("Host", self.host), ("Origin", "http://" + self.host), ("Content-Length", "0"), ("Content-Type", "application/json"), ("Sec-Fetch-Site", "same-origin")):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            connection.putrequest("GET", "/healthz", skip_host=True)
            if name != "Host":
                connection.putheader("Host", self.host)
            connection.putheader(name, value)
            connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.close()
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_body_framing_size_type_and_json_limits(self):
        self.assertEqual(self.request("POST", "/api/preview", "{}", {"Content-Length": str(4 * 1024 * 1024)})[0], 413)
        self.assertEqual(self.request("POST", "/api/preview", "{}", {"Content-Type": "text/plain"})[0], 415)
        for body in ("{", "[]", '{"html":"a","html":"b","rules":{}}'):
            self.assertIn(self.request("POST", "/api/preview", body)[0], (400, 422))
        self.assertEqual(self.request(headers={"X-Large": "x" * 9000})[0], 431)

    def test_busy_requests_fail_without_waiting_for_body(self):
        self.server.jobs.acquire()
        self.server.jobs.acquire()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=1)
            connection.putrequest("POST", "/api/preview")
            for key, value in {"Origin": "http://" + self.host, "X-CSRF-Token": self.server.token,
                               "Cookie": self.server.cookie_name + "=" + self.server.token, "Content-Type": "application/json",
                               "Content-Length": "1000"}.items():
                connection.putheader(key, value)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            response.read()
            connection.close()
        finally:
            self.server.jobs.release()
            self.server.jobs.release()
        self.assertEqual(self.request()[0], 200)

    def test_static_traversal_and_missing_route(self):
        for path in ("/static/../engine.py", "/static/%2e%2e/engine.py", "/not-a-route"):
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.request(path="/static/app.js")[0], 200)
        self.assertEqual(self.request(path="/api/status")[0], 200)

    def test_slow_headers_release_all_connection_slots_at_the_deadline(self):
        connections, stop = [], threading.Event()
        started = time.monotonic()
        try:
            with mock.patch("site_contract_api.server.HEADER_BUDGET", 0.2), mock.patch.object(self.server.source, "result") as source:
                for _ in range(16):
                    connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1)
                    connection.sendall(f"GET /api/data HTTP/1.1\r\nHost: {self.host}\r\nX-Drip: ".encode())
                    connections.append(connection)

                def drip():
                    while not stop.wait(0.02):
                        for connection in connections:
                            try:
                                connection.sendall(b"x")
                            except OSError:
                                pass

                thread = threading.Thread(target=drip, daemon=True)
                thread.start()
                for connection in connections:
                    try:
                        self.assertEqual(connection.recv(1024), b"")
                    except ConnectionError:
                        pass
                source.assert_not_called()
                self.assertLess(time.monotonic() - started, 1.5)
        finally:
            stop.set()
            for connection in connections:
                connection.close()
        deadline = time.monotonic() + 1
        while True:
            try:
                self.assertEqual(self.request(path="/healthz")[0], 200)
                break
            except (ConnectionError, http.client.RemoteDisconnected):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def test_header_deadline_stops_before_legitimate_processing(self):
        original = self.server.source.result

        def processing():
            time.sleep(0.12)
            return original()

        with mock.patch("site_contract_api.server.HEADER_BUDGET", 0.05), mock.patch.object(self.server.source, "result", side_effect=processing):
            self.assertEqual(self.request()[0], 200)

    def test_body_drip_has_an_overall_deadline(self):
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1)
        stop = threading.Event()
        try:
            with mock.patch("site_contract_api.server.BODY_BUDGET", 0.15):
                headers = (f"POST /api/preview HTTP/1.1\r\nHost: {self.host}\r\nOrigin: http://{self.host}\r\n"
                           f"Cookie: {self.server.cookie_name}={self.server.token}\r\nX-CSRF-Token: {self.server.token}\r\n"
                           "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n")
                connection.sendall(headers.encode())

                def drip():
                    while not stop.wait(0.02):
                        try:
                            connection.sendall(b" ")
                        except OSError:
                            break

                thread = threading.Thread(target=drip, daemon=True)
                thread.start()
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 408)
                self.assertEqual(json.loads(response.read())["error"], "timeout")
        finally:
            stop.set()
            connection.close()
        self.assertEqual(self.request()[0], 200)

    def test_unsupported_methods_are_json_with_security_headers(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT", "BREW"):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/sensitive-fixture-marker")
                self.assertIn(status, (405, 501))
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertFalse(json.loads(body)["ok"])
                for marker in (b"<html", b"sensitive-fixture-marker", method.encode()):
                    self.assertNotIn(marker, body)
        self.assertEqual(self.request("HEAD", "/")[2], b"")

    def test_protocol_errors_do_not_echo_input_or_stdlib_html(self):
        for request in (b"GET /sensitive-fixture-marker HTTP/9.9\r\n\r\n", b"GET /sensitive-fixture-marker HTTP/1.1 EXTRA\r\n\r\n"):
            connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1)
            try:
                connection.sendall(request)
                response = http.client.HTTPResponse(connection)
                response.begin()
                body = response.read()
                self.assertEqual(response.getheader("Content-Type"), "application/json")
                self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
                self.assertNotIn(b"sensitive-fixture-marker", body)
                self.assertNotIn(b"<html", body)
                self.assertNotIn("Python", response.getheader("Server"))
                self.assertEqual(json.loads(body)["error"], "http_request")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
