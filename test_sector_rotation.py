import json
import unittest
from datetime import date, timedelta

from sector_rotation import BENCHMARK, SECTORS, calculate_rotation


def weekdays(start, count):
    days = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


class SectorRotationTests(unittest.TestCase):
    def setUp(self):
        self.sessions = weekdays(date(2025, 1, 6), 40)
        self.series = {ticker: self.rows() for _, ticker, _ in SECTORS}
        self.series[BENCHMARK] = self.rows()

    def rows(self, close=100, volume=100):
        return [{"date": day, "close": close, "volume": volume} for day in self.sessions]

    def snapshot(self, **options):
        return calculate_rotation(self.series, self.sessions, source="synthetic", **options)

    def set_close(self, ticker, ago, close):
        self.series[ticker][-ago - 1]["close"] = close

    def test_contract_contains_all_sectors_and_is_strict_json(self):
        result = self.snapshot()
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["methodology_version"], 2)
        self.assertEqual(result["as_of_session"], self.sessions[-1])
        self.assertEqual(result["benchmark"]["ticker"], "SPY")
        self.assertEqual(result["price_basis"], "split_adjusted_close")
        self.assertEqual(result["source"], "synthetic")
        self.assertEqual(result["volume_measure"], "traded_shares")
        self.assertEqual(result["windows_sessions"], [5, 10, 20])
        self.assertEqual({row["ticker"]: row["sector_id"] for row in result["sectors"]}, {
            "XLE": "10", "XLB": "15", "XLI": "20", "XLY": "25", "XLP": "30",
            "XLV": "35", "XLF": "40", "XLK": "45", "XLC": "50", "XLU": "55", "XLRE": "60",
        })
        self.assertTrue(result["coverage"]["ranking_ready"])
        self.assertEqual(result["dominant_sector_ids"], [])
        self.assertEqual(result["emerging_sector_ids"], [])
        self.assertTrue(all(row["classification"]["status"] == "unranked" for row in result["sectors"]))

    def test_returns_relative_strength_volume_and_acceleration(self):
        # Zmiana ETF: 20% / 32% / 65%; SPY: 10% / ok. 15,24% / 21%.
        for ago, sector_price, spy_price in ((20, 80, 100), (10, 100, 105), (5, 110, 110), (0, 132, 121)):
            self.set_close("XLE", ago, sector_price)
            self.set_close("SPY", ago, spy_price)
        for row, volume in zip(self.series["XLE"], [100] * 20 + [200] * 10 + [300] * 5 + [600] * 5):
            row["volume"] = volume

        result = self.snapshot()
        sector = result["sectors"][0]
        for window, returns, relative, volume in (
            ("5", 20, 10, 100),
            ("10", 32, 16.76190476190476, 125),
            ("20", 65, 44, 225),
        ):
            with self.subTest(window=window):
                values = sector["windows"][window]
                self.assertAlmostEqual(values["return_pct"], returns)
                self.assertAlmostEqual(values["relative_strength_pp"], relative)
                self.assertAlmostEqual(values["volume_change_pct"], volume)
                self.assertEqual(values["price_direction"], "up")
                self.assertEqual(values["volume_direction"], "up")
        self.assertAlmostEqual(sector["acceleration_a5_pp"], 100 * (110 / 105 - 1))
        self.assertAlmostEqual(result["benchmark"]["windows"]["5"]["return_pct"], 10)

    def test_positive_relative_strength_can_accompany_falling_price_and_rising_volume(self):
        self.set_close("XLE", 0, 95)
        self.set_close("SPY", 0, 90)
        for row in self.series["XLE"][-5:]:
            row["volume"] = 200
        values = self.snapshot()["sectors"][0]["windows"]["5"]
        self.assertAlmostEqual(values["return_pct"], -5)
        self.assertAlmostEqual(values["relative_strength_pp"], 5)
        self.assertEqual((values["price_direction"], values["volume_direction"]), ("down", "up"))

    def test_ranking_limits_ties_and_excludes_dominant_from_emerging(self):
        for ticker in ("XLE", "XLB", "XLI", "XLY"):
            self.set_close(ticker, 0, 120)
        for ticker in ("XLF", "XLC", "XLV"):
            self.set_close(ticker, 10, 110)
            self.set_close(ticker, 20, 120)
            self.set_close(ticker, 5, 100)
            self.set_close(ticker, 0, 105)
        result = self.snapshot()
        # XLB, XLE, XLI remisują; czwarty XLY może konkurować o wschodzące.
        self.assertEqual(result["dominant_sector_ids"], ["15", "10", "20"])
        self.assertEqual(result["emerging_sector_ids"], ["25", "50"])
        by_ticker = {row["ticker"]: row for row in result["sectors"]}
        self.assertEqual(by_ticker["XLB"]["classification"]["rank"], 1)
        self.assertAlmostEqual(by_ticker["XLB"]["classification"]["score"], 20)
        self.assertIn("positive_acceleration_a5", by_ticker["XLC"]["classification"]["reasons"])

    def test_equal_returns_at_different_prices_do_not_qualify(self):
        self.series["SPY"] = self.rows(close=50.10)
        self.set_close("SPY", 0, 55.11)
        self.series["XLE"] = self.rows(close=50)
        self.set_close("XLE", 0, 55)
        result = self.snapshot()
        for window in ("5", "10", "20"):
            with self.subTest(window=window):
                sector = result["sectors"][0]["windows"][window]
                self.assertAlmostEqual(sector["return_pct"], 10)
                self.assertAlmostEqual(result["benchmark"]["windows"][window]["return_pct"], 10)
                self.assertEqual(sector["relative_strength_pp"], 0)
        self.assertEqual(result["sectors"][0]["acceleration_a5_pp"], 0)
        self.assertEqual(result["dominant_sector_ids"], [])
        self.assertEqual(result["emerging_sector_ids"], [])

    def test_equal_dominant_scores_at_different_prices_break_tie_by_ticker(self):
        self.series["XLB"] = self.rows(close=50.10)
        self.set_close("XLB", 0, 55.11)
        self.series["XLE"] = self.rows(close=50)
        self.set_close("XLE", 0, 55)
        self.assertEqual(self.snapshot()["dominant_sector_ids"], ["15", "10"])

    def test_equal_emerging_scores_at_different_prices_break_tie_by_ticker(self):
        for ticker, base, previous, recent in (
            ("XLB", 50.10, 60.12, 55.11), ("XLE", 50, 60, 55),
        ):
            self.series[ticker] = self.rows(close=base)
            self.set_close(ticker, 20, previous)
            self.set_close(ticker, 10, previous)
            self.set_close(ticker, 0, recent)
        self.assertEqual(self.snapshot()["emerging_sector_ids"], ["15", "10"])

    def test_equal_consecutive_relative_returns_have_zero_acceleration(self):
        self.series["XLE"] = self.rows(close=50.10)
        self.set_close("XLE", 5, 55.11)
        self.set_close("XLE", 0, 60.621)
        self.set_close("XLE", 20, 70)
        result = self.snapshot()
        self.assertAlmostEqual(result["sectors"][0]["windows"]["5"]["relative_strength_pp"], 10)
        self.assertEqual(result["sectors"][0]["acceleration_a5_pp"], 0)
        self.assertEqual(result["emerging_sector_ids"], [])

    def test_dominant_score_prefers_stronger_twenty_session_return(self):
        self.set_close("XLE", 0, 120)
        self.set_close("XLB", 0, 105)
        self.set_close("XLB", 10, 94)
        self.set_close("XLB", 20, 78)
        self.assertEqual(self.snapshot()["dominant_sector_ids"], ["15", "10"])

    def test_missing_sector_blocks_rank_but_preserves_other_sectors(self):
        del self.series["XLB"]
        self.set_close("XLE", 0, 110)
        result = self.snapshot()
        self.assertFalse(result["coverage"]["ranking_ready"])
        self.assertEqual(result["coverage"]["comparable_sector_count"], 10)
        self.assertEqual(result["coverage"]["unavailable_sector_ids"], ["15"])
        self.assertEqual(result["sectors"][0]["classification"]["status"], "unclassified")
        self.assertAlmostEqual(result["sectors"][0]["windows"]["5"]["relative_strength_pp"], 10)

    def test_missing_benchmark_preserves_actual_returns_and_volume(self):
        del self.series[BENCHMARK]
        self.set_close("XLE", 0, 110)
        result = self.snapshot()
        self.assertFalse(result["coverage"]["benchmark_ready"])
        self.assertFalse(result["coverage"]["ranking_ready"])
        self.assertEqual(result["emerging_sector_ids"], [])
        sector = result["sectors"][0]
        self.assertAlmostEqual(sector["windows"]["5"]["return_pct"], 10)
        self.assertIsNone(sector["windows"]["5"]["relative_strength_pp"])
        self.assertIsNone(sector["acceleration_a5_pp"])

    def test_incomplete_latest_session_does_not_roll_back_target(self):
        self.series[BENCHMARK][-1]["is_complete"] = False
        result = self.snapshot()
        self.assertEqual(result["as_of_session"], self.sessions[-1])
        self.assertFalse(result["coverage"]["benchmark_ready"])
        self.assertEqual(result["benchmark"]["missing_price_sessions"], [self.sessions[-1]])
        self.assertEqual(result["benchmark"]["missing_volume_sessions"], [self.sessions[-1]])
        self.assertEqual(result["dominant_sector_ids"], [])

    def test_holes_and_duplicates_invalidate_only_affected_windows(self):
        missing = self.series["XLE"].pop(-9)["date"]
        duplicate = self.series["XLE"][0]["date"]
        self.series["XLE"].append(self.series["XLE"][0].copy())
        self.series["XLE"].reverse()
        sector = self.snapshot()["sectors"][0]
        self.assertEqual(sector["missing_price_sessions"], [duplicate, missing])
        self.assertEqual(sector["windows"]["5"]["return_pct"], 0)
        self.assertIsNone(sector["windows"]["10"]["return_pct"])
        self.assertIsNone(sector["windows"]["20"]["return_pct"])
        self.assertIsNone(sector["acceleration_a5_pp"])

    def test_missing_and_zero_volume_are_independent_of_price_coverage(self):
        for row in self.series["XLE"]:
            row["volume"] = 0
        self.series["XLB"][-3].pop("volume")
        result = self.snapshot()
        self.assertTrue(result["coverage"]["ranking_ready"])
        self.assertEqual(len(result["coverage"]["volume_ready_sector_ids"]), 9)
        for sector in result["sectors"][:2]:
            self.assertIsNone(sector["windows"]["5"]["volume_change_pct"])
            self.assertEqual(sector["windows"]["5"]["price_direction"], "flat")

    def test_zero_recent_volume_is_valid(self):
        for row in self.series["XLE"][-5:]:
            row["volume"] = 0
        values = self.snapshot()["sectors"][0]["windows"]["5"]
        self.assertEqual(values["volume_change_pct"], -100)
        self.assertEqual(values["volume_direction"], "down")

    def test_short_history_and_empty_calendar(self):
        self.sessions = self.sessions[-6:]
        sector = self.snapshot()["sectors"][0]
        self.assertEqual(sector["windows"]["5"]["return_pct"], 0)
        self.assertIsNone(sector["windows"]["5"]["volume_change_pct"])
        self.assertIsNone(sector["windows"]["10"]["return_pct"])
        self.sessions = []
        result = self.snapshot()
        self.assertIsNone(result["as_of_session"])
        self.assertFalse(result["coverage"]["ranking_ready"])

    def test_calendar_excludes_holiday_and_marks_shortened_session(self):
        holiday = self.sessions.pop(-4)
        shortened = self.sessions[-2]
        self.series["XLE"][-4]["close"] = 0  # Rekord w święto jest pomijany.
        self.series["XLE"][-2]["volume"] = 50
        result = self.snapshot(shortened_sessions=[shortened])
        sector = result["sectors"][0]
        self.assertEqual(result["shortened_sessions"], [shortened])
        self.assertNotIn(holiday, sector["missing_price_sessions"])
        self.assertEqual(sector["windows"]["5"]["return_pct"], 0)
        self.assertAlmostEqual(sector["windows"]["5"]["volume_change_pct"], -10)

    def test_rejects_invalid_calendar(self):
        for sessions in (
            ["2025-02-29"], ["2025-02-12", "2025-02-12"], ["2025-02-12", "2025-02-11"],
        ):
            with self.subTest(sessions=sessions), self.assertRaises(ValueError):
                calculate_rotation(self.series, sessions, source="synthetic")

    def test_rejects_invalid_calendar_types_consistently(self):
        for invalid in (None, 42, "", b"", {}, {"2025-01-06": True}, [None], ["20250106"]):
            for parameter in ("sessions", "shortened_sessions"):
                values = {"sessions": self.sessions, parameter: invalid}
                with self.subTest(parameter=parameter, value=invalid):
                    with self.assertRaisesRegex(ValueError, parameter):
                        calculate_rotation(self.series, source="synthetic", **values)
        with self.assertRaisesRegex(ValueError, "sessions"):
            calculate_rotation(self.series, set(self.sessions), source="synthetic")
        with self.assertRaisesRegex(ValueError, "shortened_sessions"):
            self.snapshot(shortened_sessions=[self.sessions[0]] * 2)

    def test_invalid_numbers_and_overflows_do_not_escape_result(self):
        for bad_price in (0, True, -1, "100", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(price=type(bad_price)):
                self.set_close("XLE", 0, bad_price)
                self.assertIsNone(self.snapshot()["sectors"][0]["windows"]["5"]["return_pct"])
        self.set_close("XLE", 5, 1e-308)
        self.set_close("XLE", 0, 1e308)
        for row in self.series["XLE"][-5:]:
            row["volume"] = 1e308
        self.series["XLB"][-1]["volume"] = -1
        self.series["XLI"] = 42
        result = self.snapshot()
        self.assertIsNone(result["sectors"][0]["windows"]["5"]["return_pct"])
        self.assertIsNone(result["sectors"][0]["windows"]["5"]["volume_change_pct"])
        self.assertIsNone(result["sectors"][1]["windows"]["5"]["volume_change_pct"])
        self.assertIsNone(result["sectors"][2]["windows"]["5"]["return_pct"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
