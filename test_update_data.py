"""Testy bez dostępu do internetu: python -m unittest -v test_update_data.py"""
import unittest
from update_data import build_card, growth_pct, periods, same_quarter_last_year, ticker_to_cik


def item(start, end, value, filed="2026-11-01", form="10-Q"):
    return {"start": start, "end": end, "val": value, "filed": filed, "form": form}


class DataTests(unittest.TestCase):
    def test_map_and_missing_symbol(self):
        m = ticker_to_cik({"0": {"ticker": "IONQ", "cik_str": 1824920, "title": "IonQ"}})
        self.assertEqual(m["IONQ"], "0001824920")
        self.assertNotIn("QNC", m)

    def test_quarter_ytd_not_quarter(self):
        values = [item("2026-01-01", "2026-09-30", 300), item("2026-07-01", "2026-09-30", 120)]
        self.assertEqual([x["val"] for x in periods(values, 75, 105)], [120])

    def test_latest_amendment_preferred(self):
        values = [item("2026-07-01", "2026-09-30", 100, "2026-10-01"), item("2026-07-01", "2026-09-30", 120, "2026-11-01")]
        self.assertEqual(periods(values, 75, 105)[0]["val"], 120)

    def test_growth_is_consolidated_not_organic(self):
        values = [item("2025-07-01", "2025-09-30", 40, "2025-11-01"), item("2026-07-01", "2026-09-30", 160)]
        facts = {"entityName": "Example, Inc.", "facts": {"us-gaap": {"Revenues": {"units": {"USD": values}}}}}
        card = build_card("EX", facts, "0000000001")
        self.assertEqual(card["wzrost_przychodow_rr_proc"], 300)
        self.assertIsNone(card["wzrost_organiczny_proc"])
        self.assertIn("kondycja_finansowa", card)
        self.assertEqual(card["kondycja_finansowa"]["oceniono_z"], 0)
        self.assertEqual(same_quarter_last_year(values)["val"], 40)

    def test_no_divide_by_zero(self):
        self.assertIsNone(growth_pct(10, 0))

    def test_no_revenue_is_missing(self):
        self.assertEqual(build_card("EX", {"facts": {}}, "0000000001")["status"], "brak_danych")


if __name__ == "__main__":
    unittest.main()
