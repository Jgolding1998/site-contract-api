import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from site_contract_api import engine

DATA = Path(__file__).resolve().parent.parent / "site_contract_api" / "data"
HTML = (DATA / "catalog.html").read_text()
RULES = json.loads((DATA / "rules.json").read_text())


def supports_scripting_parser():
    try:
        engine.HTMLParser(convert_charrefs=True, scripting=True)
    except TypeError:
        return False
    return True


class EngineTests(unittest.TestCase):
    def test_noscript_fallback_is_not_a_field_or_visible_text(self):
        rules = {"item_selector": "article", "fields": [{"name": "price", "selector": ".price", "type": "number"}]}
        with self.assertRaises(engine.ContractError) as caught:
            engine.extract('<article><noscript><span class="price">0</span></noscript></article>', rules)
        if supports_scripting_parser():
            self.assertEqual(caught.exception.code, "schema_drift")
            self.assertEqual(caught.exception.issues[0]["code"], "missing_required")
        else:
            self.assertEqual(caught.exception.code, "html")
            self.assertIn("text-only", caught.exception.message)
        self.assertNotIn("items", caught.exception.as_dict())
        rules["fields"] = [{"name": "title", "selector": ":scope"}]
        self.assertEqual(engine.extract('<article>Ready<noscript>Enable scripts</noscript></article>', rules)["items"], [{"title": "Ready"}])

    def test_noscript_lazy_image_does_not_duplicate_real_image(self):
        html = '<article><img class="photo" data-src="/actual.png"><noscript><img class="photo" src="/fallback.png"></noscript></article>'
        rules = {"item_selector": "article", "fields": [{"name": "photo", "selector": ".photo", "attribute": "data-src", "type": "url"}]}
        expected = [{"photo": "https://example.invalid/actual.png"}]
        # The real image must work even when nested fallback markup is refused.
        real_only = '<article><img class="photo" data-src="/actual.png"></article>'
        self.assertEqual(engine.extract(real_only, rules)["items"], expected)
        if supports_scripting_parser():
            self.assertEqual(engine.extract(html, rules)["items"], expected)
        else:
            with self.assertRaises(engine.ContractError) as caught:
                engine.extract(html, rules)
            self.assertEqual(caught.exception.code, "html")
            self.assertIn("text-only", caught.exception.message)
            self.assertNotIn("items", caught.exception.as_dict())

    def test_legacy_raw_text_callbacks_fail_closed(self):
        # Simulate callback sequences on older supported HTMLParser patch levels.
        # Current parsers return these contents as data instead of child tags.
        for tag in ("textarea", "title", "noscript", "xmp", "iframe", "noembed", "noframes"):
            for callback in (lambda p: p.handle_starttag("span", [("class", "price")]),
                             lambda p: p.handle_startendtag("img", []),
                             lambda p: p.handle_endtag("article"),
                             lambda p: p.handle_comment("literal comment"),
                             lambda p: p.handle_decl("doctype html"),
                             lambda p: p.handle_pi("literal"),
                             lambda p: p.unknown_decl("CDATA[literal")):
                with self.subTest(tag=tag, callback=callback):
                    parser = engine.Document()
                    parser.handle_starttag(tag, [])
                    with self.assertRaises(engine.ContractError) as caught:
                        callback(parser)
                    self.assertEqual(caught.exception.code, "html")
        parser = engine.Document()
        parser.handle_starttag("textarea", [])
        parser.handle_data("plain text")
        parser.handle_endtag("textarea")
        self.assertEqual(engine.node_text(parser.root), "plain text")

    def test_modern_rcdata_keeps_literal_markup(self):
        if not getattr(engine.HTMLParser, "RCDATA_CONTENT_ELEMENTS", None):
            self.skipTest("This Python parser predates RCDATA handling; markup is rejected instead")
        rules = {"item_selector": "article", "fields": [{"name": "value", "selector": "textarea"}]}
        html = '<article><textarea><span class="price">0</span><!-- literal --> &amp;</textarea></article>'
        self.assertEqual(engine.extract(html, rules)["items"], [{"value": '<span class="price">0</span><!-- literal --> &'}])

    def test_attribute_references_never_silently_change_query_parameters(self):
        rules = {"item_selector": "article", "fields": [{"name": "url", "selector": "a", "attribute": "href", "type": "url"}]}
        for value in ("/p?id=1&region=us", "/p?id=1&currency=EUR", "/p?a=1&not=x"):
            with self.subTest(value=value):
                try:
                    result = engine.extract(f'<article><a href="{value}">x</a></article>', rules)
                except engine.ContractError as error:
                    self.assertEqual(error.code, "html")
                else:
                    self.assertEqual(result["items"], [{"url": "https://example.invalid" + value}])
        for value, expected in (("/p?a=1&page=2", "/p?a=1&page=2"), ("/p?a=1&amp;b=2", "/p?a=1&b=2"), ("/p?x=&copy;=2", "/p?x=\u00a9=2")):
            self.assertEqual(engine.extract(f'<article><a href="{value}">x</a></article>', rules)["items"], [{"url": "https://example.invalid" + expected}])

    def test_legacy_attribute_guard_is_narrow(self):
        rules = {"item_selector": "article", "fields": [{"name": "value", "selector": "span", "attribute": "data-value"}]}
        with mock.patch.object(engine, "_ATTRIBUTE_GUARD", True):
            for value in ("A&para1", "&ampx", "x&reg=us"):
                with self.subTest(value=value), self.assertRaises(engine.ContractError) as caught:
                    engine.extract(f'<article><span data-value="{value}"></span></article>', rules)
                self.assertEqual(caught.exception.code, "html")
            for value, expected in (("R&D", "R&D"), ("&page=2", "&page=2"), ("&amp;x", "&x"), ("&copy;=2", "\u00a9=2"), ("&notin;", "\u2209"), ("&parallel;", "\u2225")):
                self.assertEqual(engine.extract(f'<article><span data-value="{value}"></span></article>', rules)["items"], [{"value": expected}])

    def test_legacy_attribute_taint_does_not_reject_unread_attributes(self):
        html = '<link href="/a.css?v=1&timestamp=9"><img src="/pixel?id=1&param=x"><article class="product" data-note="&region=us"><span class="title">Ready</span></article>'
        rules = {"item_selector": "article.product", "fields": [{"name": "title", "selector": ".title"}]}
        with mock.patch.object(engine, "_ATTRIBUTE_GUARD", True):
            self.assertEqual(engine.extract(html, rules)["items"], [{"title": "Ready"}])
            # Existence selectors do not depend on the decoded value.
            rules["item_selector"] = 'article[data-note]'
            self.assertEqual(engine.extract(html, rules)["items"], [{"title": "Ready"}])
            rules["item_selector"] = 'article[data-note="&region=us"]'
            with self.assertRaises(engine.ContractError) as caught:
                engine.extract(html, rules)
            self.assertEqual(caught.exception.code, "html")
            for attribute, selector in (("class", ".product"), ("id", "#product")):
                rules["item_selector"] = selector
                with self.assertRaises(engine.ContractError) as caught:
                    engine.extract(f'<article {attribute}="product &region=us"><span class="title">Ready</span></article>', rules)
                self.assertEqual(caught.exception.code, "html")

    def test_self_closing_text_only_elements_are_rejected(self):
        rules = {"item_selector": "article", "fields": [{"name": "price", "selector": ".price", "type": "number"}]}
        for tag in engine.TEXT_ONLY:
            with self.subTest(tag=tag), self.assertRaises(engine.ContractError) as caught:
                engine.extract(f'<article><{tag}/><span class="price">5</span></{tag}></article>', rules)
            self.assertEqual(caught.exception.code, "html")

    def test_selector_combinators_use_css_whitespace_only(self):
        html = '<article><div class="wrapper"><span class="price">24.50</span></div></article>'
        rules = {"item_selector": "article", "fields": [{"name": "price", "selector": ".wrapper .price", "type": "number"}]}
        for separator in ("\t", "\n", "\r", "\f", " "):
            rules["fields"][0]["selector"] = ".wrapper" + separator + ".price"
            self.assertEqual(engine.extract(html, rules)["items"], [{"price": 24.5}])
        for separator in ("\u00a0", "\u0085", "\u2003", "\v"):
            for selector in (".wrapper" + separator + ".price", separator + ".price", ".price" + separator):
                with self.subTest(codepoint=ord(separator), position=selector.index(separator)):
                    rules["fields"][0]["selector"] = selector
                    with self.assertRaises(engine.ContractError) as caught:
                        engine.extract(html, rules)
                    self.assertEqual(caught.exception.code, "selector")
        rules["fields"][0] = {"name": "price", "selector": '[data-note="a\u00a0b"]', "type": "number"}
        self.assertEqual(engine.extract('<article><span data-note="a\u00a0b">24.50</span></article>', rules)["items"], [{"price": 24.5}])

    def test_fixture_produces_typed_data_and_provenance(self):
        result = engine.extract(HTML, RULES, retrieved_at="fixture-time")
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["items"][0]["price"], 24.5)
        self.assertEqual(result["items"][0]["stock"], 18)
        self.assertEqual(result["items"][0]["details"], "https://example.invalid/tools/driver-kit")
        self.assertEqual(result["provenance"]["source_hash"], hashlib.sha256(HTML.encode()).hexdigest())
        self.assertEqual(result, engine.extract(HTML, RULES, retrieved_at="fixture-time"))
        self.assertNotIn(HTML, json.dumps(result))

    def test_changed_required_field_fails_without_partial_items(self):
        with self.assertRaises(engine.ContractError) as caught:
            engine.extract(HTML.replace('data-price="24.50"', 'data-cost="24.50"'), RULES)
        self.assertEqual(caught.exception.code, "schema_drift")
        self.assertEqual(caught.exception.issues[0]["code"], "missing_required")
        self.assertNotIn("items", caught.exception.as_dict())

    def test_wrong_type_and_multiple_matches_fail(self):
        for changed, expected in [(HTML.replace('data-price="24.50"', 'data-price="free"'), "type_changed"),
                                  (HTML.replace('class="stock">18', 'class="stock">18</span><span class="stock">18'), "ambiguous_field")]:
            with self.subTest(expected=expected), self.assertRaises(engine.ContractError) as caught:
                engine.extract(changed, RULES)
            self.assertEqual(caught.exception.issues[0]["code"], expected)

    def test_exact_count_and_range(self):
        rules = copy.deepcopy(RULES)
        rules["expected_items"] = 4
        with self.assertRaises(engine.ContractError) as caught:
            engine.extract(HTML, rules)
        self.assertEqual(caught.exception.issues, [{"code": "item_count", "actual": 3}])
        del rules["expected_items"]
        rules["max_items"] = 2
        with self.assertRaises(engine.ContractError):
            engine.extract(HTML, rules)

    def test_optional_missing_value_is_explicit_null(self):
        rules = copy.deepcopy(RULES)
        rules["fields"].append({"name": "optional", "selector": ".absent", "required": False})
        self.assertIsNone(engine.extract(HTML, rules)["items"][0]["optional"])

    def test_resolution_base_does_not_fetch_a_url(self):
        rules = {**RULES, "base_url": "https://example.org/catalog/"}
        result = engine.extract(HTML, rules)
        self.assertEqual(result["items"][0]["details"], "https://example.org/tools/driver-kit")
        self.assertEqual(result["provenance"]["resolution_base"], rules["base_url"])
        for base in ("file:///private", "https://example.org/?x=1", None, 7, "https://example.org:bad"):
            with self.assertRaises(engine.ContractError):
                engine.load_rules({**RULES, "base_url": base})

    def test_selector_subset_and_ancestor_backtracking(self):
        doc = engine.Document()
        doc.feed('<main class="outer"><div class="middle"><div class="middle"><span class="inner" data-label="test value">match</span></div></div></main>')
        for selector in ('.outer > .middle .inner', 'main span[data-label="test value"]', '.inner[data-label]'):
            self.assertEqual(len(engine.query(doc.root, selector)), 1)
        self.assertEqual(engine.node_text(engine.query(doc.root, "span")[0]), "match")

    def test_field_queries_cannot_escape_item_scope(self):
        doc = engine.Document()
        doc.feed('<main class="outside"><article><span>x</span></article></main>')
        row = engine.query(doc.root, "article")[0]
        self.assertFalse(engine.query(row, ".outside span"))

    def test_unsupported_selectors_are_rejected(self):
        for selector in ("", "a,b", "a + b", "li:nth-child(1)", "a >", "> a", "a >> b", "[", "[x~=y]", "x" * 257):
            with self.subTest(selector=selector), self.assertRaises(engine.ContractError):
                engine.compile_selector(selector)

    def test_rules_validation(self):
        changes = [{"fields": []}, {"fields": [{}] * 17}, {"max_items": 201}, {"min_items": True},
                   {"min_items": 5, "max_items": 2}, {"expected_items": -1}, {"unknown": True}]
        for patch in changes:
            with self.subTest(patch=list(patch)), self.assertRaises(engine.ContractError):
                engine.load_rules({**RULES, **patch})
        rules = copy.deepcopy(RULES)
        rules["fields"][1]["name"] = rules["fields"][0]["name"]
        with self.assertRaises(engine.ContractError):
            engine.load_rules(rules)
        for attribute in ("onclick", "style", "srcdoc"):
            rules = copy.deepcopy(RULES)
            rules["fields"][0]["attribute"] = attribute
            with self.assertRaises(engine.ContractError):
                engine.load_rules(rules)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(engine.ContractError):
            engine.load_rules('{"item_selector":"a","item_selector":"b","fields":[]}')

    def test_large_json_integer_has_a_controlled_rules_error(self):
        text = '{"item_selector":"article","min_items":' + "1" * 5000 + ',"fields":[{"name":"title","selector":":scope"}]}'
        self.assertLess(len(text.encode()), engine.MAX_RULES)
        with self.assertRaises(engine.ContractError) as caught:
            engine.load_rules(text)
        self.assertEqual(caught.exception.code, "rules")
        self.assertNotIn("5000", caught.exception.message)

    def test_surrogate_rules_are_rejected_before_extraction(self):
        for patch in ({"base_url": "https://example.invalid/\ud800"}, {"fields": [{"name": "title", "selector": '[data-label="\ud800"]', "required": False}]}):
            rules = {**RULES, **patch}
            for value in (rules, json.dumps(rules)):
                with self.subTest(patch=list(patch), encoded=isinstance(value, str)), self.assertRaises(engine.ContractError) as caught:
                    engine.load_rules(value)
                self.assertEqual(caught.exception.code, "rules")

    def test_input_and_structure_limits(self):
        for html in ("x" * (engine.MAX_HTML + 1), "<div>" * 66, "<br>" * (engine.MAX_NODES + 1),
                     '<p ' + " ".join(f'data-{i}="x"' for i in range(33)) + ">", '<p id="a" id="b">'):
            with self.subTest(length=len(html)), self.assertRaises(engine.ContractError):
                engine.extract(html, RULES)
        with self.assertRaises(engine.ContractError):
            engine.load_rules(" " * (engine.MAX_RULES + 1))
        with mock.patch.object(engine, "MAX_WORK", 1), self.assertRaises(engine.ContractError) as caught:
            engine.extract(HTML, RULES)
        self.assertEqual(caught.exception.code, "work_limit")

    def test_hidden_script_content_is_not_extracted(self):
        doc = engine.Document()
        doc.feed('<p>visible<script>private-placeholder</script><style>hidden</style></p>')
        self.assertEqual(engine.node_text(engine.query(doc.root, "p")[0]), "visible")
        self.assertEqual(engine.query(doc.root, "script"), [])
        doc = engine.Document()
        doc.feed('<template><article class="product" data-code="hidden"></article></template>')
        self.assertEqual(engine.query(doc.root, "article.product"), [])

    def test_inline_markup_and_comments_do_not_split_text(self):
        rules = lambda kind: {"item_selector": "article", "fields": [{"name": "v", "selector": ".v", "type": kind}]}
        for html, kind, expected in (('<span class="v">$<!-- -->24.50</span>', "text", "$24.50"),
                                     ('<span class="v">24<!-- -->.50</span>', "number", 24.5),
                                     ('<span class="v">24<small>.50</small></span>', "number", 24.5),
                                     ('<h2 class="v">Super<em>Drive</em> 3000</h2>', "text", "SuperDrive 3000"),
                                     ('<span class="v">1<2</span>', "text", "1<2"),
                                     ('<p class="v">One<br>Two</p>', "text", "One Two"),
                                     ('<div class="v"><div>A</div><div>B</div></div>', "text", "A B")):
            with self.subTest(html=html):
                self.assertEqual(engine.extract(f"<article>{html}</article>", rules(kind))["items"], [{"v": expected}])

    def test_class_tokens_split_on_ascii_whitespace_only(self):
        rules = {"item_selector": "article", "fields": [{"name": "v", "selector": ".price", "required": False}]}
        for separator in (" ", "\x1f", " "):
            with self.subTest(separator=ascii(separator)):
                self.assertEqual(engine.extract(f'<article><span class="price{separator}old">9</span></article>', rules)["items"], [{"v": None}])
        self.assertEqual(engine.extract('<article><span class="old\t\nprice">9</span></article>', rules)["items"], [{"v": "9"}])

    def test_expected_items_must_fit_the_item_range(self):
        for patch in ({"expected_items": 5, "max_items": 2}, {"expected_items": 0}, {"expected_items": 1, "min_items": 2}):
            with self.subTest(patch=patch), self.assertRaises(engine.ContractError) as caught:
                engine.load_rules({**RULES, **patch})
            self.assertEqual(caught.exception.code, "rules")
        engine.load_rules({**RULES, "expected_items": 0, "min_items": 0})

    def test_value_limits_and_unsafe_urls(self):
        for value, kind in (("1e999", "number"), ("1.2", "integer"), ("1000000000001", "number"),
                            ("javascript:alert(1)", "url"), ("x" * 2049, "text")):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                engine.convert(value, kind, None)


if __name__ == "__main__":
    unittest.main()
