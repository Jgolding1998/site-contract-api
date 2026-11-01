"""Rate-policy failures must be stable across stdlib robotparser versions."""
import unittest
from unittest import mock

from site_contract_api import remote


class RobotsPolicyValidation(unittest.TestCase):
    def test_invalid_rates_refuse_before_a_parser_can_ignore_them(self):
        policies = ["Crawl-delay: " + chr(0xb2), "Request-rate: 1/" + chr(0xb2),
                    "Crawl-delay: " + chr(0x0662), "Request-rate: " + chr(0xff12) + "/1",
                    "Crawl-delay: 1.5", "Crawl-delay: -1", "Crawl-delay:",
                    "Request-rate: 0/1", "Request-rate: 1/0", "Request-rate: 1/2/3"]
        for declaration in policies:
            with self.subTest(declaration=declaration):
                source = remote.RemoteSource("https://example.invalid/catalog", "example.invalid")
                robots = ("User-agent: *\n" + declaration + "\n").encode()
                # Some supported stdlib versions ignore invalid numeric syntax.
                # The application's policy must run before either implementation.
                with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), \
                     mock.patch.object(remote, "bounded_get", return_value=(200, robots, "text/plain")) as fetch, \
                     mock.patch.object(remote, "RobotFileParser") as parser, \
                     mock.patch.object(remote.time, "sleep") as sleep:
                    for _ in range(2):
                        with self.assertRaises(remote.RemoteError) as caught:
                            source.read()
                        self.assertEqual(caught.exception.code, "robots_invalid")
                    parser.assert_not_called()
                    fetch.assert_called_once()
                    sleep.assert_not_called()
                self.assertEqual(source.last_error[0], "robots_invalid")

    def test_valid_ascii_rates_comments_and_case_keep_the_requested_delay(self):
        source = remote.RemoteSource("https://example.invalid/catalog", "example.invalid")
        robots = b"User-agent: *\nCrawl-Delay: 2 # seconds\nREQUEST-RATE: 1 / 3\n"
        page = b"<html><body>ordinary fixture</body></html>"
        with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), \
             mock.patch.object(remote, "bounded_get", side_effect=[(200, robots, "text/plain"), (200, page, "text/html")]) as fetch, \
             mock.patch.object(remote.time, "sleep") as sleep:
            self.assertEqual(source.read(), page.decode())
            self.assertEqual(source.read(), page.decode())
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(3)

    def test_a_stdlib_parser_failure_remains_a_cached_robots_failure(self):
        source = remote.RemoteSource("https://example.invalid/catalog", "example.invalid")
        with mock.patch.object(remote, "public_addresses", return_value=["1.1.1.1"]), \
             mock.patch.object(remote, "bounded_get", return_value=(200, b"User-agent: *\nCrawl-delay: 1\n", "text/plain")) as fetch, \
             mock.patch.object(remote, "RobotFileParser") as parser, \
             mock.patch.object(remote.time, "sleep") as sleep:
            parser.return_value.parse.side_effect = ValueError("Untrusted parser detail")
            for _ in range(2):
                with self.assertRaises(remote.RemoteError) as caught:
                    source.read()
                self.assertEqual(caught.exception.code, "robots_invalid")
                self.assertNotIn("Untrusted parser detail", caught.exception.message)
            fetch.assert_called_once()
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
