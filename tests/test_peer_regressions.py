import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from site_contract_api import engine, remote, server
from site_contract_api.__main__ import main


RULES = {"item_selector": "a", "fields": [{"name": "title", "selector": ":scope"}]}


def amplification_fixture():
    branch = '<div class="row">' * 50 + "\x01" * 2048 + "</div>" * 50
    return {"html": branch * 4, "rules": {"item_selector": ".row", "max_items": 200,
            "fields": [{"name": f"field_{index}", "selector": ":scope"} for index in range(16)]}}


class EngineRegressionTests(unittest.TestCase):
    def test_nested_items_with_repeated_control_values_stop_before_large_output(self):
        fixture = amplification_fixture()
        self.assertLess(len(fixture["html"].encode()), 15000)
        with self.assertRaises(engine.ContractError) as error:
            engine.extract(**fixture)
        self.assertEqual(error.exception.code, "output_limit")
        self.assertNotIn("items", error.exception.as_dict())

    def test_lone_surrogate_html_is_a_controlled_error(self):
        with self.assertRaises(engine.ContractError) as error:
            engine.extract("<a>\ud800</a>", RULES)
        self.assertEqual(error.exception.code, "html_limit")

    def test_ambiguous_optional_closing_tags_fail_instead_of_merging_text(self):
        fixtures = [
            ('<table><tr><td class="first">1<td>Two</table>', {"item_selector": "tr", "fields": [{"name": "title", "selector": ".first"}]}),
            ("<ul><li>One<li>Two</ul>", {"item_selector": "li", "fields": [{"name": "title", "selector": ":scope"}]}),
            ('<article><p class="name">Alpha<div>5</div></article>', {"item_selector": "article", "fields": [{"name": "title", "selector": ".name"}]}),
            ("<ul><li>One</ul>", {"item_selector": "li", "fields": [{"name": "title", "selector": ":scope"}]}),
            ("<p>One", {"item_selector": "p", "fields": [{"name": "title", "selector": ":scope"}]}),
        ]
        for html, rules in fixtures:
            with self.subTest(html=html), self.assertRaises(engine.ContractError) as error:
                engine.extract(html, rules)
            self.assertEqual(error.exception.code, "html")

    def test_explicit_nested_list_and_table_closings_preserve_fields(self):
        result = engine.extract('<table><tr><td class="first">1</td><td>Two</td></tr></table>',
                                {"item_selector": "tr", "fields": [{"name": "title", "selector": ".first"}]})
        self.assertEqual(result["items"], [{"title": "1"}])
        result = engine.extract("<ul><li>Outer<ul><li>Inner</li></ul></li></ul>",
                                {"item_selector": "li", "fields": [{"name": "title", "selector": ":scope"}]})
        self.assertEqual([row["title"] for row in result["items"]], ["Outer Inner", "Inner"])

    def test_nested_links_fail_instead_of_merging_text(self):
        rules = {"item_selector": "li.row", "fields": [{"name": "label", "selector": "a.first"}]}
        with self.assertRaises(engine.ContractError) as caught:
            engine.extract('<ul><li class="row"><a class="first" href="/a">One<a class="second" href="/b">Two</a></li></ul>', rules)
        self.assertEqual(caught.exception.code, "html")
        result = engine.extract('<ul><li class="row"><a class="first" href="/a">One</a><a href="/b">Two</a></li></ul>', rules)
        self.assertEqual(result["items"], [{"label": "One"}])
        cell = '<a class="first" href="/a">Outer<table><tr><td><a href="/b">Cell</a></td></tr></table></a>'
        self.assertEqual(engine.extract(f'<ul><li class="row">{cell}</li></ul>', rules)["items"], [{"label": "Outer Cell"}])

    def test_cli_json_works_with_a_windows_legacy_output_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            source, rules = Path(directory) / "page.html", Path(directory) / "rules.json"
            source.write_text("<a>温度 🔧</a>", encoding="utf-8")
            rules.write_text(json.dumps(RULES), encoding="utf-8")
            buffer = io.BytesIO()
            output = io.TextIOWrapper(buffer, encoding="cp1252")
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["preview", "--source", str(source), "--rules", str(rules)]), 0)
            output.flush()
            encoded = buffer.getvalue()
            self.assertEqual(json.loads(encoded.decode("ascii"))["items"], [{"title": "温度 🔧"}])


