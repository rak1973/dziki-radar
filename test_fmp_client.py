import contextlib
import http.client
import io
import json
import logging
import os
import ssl
import threading
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import call, patch
from urllib.parse import parse_qs, quote, urlsplit

from fmp_client import (
    FMPClient,
    FMPConfigurationError,
    FMPConnectionError,
    FMPHTTPError,
    FMPRateLimitError,
    FMPRequestBudgetError,
    FMPResponseError,
)


FAKE_KEY = "fixture-fmp-secret+&/%"
STATEMENT = {
    "symbol": "AAPL",
    "date": "2025-09-27",
    "period": "Q3",
    "reportedCurrency": "USD",
    "fiscalYear": "2025",
    "filingDate": "2025-10-31",
    "revenue": 120,
}
EOD = {
    "symbol": "AAPL", "date": "2025-12-31",
    "open": 42, "high": 44, "low": 41, "close": 43, "volume": 1000,
}


class FMPClientTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"FMP_API_KEY": FAKE_KEY}))
        self.connect = self.enterContext(patch("fmp_client.http.client.HTTPSConnection"))
        self.connection = self.connect.return_value
        self.response = self.connection.getresponse.return_value.__enter__.return_value
        self.now = 100
        self.enterContext(patch("fmp_client.time.monotonic", side_effect=lambda: self.now))
        self.sleep = self.enterContext(patch("fmp_client.time.sleep", side_effect=self.advance))
        self.client = FMPClient()

    def advance(self, seconds):
        self.now += seconds

    def respond(self, status=200, *, payload=None, body=None, retry_after=None):
        self.response.status = status
        self.response.getheader.return_value = retry_after
        self.response.read.return_value = body if body is not None else json.dumps(payload).encode()

    def assert_request(self, endpoint, *, symbol="AAPL", timeout=10, **params):
        self.connect.assert_called_with("financialmodelingprep.com", timeout=timeout)
        self.connection.set_debuglevel.assert_called_with(0)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(kwargs, {"headers": {"Accept": "application/json"}})
        url = urlsplit(args[1])
        self.assertEqual(url.path, f"/stable/{endpoint}")
        self.assertEqual(parse_qs(url.query), {
            name: [str(value)]
            for name, value in {"symbol": symbol, "apikey": FAKE_KEY, **params}.items()
        })

    def test_key_required_from_environment(self):
        for value in ("", "  "):
            with patch.dict(os.environ, {"FMP_API_KEY": value}):
                with self.assertRaises(FMPConfigurationError):
                    FMPClient()
                with self.assertRaises(FMPConfigurationError):
                    self.client.quote("AAPL")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(FMPConfigurationError):
                FMPClient()
        self.connect.assert_not_called()

    def test_financial_statements(self):
        for method, endpoint, metrics in (
            (self.client.income_statement, "income-statement", {"revenue": 120}),
            (self.client.balance_sheet_statement, "balance-sheet-statement", {"totalAssets": None}),
            (self.client.cash_flow_statement, "cash-flow-statement", {"netCashProvidedByOperatingActivities": -25}),
        ):
            with self.subTest(endpoint=endpoint):
                rows = [{**STATEMENT, **metrics}]
                self.respond(payload=rows)
                self.assertEqual(method(" aapl ", period="quarter", limit=2), rows)
                self.assert_request(endpoint, period="quarter", limit=2)

    def test_period_names_and_limit_boundaries(self):
        for supplied, canonical in (
            ("annual", "annual"), (" QUARTER ", "quarter"), ("fy", "FY"),
            ("q1", "Q1"), ("Q2", "Q2"), ("q3", "Q3"), ("Q4", "Q4"),
        ):
            with self.subTest(period=supplied):
                self.respond(payload=[])
                self.assertEqual(self.client.income_statement("AAPL", period=supplied, limit=1000), [])
                self.assert_request("income-statement", period=canonical, limit=1000)
        self.client.income_statement("AAPL", limit=1)
        self.assert_request("income-statement", period="annual", limit=1)

    def test_quote(self):
        rows = [{"symbol": "AAPL", "price": 200.25}]
        self.respond(payload=rows)
        self.assertEqual(self.client.quote("aapl"), rows)
        self.assert_request("quote")

    def test_historical_prices_and_empty_results(self):
        self.respond(payload=[EOD])
        self.assertEqual(
            self.client.historical_prices("AAPL", start="2025-01-01", end="2025-12-31"),
            [EOD],
        )
        self.assert_request("historical-price-eod/full", **{"from": "2025-01-01", "to": "2025-12-31"})
        self.respond(payload=[])
        self.assertEqual(self.client.historical_prices("AAPL"), [])
        self.assert_request("historical-price-eod/full")

    def test_invalid_query_arguments(self):
        for action in (
            lambda: self.client.quote(" "),
            lambda: self.client.income_statement("AAPL", period="monthly"),
            lambda: self.client.income_statement("AAPL", period=None),
            lambda: self.client.cash_flow_statement("AAPL", limit=0),
            lambda: self.client.cash_flow_statement("AAPL", limit=1001),
            lambda: self.client.cash_flow_statement("AAPL", limit=True),
            lambda: self.client.historical_prices("AAPL", start="2025-02-30"),
            lambda: self.client.historical_prices("AAPL", start="2025-02-01", end="2025-01-01"),
            lambda: FMPClient(max_requests=0),
            lambda: FMPClient(max_requests=True),
        ):
            with self.assertRaises(ValueError):
                action()
        self.connect.assert_not_called()

    def test_schema_rejects_incorrect_financial_records(self):
        self.respond(payload=[{**STATEMENT, "period": "FY"}])
        with self.assertRaises(FMPResponseError):
            self.client.income_statement("AAPL", period="quarter")
        for row in (
            {},
            {**STATEMENT, "symbol": "MSFT"},
            {**STATEMENT, "date": "2025-02-29"},
            {**STATEMENT, "reportedCurrency": "usd"},
            {**STATEMENT, "period": "Q2"},
            {**STATEMENT, "revenue": True},
            {**STATEMENT, "revenue": "120"},
            {**STATEMENT, "revenue": float("inf")},
            {key: value for key, value in STATEMENT.items() if key != "revenue"},
        ):
            with self.subTest(row=row):
                self.respond(payload=[STATEMENT, row])
                with self.assertRaises(FMPResponseError):
                    self.client.income_statement("AAPL", period="Q3")
        self.sleep.assert_not_called()

    def test_schema_rejects_incorrect_market_records(self):
        self.respond(payload=[{**EOD, "volume": None}])
        with self.assertRaises(FMPResponseError):
            self.client.historical_prices("AAPL")
        self.respond(payload=[{"symbol": "AAPL", "price": False}])
        with self.assertRaises(FMPResponseError):
            self.client.quote("AAPL")

    def test_rate_limit_honors_retry_after_seconds(self):
        self.respond(429, retry_after="3")

        def recover(seconds):
            self.advance(seconds)
            self.respond(payload=[])

        self.sleep.side_effect = recover
        self.assertEqual(self.client.quote("AAPL"), [])
        self.sleep.assert_called_once_with(3)
        self.assertEqual(self.connect.call_count, 2)

    def test_rate_limit_honors_retry_after_http_date(self):
        self.respond(429, retry_after="Wed, 01 Jan 2025 00:00:04 GMT")
        with patch("fmp_client.datetime") as clock:
            clock.now.return_value = datetime(2025, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(FMPRateLimitError) as caught:
                FMPClient(max_retries=0).quote("AAPL")
        self.assertEqual(caught.exception.retry_after, 4)

    def test_rate_limit_stops_after_retry_budget(self):
        self.respond(429, retry_after="invalid")
        with self.assertRaises(FMPRateLimitError) as caught:
            self.client.quote("AAPL")
        self.assertEqual(caught.exception.retry_after, 4)
        self.assertEqual(self.connect.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.respond(payload=[])
        self.client.quote("MSFT")
        self.sleep.assert_called_with(4)

    def test_long_cooldown_is_shared_between_tickers(self):
        self.respond(429, retry_after="60")
        client = FMPClient(max_requests=2)
        with self.assertRaises(FMPRateLimitError) as caught:
            client.quote("AAPL")
        self.assertEqual(caught.exception.retry_after, 60)
        self.advance(10)
        with self.assertRaises(FMPRateLimitError) as caught:
            client.historical_prices("MSFT")
        self.assertEqual(caught.exception.retry_after, 50)
        self.connect.assert_called_once()
        self.sleep.assert_not_called()
        self.advance(50)
        self.respond(payload=[])
        client.historical_prices("MSFT")
        self.assert_request("historical-price-eod/full", symbol="MSFT")
        with self.assertRaises(FMPRequestBudgetError):
            client.quote("NVDA")

    def test_concurrent_tickers_share_cooldown(self):
        self.respond(429, retry_after="60")
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(self.client.quote, symbol) for symbol in ("AAPL", "MSFT")]
            for job in jobs:
                with self.assertRaises(FMPRateLimitError):
                    job.result()
        self.connect.assert_called_once()

    def test_request_budget_counts_retries_and_tickers(self):
        self.respond(payload=[])
        self.connection.request.side_effect = [TimeoutError(FAKE_KEY), None, None]
        client = FMPClient(max_requests=3)
        with self.assertRaises(ValueError):
            client.quote("")
        client.quote("AAPL")
        client.quote("MSFT")
        with self.assertRaises(FMPRequestBudgetError):
            client.quote("NVDA")
        self.assertEqual(self.connect.call_count, 3)
        self.sleep.assert_called_once_with(1)

    def test_exhausted_budget_prevents_a_retry_and_wait(self):
        self.respond(503)
        with self.assertRaises(FMPRequestBudgetError):
            FMPClient(max_requests=1).quote("AAPL")
        self.connect.assert_called_once()
        self.sleep.assert_not_called()

    def test_temporary_http_error_reports_long_retry_after(self):
        self.respond(503, retry_after="60")
        with self.assertRaises(FMPHTTPError) as caught:
            self.client.quote("AAPL")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.retry_after, 60)
        self.connect.assert_called_once()
        self.sleep.assert_not_called()

    def test_temporary_http_error_is_retried(self):
        self.respond(503, retry_after="2")

        def recover(seconds):
            self.advance(seconds)
            self.respond(payload=[])

        self.sleep.side_effect = recover
        self.assertEqual(self.client.quote("AAPL"), [])
        self.sleep.assert_called_once_with(2)
        self.assertEqual(self.connect.call_count, 2)

    def test_transport_error_has_no_secret_in_diagnostics(self):
        self.connection.request.side_effect = TimeoutError(f"URL?apikey={FAKE_KEY}")
        with self.assertRaises(FMPConnectionError) as caught:
            FMPClient(timeout=3, max_retries=0).quote("AAPL")
        error = caught.exception
        self.assertNotIn(FAKE_KEY, str(error))
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assert_request("quote", timeout=3)
        self.sleep.assert_not_called()

    def test_tls_error_is_not_retried(self):
        self.connection.request.side_effect = ssl.SSLCertVerificationError(FAKE_KEY)
        with self.assertRaises(FMPConnectionError) as caught:
            self.client.quote("AAPL")
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.connect.assert_called_once()

    def test_permanent_http_errors_and_redirects_are_not_retried(self):
        for status in (302, 400, 401, 402, 403, 404):
            with self.subTest(status=status):
                self.connect.reset_mock()
                self.respond(status, payload={"Error Message": FAKE_KEY})
                with self.assertRaises(FMPHTTPError) as caught:
                    self.client.quote("AAPL")
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn(FAKE_KEY, str(caught.exception))
                self.connect.assert_called_once()
        self.response.read.assert_not_called()
        self.sleep.assert_not_called()

    def test_invalid_json_unexpected_payload_and_secret_echo(self):
        for body in (
            f"invalid JSON {FAKE_KEY}".encode(),
            b'[42]',
            json.dumps([{"symbol": "AAPL", "price": 3, "url": FAKE_KEY}]).encode(),
            json.dumps([{"symbol": "AAPL", "price": 3, "url": quote(FAKE_KEY, safe="")}]).encode(),
        ):
            self.respond(body=body)
            with self.assertRaises(FMPResponseError) as caught:
                self.client.quote("AAPL")
            self.assertNotIn(FAKE_KEY, str(caught.exception))
            self.assertIsNone(caught.exception.__context__)
        self.assertEqual(self.connect.call_count, 4)
        self.sleep.assert_not_called()


