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


class EngineTests(unittest.TestCase):
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
