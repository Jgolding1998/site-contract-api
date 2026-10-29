import http.client
from email.message import Message
import unittest
import socket
import threading
import time
from unittest import mock

from site_contract_api import remote


def addresses(*values):
    return [(2, 1, 6, "", (value, 443)) for value in values]


class FakeResponse:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status, self.body = status, body
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, amount):
        part, self.body = self.body[:amount], self.body[amount:]
        return part


class RemoteTests(unittest.TestCase):
    def test_target_restrictions(self):
        self.assertEqual(remote.validate_target("https://example.org/catalog", "example.org"), ("example.org", "/catalog"))
        bad = ["http://example.org/", "https://example.org/?key=fixture", "https://example.org/#x",
               "https://example.org:444/", "https://other.example/", "https://example.org/a b"]
        bad.append("https://" + "user:pass" + "@example.org/")
        for url in bad:
            with self.subTest(url=url.split("?", 1)[0]), self.assertRaises(remote.RemoteError):
                remote.validate_target(url, "example.org")
        with self.assertRaises(remote.RemoteError):
            remote.validate_target("https://example.org/", None)

    def test_all_dns_results_must_be_public(self):
        for values in [("127.0.0.1",), ("10.0.0.1",), ("169.254.169.254",), ("::1",),
                       ("1.1.1.1", "192.168.1.2"), ("2002:7f00:1::",), ("100.64.0.1",)]:
            with self.subTest(values=values), mock.patch.object(remote.socket, "getaddrinfo", return_value=addresses(*values)), self.assertRaises(remote.RemoteError):
                remote.public_addresses("example.org")
        with mock.patch.object(remote.socket, "getaddrinfo", return_value=addresses("1.1.1.1")):
            self.assertEqual(remote.public_addresses("example.org"), ["1.1.1.1"])

    def test_ipv6_special_use_ranges_are_denied_across_python_versions(self):
        blocked = ("fec0::1", "2001:10::1", "2001:20::1", "2001:2::1", "64:ff9b::7f00:1",
                   "64:ff9b:1::1", "3fff::1", "2001:db8::1", "::ffff:127.0.0.1")
        for value in blocked:
            with self.subTest(value=value), mock.patch.object(remote.socket, "getaddrinfo", return_value=addresses(value)), self.assertRaises(remote.RemoteError):
                remote.public_addresses("example.org")
        for value in ("2606:4700:4700::1111", "::ffff:1.1.1.1"):
            with self.subTest(value=value), mock.patch.object(remote.socket, "getaddrinfo", return_value=addresses(value)):
                self.assertEqual(remote.public_addresses("example.org"), [value])

    def test_connection_pins_address_but_preserves_tls_hostname(self):
        context, raw = mock.Mock(), mock.Mock()
        with mock.patch.object(remote.ssl, "create_default_context", return_value=context), mock.patch.object(remote.socket, "create_connection", return_value=raw) as connect:
            connection = remote.PinnedConnection("example.org", "1.1.1.1", 5)
            connection.connect()
        connect.assert_called_once_with(("1.1.1.1", 443), timeout=5)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="example.org")

    def test_expired_connection_deadline_does_not_start_a_socket(self):
        with mock.patch.object(remote.socket, "create_connection") as connect:
            connection = remote.PinnedConnection("example.org", "1.1.1.1", 5)
            connection.deadline = time.monotonic() - 1
            with self.assertRaises(TimeoutError):
                connection.connect()
            connect.assert_not_called()

    def test_stalled_dns_is_bounded_and_does_not_add_resolver_threads(self):
        finish = threading.Event()

        def stall(*args, **kwargs):
            finish.wait(timeout=1)
            return addresses("1.1.1.1")

        try:
            with mock.patch.object(remote, "DNS_BUDGET", 0.02), mock.patch.object(remote.socket, "getaddrinfo", side_effect=stall) as resolve:
                with self.assertRaises(remote.RemoteError):
                    remote.public_addresses("example.org")
                with self.assertRaises(remote.RemoteError):
                    remote.public_addresses("example.org")
                self.assertEqual(resolve.call_count, 1)
        finally:
            finish.set()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if remote.DNS_SLOT.acquire(blocking=False):
                    remote.DNS_SLOT.release()
                    break
                time.sleep(0.001)

    def test_redirect_compression_size_and_truncation_rejected(self):
        cases = [FakeResponse(302), FakeResponse(headers={"Content-Encoding": "gzip"}),
                 FakeResponse(headers={"Content-Length": "999"}), FakeResponse(body=b"x", headers={"Content-Length": "2"})]
        for response in cases:
            connection = mock.Mock(sock=None)
            connection.getresponse.return_value = response
            with self.subTest(status=response.status), mock.patch.object(remote, "PinnedConnection", return_value=connection), self.assertRaises(remote.RemoteError):
                remote.bounded_get("example.org", "1.1.1.1", "/", 16)
            connection.close.assert_called_once()

    def test_body_stream_limit_and_time_budget(self):
        connection = mock.Mock(sock=None)
        connection.getresponse.return_value = FakeResponse(body=b"x" * 20)
        with mock.patch.object(remote, "PinnedConnection", return_value=connection), self.assertRaises(remote.RemoteError):
            remote.bounded_get("example.org", "1.1.1.1", "/", 8)
        with mock.patch.object(remote, "PinnedConnection", return_value=connection), mock.patch.object(remote.time, "monotonic", side_effect=[0, 11]), self.assertRaises(remote.RemoteError) as caught:
            remote.bounded_get("example.org", "1.1.1.1", "/", 8)
        self.assertEqual(caught.exception.code, "remote_timeout")

    def test_robots_denial_never_fetches_page_and_failure_is_rate_limited(self):
        source = remote.RemoteSource("https://example.org/catalog", "example.org")
        with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), mock.patch.object(remote, "bounded_get", return_value=(200, b"User-agent: *\nDisallow: /", "text/plain")) as get:
            for _ in range(2):
                with self.assertRaises(remote.RemoteError) as caught:
                    source.read()
                self.assertEqual(caught.exception.code, "robots_denied")
            self.assertEqual(get.call_count, 1)

    def test_allowed_page_is_cached_and_uses_same_pinned_address(self):
        source = remote.RemoteSource("https://example.org/catalog", "example.org")
        with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]) as dns, mock.patch.object(remote, "bounded_get", side_effect=[(404, b"", "text/plain"), (200, b"<p>ok</p>", "text/html")]) as get, mock.patch.object(remote.time, "sleep") as sleep:
            self.assertEqual(source.read(), "<p>ok</p>")
            timestamp = source.retrieved_at
            self.assertEqual(source.read(), "<p>ok</p>")
            self.assertEqual(source.retrieved_at, timestamp)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args_list[0].args[1], get.call_args_list[1].args[1])
            dns.assert_called_once()
            sleep.assert_called_once_with(1)

    def test_unavailable_robots_and_large_crawl_delay_fail_closed(self):
        for reply in [(503, b"", "text/plain"), (200, b"User-agent: *\nCrawl-delay: 20", "text/plain")]:
            source = remote.RemoteSource("https://example.org/", "example.org")
            with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), mock.patch.object(remote, "bounded_get", return_value=reply) as get, self.assertRaises(remote.RemoteError):
                source.read()
            get.assert_called_once()

    def test_interval_validation(self):
        for value in (0, 59, 86401, True):
            with self.assertRaises(remote.RemoteError):
                remote.RemoteSource("https://example.org/", "example.org", value)

    def test_slow_response_headers_are_interrupted_by_total_deadline(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        port = listener.getsockname()[1]
        stop = threading.Event()

        def drip():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                    while not stop.wait(0.02):
                        connection.sendall(b"x")
            except OSError:
                pass

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        original_connect = socket.create_connection
        context = mock.Mock()
        context.wrap_socket.side_effect = lambda raw, server_hostname: raw
        started = time.monotonic()
        try:
            with mock.patch.object(remote, "HTTPS_BUDGET", 0.15), mock.patch.object(remote.ssl, "create_default_context", return_value=context), mock.patch.object(remote.socket, "create_connection", side_effect=lambda address, timeout: original_connect(("127.0.0.1", port), timeout=timeout)), self.assertRaises(remote.RemoteError) as caught:
                remote.bounded_get("example.org", "1.1.1.1", "/", 1024)
            self.assertEqual(caught.exception.code, "remote_timeout")
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=1)

    def test_concurrent_refresh_returns_cached_result_after_owner_finishes(self):
        source = remote.RemoteSource("https://example.org/", "example.org")
        entered, finish = threading.Event(), threading.Event()
        results = []

        def get(host, address, path, limit):
            if path == "/robots.txt":
                return 404, b"", "text/plain"
            entered.set()
            finish.wait(timeout=1)
            return 200, b"<p>fixture</p>", "text/html"

        with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), mock.patch.object(remote, "bounded_get", side_effect=get) as fetch, mock.patch.object(remote.time, "sleep"):
            first = threading.Thread(target=lambda: results.append(source.read()))
            second = threading.Thread(target=lambda: results.append(source.read()))
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            finish.set()
            first.join(timeout=1)
            second.join(timeout=1)
        self.assertEqual(results, ["<p>fixture</p>", "<p>fixture</p>"])
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
