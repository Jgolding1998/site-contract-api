"""Regression tests for configured-source limit statuses and malformed-markup handling.

All inputs are synthetic. Nothing here contacts a network service.
"""
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from site_contract_api import engine, remote, server

CANARY = "CANARYMARKEDSECTION7Q"
HTML = '<article><span class="name">Alpha</span></article>'
RULES = {"item_selector": "article", "fields": [{"name": "name", "selector": ".name"}]}


class EnvelopeGuardTests(unittest.TestCase):
    def test_wide_containers_are_rejected_before_json_decoding(self):
        for text in ('[' + ','.join(['{}'] * 10000) + ']', '{' + ','.join(f'"key{i}":0' for i in range(10000)) + '}'):
            with self.subTest(prefix=text[:1]), self.assertRaises(ValueError):
                server.guard_envelope(text)

    def test_maximum_html_with_escaped_syntax_is_not_treated_as_structure(self):
        page = '\\"[]{}:,\n' * 500000
        document = json.dumps({'html':page,'rules':RULES})
        server.guard_envelope(document)


class MalformedMarkupTests(unittest.TestCase):
    def test_parser_assertions_become_fixed_html_errors(self):
        for method in ("feed", "close"):
            with self.subTest(method=method), mock.patch.object(engine.Document, method, side_effect=AssertionError(f"unknown status keyword {CANARY!r}")):
                with self.assertRaises(engine.ContractError) as caught:
                    engine.extract(HTML, RULES)
            self.assertEqual(caught.exception.code, "html")
            self.assertNotIn(CANARY, json.dumps(caught.exception.as_dict()))
            self.assertNotIn("items", caught.exception.as_dict())

    def test_real_marked_sections_parse_or_fail_without_reflection(self):
        # Older parsers raise AssertionError for these; newer parsers treat them as bogus comments.
        for markup in (f"<![{CANARY}[ hidden ]]>", f"<![ {CANARY}", f"<!{CANARY}[", "<![if !supportLists]><![endif]>"):
            page = "<article>" + markup + '<span class="name">Alpha</span></article>'
            with self.subTest(markup=markup.replace(CANARY, "canary")):
                try:
                    result = engine.extract(page, RULES)
                except engine.ContractError as exc:
                    self.assertNotIn(CANARY, json.dumps(exc.as_dict()))
                else:
                    self.assertNotIn(CANARY, json.dumps(result["items"]))


class ConfiguredSourceStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "source.html"
        self.path.write_text(HTML, encoding="utf-8")
        self.server = server.Server(("127.0.0.1", 0), server.Source(RULES, self.path), HTML, RULES)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path="/api/data", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Host": self.host}
        if method == "POST":
            headers.update({"Origin": "http://" + self.host, "Content-Type": "application/json",
                            "X-CSRF-Token": self.server.token, "Cookie": self.server.cookie_name + "=" + self.server.token})
        try:
            connection.request(method, path, body.encode() if isinstance(body, str) else body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_oversized_configured_file_returns_413_with_numbers_only(self):
        self.path.write_bytes(b"x" * (engine.MAX_HTML + len(engine.UTF8_BOM) + 1))
        status, value = self.request()
        self.assertEqual(status, 413)
        self.assertEqual(value["error"], "html_limit")
        self.assertEqual(value["issues"], [{"code": "html_bytes", "limit": engine.MAX_HTML}])
        self.assertNotIn(self.directory.name, json.dumps(value))
        # Within the reader's byte-order-mark allowance, the exact analyzed size is reported.
        self.path.write_bytes(b"x" * (engine.MAX_HTML + 2))
        status, value = self.request()
        self.assertEqual(status, 413)
        self.assertEqual(value["issues"], [{"code": "html_bytes", "limit": engine.MAX_HTML, "actual": engine.MAX_HTML + 2}])
        self.path.write_text(HTML, encoding="utf-8")
        self.assertEqual(self.request()[0], 200)

    def test_configured_remote_limits_return_413_and_encoding_stays_upstream_failure(self):
        capture = SimpleNamespace(url="https://example.invalid/catalog", retrieved_at=None, read=mock.Mock(return_value="x" * (engine.MAX_HTML + 1)))
        self.server.source.remote = capture
        status, value = self.request()
        self.assertEqual((status, value["error"]), (413, "html_limit"))
        capture.read.side_effect = remote.RemoteError("remote_limit", "Remote response is larger than the limit.", [{"code": "remote_bytes", "limit": 1}])
        status, value = self.request()
        self.assertEqual((status, value["error"]), (413, "remote_limit"))
        capture.read.side_effect = remote.RemoteError("html_encoding", "Remote HTML is not valid UTF-8.", [{"code": "invalid_utf8", "offset": 3}])
        status, value = self.request()
        self.assertEqual((status, value["error"]), (502, "html_encoding"))

    def test_configured_malformed_markup_is_controlled(self):
        self.path.write_text(f"<article><![{CANARY}[ x ]]><span class=\"name\">Alpha</span></article>", encoding="utf-8")
        status, value = self.request()
        self.assertIn(status, (200, 502))
        self.assertNotIn(CANARY, json.dumps(value))
        with mock.patch.object(engine.Document, "feed", side_effect=AssertionError(CANARY)):
            self.path.write_text(HTML + " ", encoding="utf-8")
            status, value = self.request()
        self.assertEqual((status, value["error"]), (502, "html"))
        self.assertNotIn(CANARY, json.dumps(value))

    def test_preview_malformed_markup_and_envelope_errors_do_not_reflect_input(self):
        with mock.patch.object(engine.Document, "feed", side_effect=AssertionError(CANARY)):
            status, value = self.request("POST", "/api/preview", json.dumps({"html": HTML, "rules": RULES}))
        self.assertEqual((status, value["error"]), (422, "html"))
        self.assertNotIn(CANARY, json.dumps(value))
        for body, expected in (('{"html":"a","html":"b","rules":{}}', (422, "json")),
                               ("[" * 100000 + "]" * 100000, (400, "json")),
                               ('{"html":"' + CANARY + '","rules":' + "[" * 100000 + "]" * 100000 + "}", (400, "json"))):
            with self.subTest(size=len(body)):
                status, value = self.request("POST", "/api/preview", body)
                self.assertEqual((status, value["error"]), expected)
                self.assertNotIn(CANARY, json.dumps(value))

    def test_published_limits_stay_coherent_across_readers(self):
        status, value = self.request(path="/api/status")
        self.assertEqual(status, 200)
        limits = value["limits"]
        self.assertEqual(remote.MAX_PAGE, limits["html_bytes"] + len(engine.UTF8_BOM))
        self.assertGreaterEqual(limits["request_bytes"], limits["html_b64_chars"] + 6 * limits["rules_bytes"])
        self.assertLessEqual(limits["output_bytes"], limits["request_bytes"])
        self.assertEqual(limits["attribute_bytes"], engine.MAX_ATTRIBUTE_BYTES)


if __name__ == "__main__":
    unittest.main()