class RemoteRegressionTests(unittest.TestCase):
    def test_malformed_robots_rates_fail_closed_and_cache_the_same_error_code(self):
        policies = [b"User-agent: *\nRequest-rate: 0/1\n", b"User-agent: *\nCrawl-delay: \xc2\xb2\n", b"User-agent: *\nRequest-rate: 1/\xc2\xb2\n"]
        for policy in policies:
            with self.subTest(policy=policy):
                source = remote.RemoteSource("https://example.invalid/catalog", "example.invalid")
                with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), mock.patch.object(remote, "bounded_get", return_value=(200, policy, "text/plain")) as fetch, mock.patch.object(remote.time, "sleep") as sleep:
                    for _ in range(2):
                        with self.assertRaises(remote.RemoteError) as error:
                            source.read()
                        self.assertEqual(error.exception.code, "robots_invalid")
                    fetch.assert_called_once()
                    sleep.assert_not_called()
                self.assertEqual(source.last_error[0], "robots_invalid")

    def test_targets_that_robots_cannot_check_are_rejected_before_any_request(self):
        policy = b"User-agent: *\nDisallow: /private\n"
        for url in ("https://example.invalid/public/../private/data", "https://example.invalid/public/%2e%2E/private/data",
                    "https://example.invalid/./private", "https://example.invalid/café"):
            with self.subTest(url=ascii(url)), mock.patch.object(remote, "bounded_get", return_value=(200, policy, "text/plain")) as fetch:
                with self.assertRaises(remote.RemoteError) as error:
                    remote.RemoteSource(url, "example.invalid")
                self.assertEqual(error.exception.code, "remote_target")
                fetch.assert_not_called()
        self.assertEqual(remote.validate_target("https://example.invalid/caf%C3%A9/.well-known", "example.invalid")[1], "/caf%C3%A9/.well-known")

    def test_cached_errors_do_not_retain_or_grow_exception_tracebacks(self):
        source = remote.RemoteSource("https://example.invalid/catalog", "example.invalid")
        previous, depths = None, []
        with mock.patch.object(remote, "public_addresses", side_effect=remote.RemoteError("remote_dns", "Destination unavailable.")) as resolve:
            for _ in range(250):
                try:
                    source.read()
                except remote.RemoteError as error:
                    self.assertIsNot(error, previous)
                    depth, traceback = 0, error.__traceback__
                    while traceback:
                        depth, traceback = depth + 1, traceback.tb_next
                    depths.append(depth)
                    previous = error
                else:
                    self.fail("A cached remote error was expected")
            resolve.assert_called_once()
        self.assertLessEqual(max(depths), 5)
        self.assertIsInstance(source.last_error, tuple)
        self.assertNotIsInstance(source.last_error, BaseException)


class HTTPRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        source = Path(self.directory.name) / "source.html"
        source.write_text("<a>Valid</a>", encoding="utf-8")
        self.server = server.Server(("127.0.0.1", 0), server.Source(RULES, source), "<a>Valid</a>", RULES)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, payload=None, cookie=None, path="/api/preview"):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        host = f"127.0.0.1:{self.server.server_port}"
        headers = {"Host": host, "Origin": "http://" + host, "X-CSRF-Token": self.server.token,
                   "Cookie": cookie if cookie is not None else self.server.cookie_name + "=" + self.server.token,
                   "Content-Type": "application/json"}
        client.request("POST" if payload else "GET", path, json.dumps(payload) if payload else None, headers)
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def test_unrelated_malformed_cookies_do_not_break_the_exact_csrf_cookie(self):
        payload = {"html": "<a>Valid</a>", "rules": RULES}
        name = self.server.cookie_name
        for prefix in ['theme={"dark":true}; ', 'a=b c; ', 'pref="x; ', 'x=[1,2]; ', 'k=v=w; ', 'n=a\\b; ', 'q="a"b; ', 'sca_csrf=legacy; ', 'sca_csrf_1=unrelated; ']:
            with self.subTest(prefix=prefix):
                self.assertEqual(self.request(payload, prefix + name + "=" + self.server.token)[0], 200)
        for cookie in ["other_" + name + "=" + self.server.token, "foo=" + name + "=" + self.server.token,
                       name + "=" + self.server.token + "; " + name + "=" + self.server.token,
                       name + "=" + self.server.token + "; " + name + "=invalid", "sca_csrf=" + self.server.token]:
            with self.subTest(cookie=cookie[:20]):
                self.assertEqual(self.request(payload, cookie)[0], 403)

    def test_amplification_returns_a_small_error_and_releases_the_processing_slot(self):
        status, headers, body = self.request(amplification_fixture())
        self.assertEqual(status, 413)
        self.assertLess(len(body), 512)
        self.assertEqual(json.loads(body)["error"], "output_limit")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.request({"html": "<a>Valid</a>", "rules": RULES})[0], 200)

    def test_serializer_caps_arbitrary_result_values_before_headers_are_written(self):
        with mock.patch.object(self.server.source, "result", return_value={"items": ["\x01" * 3000]}), mock.patch.object(server, "MAX_RESPONSE", 1024):
            status, _, body = self.request(path="/api/data")
        self.assertEqual(status, 413)
        self.assertLess(len(body), 512)
        self.assertEqual(json.loads(body)["error"], "output_limit")

    def test_invalid_robots_returns_fixed_errors_on_initial_and_cached_requests(self):
        self.server.source.remote = remote.RemoteSource("https://example.invalid/catalog", "example.invalid")
        with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), mock.patch.object(remote, "bounded_get", return_value=(200, b"User-agent: *\nRequest-rate: 0/1\n", "text/plain")) as fetch:
            for _ in range(2):
                status, _, body = self.request(path="/api/data")
                self.assertEqual(status, 502)
                self.assertEqual(json.loads(body)["error"], "robots_invalid")
                self.assertNotIn(b"Traceback", body)
            fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
