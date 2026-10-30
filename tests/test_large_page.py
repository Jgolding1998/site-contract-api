"""Large static pages: byte limits, attribute limits, html_b64 previews and source caps.

Every fixture is generated here. No test contacts a real website.
"""
import base64
import functools
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from site_contract_api import engine, remote, server
from site_contract_api.__main__ import read_html, read_text
from .test_remote import FakeResponse

MIB = 1024 * 1024
PLAN_RULES = {"item_selector": "article.plan", "expected_items": 3, "fields": [
    {"name": "name", "selector": "h3.name"},
    {"name": "price", "selector": ".price", "attribute": "data-price", "type": "number"},
    {"name": "url", "selector": "a.details", "attribute": "href", "type": "url"}]}
HERO_RULES = {"item_selector": "main", "fields": [{"name": "headline", "selector": "h1"}]}
SIMPLE_HTML = '<article><span class="name">Café</span></article>'
SIMPLE_RULES = {"item_selector": "article", "fields": [{"name": "name", "selector": ".name"}]}
EXPECTED_PLANS = [{"name": "Starter", "price": 0.0, "url": "https://example.invalid/plans/starter"},
                  {"name": "Équipe", "price": 450.0, "url": "https://example.invalid/plans/team"},
                  {"name": "Scale", "price": 2000.5, "url": "https://example.invalid/plans/scale"}]


def card(slug, name, price):
    return (f'<article class="plan"><h3 class="name">{name}</h3><span class="price" data-price="{price}">${price}</span>'
            f'<a class="details" href="/plans/{slug}">Details</a></article>')


def svg_path(length=28 * 1024):
    steps = "L10 10 " * ((length - 4) // 7 + 1)
    return ("M0 0" + steps)[:length]


@functools.lru_cache(maxsize=None)
def marketing_page():
    """A fictional marketing page: over 1 MiB of style and script, one large SVG path, three visible plans."""
    style = "<style>" + ".hero-gradient{background:linear-gradient(90deg,#1d4aff,#f54e00)}\n" * 4500 + "</style>"
    record = json.dumps({"slug": "product-analytics", "title": "Autocapture", "summary": 'Track "every" click \\ and view',
                         "markup": card("injected", "Injected", "9")})
    state = '<script id="__STATE__" type="application/json">{"entries":[' + ",".join([record] * 3500) + "]}</script>"
    writer = "<script>document.write('" + card("written", "Script plan", "1") + "');</script>"
    flight = ("<script>self.__flight=self.__flight||[];self.__flight.push([1,"
              + json.dumps('Hydration chunk with "quotes" and \\slashes\\ ' * 6000) + "]);</script>")
    raw = style + state + writer + flight
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Fictional analytics suite</title>' + style + state + "</head>"
            '<body><header><nav><a class="nav" href="/docs">Docs</a></nav></header>'
            "<main><h1>Ship product data you can trust</h1>"
            f'<svg viewBox="0 0 100 100" aria-hidden="true"><path d="{svg_path()}"></path></svg>'
            '<section class="pricing">' + card("starter", "Starter", "0") + card("team", "Équipe", "450.00")
            + card("scale", "Scale", "2000.50") + "</section>" + writer + flight + "</main></body></html>")
    return page, len(raw.encode())


