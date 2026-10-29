import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from site_contract_api.__main__ import main, read_text
from site_contract_api.engine import ContractError


class CLITests(unittest.TestCase):
    def test_demo_preview_runs_without_install_or_network(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["preview"]), 0)
        self.assertEqual(json.loads(output.getvalue())["contract"]["item_count"], 3)

    def test_invalid_remote_configuration_and_ports_fail_cleanly(self):
        for args in (["serve", "--port", "0"], ["serve", "--allow-host", "example.org"],
                     ["serve", "--remote-url", "https://example.org"],
                     ["serve", "--remote-url", "http://example.org", "--allow-host", "example.org"]):
            with self.subTest(args=args[:2]), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(args), 1)
            self.assertNotIn("Traceback", error.getvalue())

    def test_input_file_limits_and_redacted_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            path.write_text("x" * 101)
            with self.assertRaises(ContractError):
                read_text(path, 100)
            with self.assertRaises(ContractError) as caught:
                read_text(path.parent / "absent.txt", 100)
            self.assertNotIn(str(path.parent), caught.exception.message)

    def test_malformed_numeric_and_unicode_rules_fail_without_tracebacks(self):
        fixtures = ['{"item_selector":"article","min_items":' + "1" * 5000 + ',"fields":[{"name":"title","selector":":scope"}]}',
                    '{"item_selector":"article","base_url":"https://example.invalid/\\ud800","fields":[{"name":"title","selector":":scope"}]}']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            for fixture in fixtures:
                path.write_text(fixture, encoding="utf-8")
                with self.subTest(length=len(fixture)), contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(main(["preview", "--rules", str(path)]), 1)
                self.assertEqual(json.loads(error.getvalue())["error"], "rules")
                self.assertNotIn("Traceback", error.getvalue())
                self.assertNotIn(directory, error.getvalue())


if __name__ == "__main__":
    unittest.main()