class FMPTransportSecurityTests(unittest.TestCase):
    def test_network_logs_and_traceback_with_locals_hide_secret(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(parse_qs(urlsplit(self.path).query))
                self.send_response(403)
                self.end_headers()

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 3
            worker = threading.Thread(target=server.handle_request, daemon=True)
            worker.start()
            debug = io.StringIO()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.set_debuglevel(1)
            with (
                patch.dict(os.environ, {"FMP_API_KEY": FAKE_KEY}),
                patch("fmp_client.http.client.HTTPSConnection", return_value=connection),
                contextlib.redirect_stdout(debug),
                contextlib.redirect_stderr(debug),
                self.assertLogs(level=logging.DEBUG) as logs,
            ):
                logging.getLogger("fmp-test").debug("Próba logowania transportu")
                try:
                    FMPClient().quote("AAPL")
                except FMPHTTPError as exc:
                    # Odczytaj tylko ramki klienta, pomijając celowo bogatą w
                    # dane ramkę testu i obiekty instrumentacji.
                    stack = traceback.StackSummary.extract(
                        ((frame, line) for frame, line in traceback.walk_tb(exc.__traceback__)
                         if frame.f_globals["__name__"] == "fmp_client"),
                        capture_locals=True,
                    )
                    diagnostics = "".join(stack.format())
                    message = str(exc)
                else:
                    self.fail("Oczekiwano HTTP 403")
            worker.join(4)
        self.assertEqual(received, [{"symbol": ["AAPL"], "apikey": [FAKE_KEY]}])
        for channel in (debug.getvalue(), "".join(logs.output), diagnostics, message):
            self.assertNotIn("fixture-fmp-secret", channel)


if __name__ == "__main__":
    unittest.main()