@functools.lru_cache(maxsize=None)
def exact_page(size=None):
    """SIMPLE_HTML padded with a two-byte-character comment to exactly `size` UTF-8 bytes."""
    size = engine.MAX_HTML if size is None else size
    remaining = size - len(SIMPLE_HTML.encode()) - len("<!---->")
    page = SIMPLE_HTML + "<!--" + "é" * (remaining // 2) + "x" * (remaining % 2) + "-->"
    assert len(page.encode()) == size
    return page


@functools.lru_cache(maxsize=None)
def quote_heavy_page():
    """Just under the HTML limit, dominated by quotes and backslashes that JSON escaping enlarges."""
    head = '<main><section class="pricing">' + card("starter", "Starter", "0") + card("team", "Équipe", "450.00") + card("scale", "Scale", "2000.50") + "</section>"
    unit = '["\\"q\\"","\\\\"],'
    tail = "0]</script></main>"
    count = (engine.MAX_HTML - 64 * 1024 - len(head) - len(tail)) // len(unit)
    return head + '<script type="application/json">[' + unit * count + tail


def b64(data):
    return base64.b64encode(data).decode("ascii")


class EngineLimitTests(unittest.TestCase):
    def test_generated_marketing_page_extracts_visible_fields_without_running_scripts(self):
        page, raw_bytes = marketing_page()
        data = page.encode()
        self.assertGreater(len(data), MIB)
        self.assertGreater(raw_bytes, MIB)
        self.assertGreater(len(svg_path()), 28_000)
        result = engine.extract(page, PLAN_RULES)
        self.assertEqual(result["items"], EXPECTED_PLANS)
        self.assertNotIn("Script plan", json.dumps(result))
        self.assertNotIn("Injected", json.dumps(result))
        provenance = result["provenance"]
        self.assertEqual(provenance["content_model"], "static_html_no_js")
        self.assertEqual(provenance["source_bytes"], len(data))
        self.assertEqual(provenance["source_hash"], hashlib.sha256(data).hexdigest())
        self.assertEqual(provenance["script_elements"], 3)
        self.assertLess(provenance["html_elements"], engine.MAX_NODES)
        self.assertEqual(engine.extract(page, HERO_RULES)["items"], [{"headline": "Ship product data you can trust"}])

    def test_previous_one_mib_and_4_kib_attribute_limits_would_reject_the_page(self):
        page, _ = marketing_page()
        with mock.patch.object(engine, "MAX_HTML", MIB), self.assertRaises(engine.ContractError) as caught:
            engine.extract(page, PLAN_RULES)
        self.assertEqual(caught.exception.issues, [{"code": "html_bytes", "limit": MIB, "actual": len(page.encode())}])
        with mock.patch.object(engine, "MAX_ATTRIBUTE_BYTES", 4096), self.assertRaises(engine.ContractError) as caught:
            engine.extract(page, PLAN_RULES)
        self.assertEqual(caught.exception.issues[0]["code"], "html_attribute_bytes")
        self.assertEqual(caught.exception.issues[0]["actual"], len(svg_path()))

    def test_html_byte_limit_is_exact_and_counts_utf8(self):
        self.assertEqual(engine.MAX_HTML, 4 * MIB)
        self.assertEqual(engine.extract(exact_page(), SIMPLE_RULES)["items"], [{"name": "Café"}])
        with self.assertRaises(engine.ContractError) as caught:
            engine.extract(exact_page() + "x", SIMPLE_RULES)
        self.assertEqual(caught.exception.code, "html_limit")
        self.assertEqual(caught.exception.issues, [{"code": "html_bytes", "limit": engine.MAX_HTML, "actual": engine.MAX_HTML + 1}])
        self.assertIn("4,194,305", caught.exception.message)
        self.assertNotIn("Caf", caught.exception.message)
        wide = SIMPLE_HTML + "<!--" + "é" * (engine.MAX_HTML // 2) + "-->"
        self.assertLess(len(wide), engine.MAX_HTML)
        with self.assertRaises(engine.ContractError) as caught:
            engine.extract(wide, SIMPLE_RULES)
        self.assertEqual(caught.exception.issues[0]["actual"], len(wide.encode()))

    def test_attribute_value_limit_is_exact_and_counts_utf8(self):
        limit = engine.MAX_ATTRIBUTE_BYTES
        self.assertEqual(limit, 64 * 1024)
        for value in ("a" * limit, "é" * (limit // 2)):
            html = f'<article title="{value}"><span class="name">A</span></article>'
            self.assertEqual(engine.extract(html, SIMPLE_RULES)["items"], [{"name": "A"}])
        for value in ("CANARY" + "a" * (limit - 5), "CANARY" + "é" * ((limit - 6) // 2) + "a"):
            self.assertEqual(len(value.encode()), limit + 1)
            html = f'<article>\n<span class="name" title="{value}">A</span></article>'
            with self.subTest(chars=len(value)), self.assertRaises(engine.ContractError) as caught:
                engine.extract(html, SIMPLE_RULES)
            self.assertEqual(caught.exception.code, "html_limit")
            self.assertEqual(caught.exception.issues, [{"code": "html_attribute_bytes", "limit": limit, "actual": len(value.encode()), "line": 2}])
            self.assertNotIn("CANARY", json.dumps(caught.exception.as_dict()))

    def test_structure_errors_name_the_limit_kind_without_markup(self):
        cases = [("<div>" * 66, "html_depth"), ("<br>" * (engine.MAX_NODES + 1), "html_elements"),
                 ('<p ' + " ".join(f'data-{i}="x"' for i in range(33)) + "></p>", "html_attributes"),
                 ('<p data-' + "a" * 70 + '="x"></p>', "html_attribute_name")]
        for html, kind in cases:
            with self.subTest(kind=kind), self.assertRaises(engine.ContractError) as caught:
                engine.extract(html, SIMPLE_RULES)
            self.assertEqual(caught.exception.code, "html_limit")
            self.assertEqual(caught.exception.issues[0]["code"], kind)
            self.assertIn("limit", caught.exception.issues[0])
            self.assertNotIn("<", caught.exception.message)
            self.assertNotIn("data-", caught.exception.message)

    def test_invalid_utf8_and_byte_order_mark_are_described_accurately(self):
        with self.assertRaises(engine.ContractError) as caught:
            engine.decode_html(b"<p>ok</p>\xff")
        self.assertEqual(caught.exception.code, "html_encoding")
        self.assertEqual(caught.exception.issues, [{"code": "invalid_utf8", "offset": 9}])
        self.assertIn("not valid UTF-8", caught.exception.message)
        with self.assertRaises(engine.ContractError) as caught:
            engine.decode_html(engine.UTF8_BOM + b"\xc3(")
        self.assertEqual(caught.exception.issues[0]["offset"], 3)
        self.assertEqual(len(engine.decode_html(engine.UTF8_BOM + b"x" * engine.MAX_HTML)), engine.MAX_HTML)
        with self.assertRaises(engine.ContractError) as caught:
            engine.decode_html(engine.UTF8_BOM + b"x" * (engine.MAX_HTML + 1))
        self.assertEqual(caught.exception.issues, [{"code": "html_bytes", "limit": engine.MAX_HTML, "actual": engine.MAX_HTML + 1}])
        with self.assertRaises(engine.ContractError) as caught:
            engine.extract(b"<p>bytes</p>", SIMPLE_RULES)
        self.assertEqual(caught.exception.code, "html")


class LocalSourceLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "page.html"

    def test_configured_file_source_uses_the_same_limits(self):
        source = server.Source(SIMPLE_RULES, self.path)
        self.path.write_bytes(engine.UTF8_BOM + exact_page().encode())
        result = source.result()
        self.assertEqual(result["items"], [{"name": "Café"}])
        self.assertEqual(result["provenance"]["source_bytes"], engine.MAX_HTML)
        self.assertEqual(result["provenance"]["source_hash"], hashlib.sha256(exact_page().encode()).hexdigest())
        for data, kind, actual in ((exact_page().encode() + b"x", "html_bytes", engine.MAX_HTML + 1),
                                   (b"x" * (engine.MAX_HTML + 4), "html_bytes", None)):
            self.path.write_bytes(data)
            with self.assertRaises(engine.ContractError) as caught:
                source.result()
            self.assertEqual(caught.exception.code, "html_limit")
            self.assertEqual(caught.exception.issues[0]["code"], kind)
            self.assertEqual(caught.exception.issues[0].get("actual"), actual)
        self.path.write_bytes(b"<p>\xff</p>")
        with self.assertRaises(engine.ContractError) as caught:
            source.result()
        self.assertEqual(caught.exception.code, "html_encoding")
        self.assertNotIn(self.directory.name, caught.exception.message)

    def test_cli_readers_name_the_input_and_limit_without_paths(self):
        self.path.write_bytes(engine.UTF8_BOM + exact_page().encode())
        self.assertEqual(read_html(self.path), exact_page())
        self.path.write_bytes(exact_page().encode() + b"x")
        with self.assertRaises(engine.ContractError) as caught:
            read_html(self.path)
        self.assertEqual(caught.exception.issues, [{"code": "html_bytes", "limit": engine.MAX_HTML, "actual": engine.MAX_HTML + 1}])
        self.path.write_bytes(b"<p>\xff</p>")
        with self.assertRaises(engine.ContractError) as caught:
            read_html(self.path)
        self.assertEqual(caught.exception.code, "html_encoding")
        self.path.write_bytes(b" " * (engine.MAX_RULES + 1))
        with self.assertRaises(engine.ContractError) as caught:
            read_text(self.path, engine.MAX_RULES, "rules")
        self.assertEqual(caught.exception.code, "file_limit")
        self.assertTrue(caught.exception.message.startswith("The rules file"))
        self.assertIn("32,768", caught.exception.message)
        self.path.write_bytes(engine.UTF8_BOM + b" " * engine.MAX_RULES)
        self.assertEqual(len(read_text(self.path, engine.MAX_RULES, "rules")), engine.MAX_RULES)
        for message in (caught.exception.message, json.dumps(caught.exception.as_dict())):
            self.assertNotIn(self.directory.name, message)


class RemoteLimitTests(unittest.TestCase):
    def test_page_read_limit_follows_html_limit_and_removes_byte_order_mark(self):
        source = remote.RemoteSource("https://example.org/catalog", "example.org")
        page = engine.UTF8_BOM + b"<main><h1>Title</h1></main>"
        with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), \
                mock.patch.object(remote, "bounded_get", side_effect=[(404, b"", "text/plain"), (200, page, "text/html")]) as get, \
                mock.patch.object(remote.time, "sleep"):
            self.assertEqual(source.read(), "<main><h1>Title</h1></main>")
        self.assertEqual(get.call_args_list[0].args[3], remote.MAX_ROBOTS)
        self.assertEqual(get.call_args_list[1].args[3], engine.MAX_HTML + len(engine.UTF8_BOM))
        self.assertEqual(remote.HTTPS_BUDGET, 10)

    def test_invalid_remote_utf8_is_cached_as_fixed_numeric_error(self):
        source = remote.RemoteSource("https://example.org/catalog", "example.org")
        with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), \
                mock.patch.object(remote, "bounded_get", side_effect=[(404, b"", "text/plain"), (200, b"<p>\xff</p>", "text/html")]) as get, \
                mock.patch.object(remote.time, "sleep"):
            for _ in range(2):
                with self.assertRaises(remote.RemoteError) as caught:
                    source.read()
                self.assertEqual(caught.exception.code, "html_encoding")
                self.assertEqual(caught.exception.issues, [{"code": "invalid_utf8", "offset": 3}])
                caught.exception.issues.append({"code": "changed by caller"})
            self.assertEqual(get.call_count, 2)
        self.assertIsInstance(source.last_error, tuple)

    def test_declared_streamed_and_invalid_sizes_are_explicit(self):
        cases = [(FakeResponse(body=b"", headers={"Content-Length": "33"}), "remote_limit", 33),
                 (FakeResponse(body=b"x" * 40), "remote_limit", None),
                 (FakeResponse(headers={"Content-Length": "1x"}), "remote_response", None)]
        for response, code, actual in cases:
            connection = mock.Mock(sock=None)
            connection.getresponse.return_value = response
            with self.subTest(code=code, actual=actual), mock.patch.object(remote, "PinnedConnection", return_value=connection), \
                    self.assertRaises(remote.RemoteError) as caught:
                remote.bounded_get("example.org", "1.1.1.1", "/", 32)
            self.assertEqual(caught.exception.code, code)
            if code == "remote_limit":
                self.assertEqual(caught.exception.issues[0]["limit"], 32)
                self.assertEqual(caught.exception.issues[0].get("actual"), actual)
                self.assertIn("32", caught.exception.message)
            self.assertTrue(response.closed)


class LargePreviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "source.html"
        path.write_text(SIMPLE_HTML, encoding="utf-8")
        self.server = server.Server(("127.0.0.1", 0), server.Source(SIMPLE_RULES, path), SIMPLE_HTML, SIMPLE_RULES)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path="/api/preview", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        if isinstance(body, dict):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        defaults = {"Host": self.host}
        if method == "POST":
            defaults.update({"Origin": "http://" + self.host, "Content-Type": "application/json",
                             "X-CSRF-Token": self.server.token, "Cookie": self.server.cookie_name + "=" + self.server.token})
        try:
            connection.request(method, path, body, headers={**defaults, **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_status_publishes_coordinated_limits(self):
        status, value = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        limits = value["limits"]
        self.assertEqual(limits, server.LIMITS)
        self.assertEqual(limits["html_bytes"], 4 * MIB)
        self.assertEqual(limits["attribute_bytes"], 64 * 1024)
        self.assertEqual((limits["html_elements"], limits["html_depth"], limits["html_attributes"]), (20_000, 64, 32))
        self.assertEqual(limits["output_bytes"], 3 * MIB)
        self.assertEqual(limits["html_b64_chars"], len(b64(b"x" * (engine.MAX_HTML + len(engine.UTF8_BOM)))))
        self.assertGreaterEqual(limits["request_bytes"], limits["html_b64_chars"] + 6 * engine.MAX_RULES)
        self.assertGreaterEqual(limits["request_bytes"], 3 * MIB)
        self.assertEqual(limits["request_bytes"], server.MAX_BODY)

    def test_marketing_page_preview_through_html_b64(self):
        page, _ = marketing_page()
        status, value = self.request("POST", body={"html_b64": b64(page.encode()), "rules": PLAN_RULES})
        self.assertEqual(status, 200)
        self.assertEqual(value["items"], EXPECTED_PLANS)
        self.assertEqual(value["provenance"]["source_hash"], hashlib.sha256(page.encode()).hexdigest())
        status, value = self.request("POST", body={"html_b64": b64(engine.UTF8_BOM + page.encode()), "rules": PLAN_RULES})
        self.assertEqual(status, 200)
        self.assertEqual(value["provenance"]["source_hash"], hashlib.sha256(page.encode()).hexdigest())

    def test_quote_heavy_page_exceeds_legacy_wire_cap_but_fits_html_b64(self):
        page = quote_heavy_page()
        rules = {**PLAN_RULES, "expected_items": 3}
        self.assertLessEqual(len(page.encode()), engine.MAX_HTML)
        legacy = len(json.dumps({"html": page, "rules": rules}).encode())
        self.assertGreater(legacy, server.MAX_BODY)
        status, value = self.request("POST", body="{}", headers={"Content-Length": str(legacy)})
        self.assertEqual(status, 413)
        self.assertEqual(value["error"], "body_limit")
        self.assertIn("html_b64", value["message"])
        self.assertEqual(value["issues"], [{"code": "request_bytes", "limit": server.MAX_BODY, "actual": legacy}])
        status, value = self.request("POST", body={"html_b64": b64(page.encode()), "rules": rules})
        self.assertEqual(status, 200)
        self.assertEqual(value["items"], EXPECTED_PLANS)

    def test_legacy_html_field_keeps_working_with_sized_errors(self):
        status, value = self.request("POST", body={"html": SIMPLE_HTML, "rules": SIMPLE_RULES})
        self.assertEqual((status, value["items"]), (200, [{"name": "Café"}]))
        status, value = self.request("POST", body={"html": "x" * (engine.MAX_HTML + 1), "rules": SIMPLE_RULES})
        self.assertEqual(status, 422)
        self.assertEqual(value["issues"], [{"code": "html_bytes", "limit": engine.MAX_HTML, "actual": engine.MAX_HTML + 1}])

    def test_dual_malformed_and_oversized_html_b64_are_rejected(self):
        encoded = b64(SIMPLE_HTML.encode())
        status, value = self.request("POST", body={"html": SIMPLE_HTML, "html_b64": encoded, "rules": SIMPLE_RULES})
        self.assertEqual((status, value["error"]), (422, "html_input"))
        for bad in ("abc", "ab=c", encoded + "\n", " " + encoded[1:], "QR==", "QQ=", "====", encoded.replace("+", "-") + "-AAA", 123, None):
            with self.subTest(bad=bad if not isinstance(bad, str) else bad[:8]):
                status, value = self.request("POST", body={"html_b64": bad, "rules": SIMPLE_RULES})
                self.assertEqual((status, value["error"]), (422, "html_b64"))
                self.assertNotIn("items", value)
        status, value = self.request("POST", body={"html_b64": "A" * (server.MAX_HTML_B64 + 4), "rules": SIMPLE_RULES})
        self.assertEqual((status, value["error"]), (422, "html_limit"))
        self.assertEqual(value["issues"], [{"code": "html_b64_chars", "limit": server.MAX_HTML_B64, "actual": server.MAX_HTML_B64 + 4}])
        status, value = self.request("POST", body={"html_b64": b64(b"x" * (engine.MAX_HTML + 1)), "rules": SIMPLE_RULES})
        self.assertEqual((status, value["error"]), (422, "html_limit"))
        self.assertEqual(value["issues"], [{"code": "html_bytes", "limit": engine.MAX_HTML, "actual": engine.MAX_HTML + 1}])
        status, value = self.request("POST", body={"html_b64": b64(b"<p>\xff</p>"), "rules": SIMPLE_RULES})
        self.assertEqual((status, value["error"]), (422, "html_encoding"))
        self.assertEqual(value["issues"], [{"code": "invalid_utf8", "offset": 3}])
        for body in ({"html_b64": encoded}, {"rules": SIMPLE_RULES}, {"html_b64": encoded, "rules": SIMPLE_RULES, "url": "x"}):
            self.assertEqual(self.request("POST", body=body)[0], 400)


if __name__ == "__main__":
    unittest.main()
