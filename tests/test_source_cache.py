import copy
import io
import math
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest import mock

from site_contract_api import engine, remote, server


HTML = '<article><span class="name">Alpha</span></article>'
RULES = {"item_selector": "article", "fields": [{"name": "name", "selector": ".name"}]}


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "capture.html"
        self.path.write_text(HTML, encoding="utf-8")
        self.source = server.Source(copy.deepcopy(RULES), self.path)

    def test_unchanged_reads_reuse_validation_with_independent_results(self):
        with mock.patch.object(engine, "datetime") as parsed_clock, mock.patch.object(server, "datetime", create=True) as cached_clock, mock.patch.object(server, "extract", wraps=engine.extract) as extract:
            parsed_clock.now.return_value.isoformat.side_effect = ["2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"]
            cached_clock.now.return_value.isoformat.return_value = "2026-01-01T00:00:02+00:00"
            first = self.source.result()
            first["items"][0]["name"] = "changed by caller"
            first["provenance"]["field_selectors"]["name"] = "changed"
            second = self.source.result()
            self.assertEqual(extract.call_count, 1)
        self.assertEqual(second["items"], [{"name": "Alpha"}])
        self.assertEqual(second["provenance"]["field_selectors"], {"name": ".name"})
        self.assertEqual(second["provenance"]["retrieved_at"], "2026-01-01T00:00:02+00:00")

    def test_same_size_and_mtime_edit_is_read_and_revalidated(self):
        with mock.patch.object(server, "extract", wraps=engine.extract) as extract:
            first = self.source.result()
            metadata = self.path.stat()
            self.path.write_text(HTML.replace("Alpha", "Bravo"), encoding="utf-8")
            os.utime(self.path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            changed = self.source.result()
            self.assertEqual(extract.call_count, 2)
        self.assertEqual(changed["items"], [{"name": "Bravo"}])
        self.assertNotEqual(first["provenance"]["source_hash"], changed["provenance"]["source_hash"])

    def test_cached_typed_values_preserve_numbers_null_and_unicode(self):
        self.path.write_text('<article><span class="number">-0.0</span><span class="integer">1000000000000</span><span class="label">Snow \u2603 and \U0001f680</span></article>', encoding="utf-8")
        rules = {"item_selector": "article", "fields": [
            {"name": "number", "selector": ".number", "type": "number"},
            {"name": "integer", "selector": ".integer", "type": "integer"},
            {"name": "label", "selector": ".label"},
            {"name": "optional", "selector": ".absent", "required": False},
        ]}
        source = server.Source(rules, self.path)
        with mock.patch.object(server, "extract", wraps=engine.extract) as extract:
            first, second = source.result(), source.result()
            self.assertEqual(extract.call_count, 1)
        self.assertEqual(first["items"], second["items"])
        self.assertIs(type(second["items"][0]["number"]), float)
        self.assertEqual(math.copysign(1, second["items"][0]["number"]), -1)
        self.assertIs(type(second["items"][0]["integer"]), int)
        self.assertIsNone(second["items"][0]["optional"])
        self.assertEqual(second["items"][0]["label"], "Snow \u2603 and \U0001f680")

    def test_bom_rewrite_and_rules_changes_do_not_share_keys(self):
        with mock.patch.object(server, "extract", wraps=engine.extract) as extract:
            first = self.source.result()
            self.path.write_bytes(b"\xef\xbb\xbf" + HTML.encode())
            second = self.source.result()
            self.assertEqual(extract.call_count, 2)
            self.assertEqual(first["items"], second["items"])
            self.assertEqual(first["provenance"]["source_hash"], second["provenance"]["source_hash"])
            self.source.rules["fields"][0]["name"] = "title"
            changed = self.source.result()
            self.assertEqual(extract.call_count, 3)
            self.assertEqual(changed["items"], [{"title": "Alpha"}])
            self.assertNotEqual(first["provenance"]["rules_hash"], changed["provenance"]["rules_hash"])

    def test_read_and_validation_failures_evict_previous_success(self):
        for failure in ("unicode", "oversize", "missing", "drift", "rules"):
            with self.subTest(failure=failure):
                self.path.write_text(HTML, encoding="utf-8")
                source = server.Source(copy.deepcopy(RULES), self.path)
                with mock.patch.object(server, "extract", wraps=engine.extract) as extract:
                    source.result()
                    if failure == "unicode": self.path.write_bytes(b"\xff")
                    elif failure == "oversize": self.path.write_bytes(b"x" * (engine.MAX_HTML + 1))
                    elif failure == "missing": self.path.unlink()
                    elif failure == "drift": self.path.write_text(HTML.replace('class="name"', 'class="changed"'))
                    else: source.rules["fields"][0]["required"] = "invalid"
                    with self.assertRaises(engine.ContractError): source.result()
                    count = extract.call_count
                    self.path.write_text(HTML, encoding="utf-8")
                    source.rules = copy.deepcopy(RULES)
                    self.assertEqual(source.result()["items"], [{"name": "Alpha"}])
                    self.assertEqual(extract.call_count, count + 1)

    def test_each_hit_reopens_source_and_reparse_errors_cannot_serve_stale_data(self):
        with mock.patch.object(Path, "open", side_effect=[io.BytesIO(HTML.encode()), OSError("private reparse detail"), io.BytesIO(HTML.encode())]) as opened:
            self.source.result()
            with self.assertRaises(engine.ContractError) as caught: self.source.result()
            self.assertEqual(caught.exception.code, "source")
            self.assertNotIn("private reparse detail", caught.exception.message)
            self.assertEqual(self.source.result()["items"], [{"name": "Alpha"}])
            self.assertEqual(opened.call_count, 3)

    def test_remote_read_policy_and_capture_time_are_never_bypassed(self):
        capture = SimpleNamespace(url="https://example.invalid/catalog", retrieved_at="capture-one", read=mock.Mock(return_value=HTML))
        source = server.Source(copy.deepcopy(RULES), remote=capture)
        with mock.patch.object(server, "extract", wraps=engine.extract) as extract:
            source.result()
            capture.retrieved_at = "capture-two"
            self.assertEqual(source.result()["provenance"]["retrieved_at"], "capture-two")
            self.assertEqual(extract.call_count, 1)
            capture.read.side_effect = remote.RemoteError("robots_denied", "robots.txt does not permit this source.")
            with self.assertRaises(engine.ContractError) as caught: source.result()
            self.assertEqual(caught.exception.code, "robots_denied")
            capture.read.side_effect = None
            self.assertEqual(source.result()["items"], [{"name": "Alpha"}])
            self.assertEqual(extract.call_count, 2)
        self.assertEqual(capture.read.call_count, 4)

    def test_remote_url_and_source_kind_are_part_of_the_result_identity(self):
        html = '<article><a href="details">Details</a></article>'
        rules = {"item_selector": "article", "fields": [{"name": "url", "selector": "a", "attribute": "href", "type": "url"}]}
        self.path.write_text(html)
        source = server.Source(rules, self.path)
        self.assertEqual(source.result()["items"], [{"url": "https://example.invalid/details"}])
        source.remote = SimpleNamespace(url="https://example.invalid/first/page", retrieved_at="one", read=lambda: html)
        self.assertEqual(source.result()["items"], [{"url": "https://example.invalid/first/details"}])
        source.remote.url = "https://example.invalid/second/page"
        result = source.result()
        self.assertEqual(result["items"], [{"url": "https://example.invalid/second/details"}])
        self.assertEqual(result["provenance"]["source_kind"], "https")

    def test_real_remote_ttl_and_robots_failure_still_control_cached_results(self):
        clock = [0]
        allowed = (200, b"User-agent: *\nAllow: /\n", "text/plain")
        denied = (200, b"User-agent: *\nDisallow: /\n", "text/plain")
        page = (200, HTML.encode(), "text/html")
        capture = remote.RemoteSource("https://example.org/catalog", "example.org")
        source = server.Source(copy.deepcopy(RULES), remote=capture)
        with mock.patch.object(remote, "public_addresses", return_value=["8.8.8.8"]), mock.patch.object(remote, "bounded_get", side_effect=[allowed, page, denied, allowed, page]) as fetch, mock.patch.object(remote.time, "sleep"), mock.patch.object(remote.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(server, "extract", wraps=engine.extract) as extract:
            first = source.result()
            clock[0] = 1
            self.assertEqual(source.result()["provenance"]["retrieved_at"], first["provenance"]["retrieved_at"])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(extract.call_count, 1)
            clock[0] = 61
            with self.assertRaises(remote.RemoteError) as caught: source.result()
            self.assertEqual(caught.exception.code, "robots_denied")
            clock[0] = 62
            with self.assertRaises(remote.RemoteError): source.result()
            self.assertEqual(fetch.call_count, 3)
            clock[0] = 122
            self.assertEqual(source.result()["items"], [{"name": "Alpha"}])
            self.assertEqual(fetch.call_count, 5)
            self.assertEqual(extract.call_count, 2)

    def test_concurrent_reads_share_work_without_sharing_mutable_results(self):
        with mock.patch.object(server, "extract", wraps=engine.extract) as extract, ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.source.result(), range(8)))
            self.assertEqual(extract.call_count, 1)
        results[0]["items"][0]["name"] = "changed"
        self.assertTrue(all(result["items"] == [{"name": "Alpha"}] for result in results[1:]))

    def test_cache_retains_no_unselected_html_and_is_bounded(self):
        self.path.write_text(HTML + '<script>UNSELECTED-CANARY-R9</script>')
        self.source.result()
        self.source.result()
        self.assertNotIn("UNSELECTED-CANARY-R9", repr(self.source.__dict__))
        rules = {"item_selector": "article", "fields": [{"name": "field_" + str(i), "selector": ".value"} for i in range(16)]}
        html = ('<article><span class="value">' + "x" * 2000 + '</span></article>') * 95
        self.path.write_text(html)
        source = server.Source(rules, self.path)
        with mock.patch.object(server, "extract", wraps=engine.extract) as extract:
            result = source.result()
            self.assertLessEqual(len(server.encoded_response(result, engine.MAX_OUTPUT)), engine.MAX_OUTPUT)
            self.assertEqual(source.result()["items"], result["items"])
            self.assertEqual(extract.call_count, 1)
            self.path.write_text(html + ('<article><span class="value">' + "x" * 2000 + '</span></article>') * 10)
            with self.assertRaises(engine.ContractError): source.result()
            self.path.write_text(html)
            self.assertEqual(source.result()["items"], result["items"])
            self.assertEqual(extract.call_count, 3)


if __name__ == "__main__":
    unittest.main()
