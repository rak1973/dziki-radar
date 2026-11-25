"""Testy oceny finansowej: python -m unittest -v test_score_finance.py"""
import unittest
from score_finance import evaluate_financial_health


def instant(tag, val, end="2026-09-30"):
    return tag, {"units": {"USD": [{"end": end, "val": val, "form": "10-Q", "filed": "2026-11-01"}]}}


def annual_cf():
    return {"units": {"USD": [
        {"start":"2025-01-01", "end":"2025-12-31", "val":-100_000_000, "form":"10-K", "filed":"2026-02-01"},
        {"start":"2025-01-01", "end":"2025-09-30", "val":-75_000_000, "form":"10-Q", "filed":"2025-11-01"},
        {"start":"2026-01-01", "end":"2026-09-30", "val":-90_000_000, "form":"10-Q", "filed":"2026-11-01"},
    ]}}


def facts(assets=None, liabilities=None, cash=None, cashflow=True):
    gaap={}
    for tag,value in [("AssetsCurrent",assets), ("LiabilitiesCurrent",liabilities), ("CashAndCashEquivalentsAtCarryingValue",cash)]:
        if value is not None:
            k,v=instant(tag,value);gaap[k]=v
    if cashflow:
        gaap["NetCashProvidedByUsedInOperatingActivities"]=annual_cf()
    return {"facts":{"us-gaap":gaap}}


class FinancialScoreTests(unittest.TestCase):
    def test_full_score_measured_and_ttm_formula(self):
        # Cashflow TTM FY(-100) + YTD(-90) - prior YTD(-75) = -115 mln.
        result=evaluate_financial_health(facts(300_000_000,100_000_000,300_000_000),"2026-09-30")
        self.assertTrue(result["kompletna"])
        self.assertEqual(result["oceniono_z"],12)
        self.assertEqual([r["punkty"] for r in result["skladniki"]],[4,4,3])
        self.assertEqual(result["punkty"],11)

    def test_missing_is_not_zero(self):
        result=evaluate_financial_health(facts(assets=100,liabilities=100,cash=None,cashflow=False),"2026-09-30")
        self.assertFalse(result["kompletna"])
        self.assertEqual(result["oceniono_z"],4)
        self.assertEqual(result["punkty"],1)
        self.assertEqual(result["skladniki"][1]["punkty"],None)

    def test_no_data_unevaluated(self):
        result=evaluate_financial_health({"facts":{}},"2026-09-30")
        self.assertIsNone(result["punkty"])
        self.assertEqual(result["oceniono_z"],0)
        self.assertFalse(result["kompletna"])

    def test_nonmatching_balance_sheet_date_rejected(self):
        result=evaluate_financial_health(facts(200,100,300,cashflow=False),"2026-06-30")
        self.assertEqual(result["oceniono_z"],0)

    def test_positive_operating_cashflow(self):
        data=facts(300,100,50)
        cf=data['facts']['us-gaap']['NetCashProvidedByUsedInOperatingActivities']['units']['USD']
        for r in cf: r['val']=abs(r['val'])
        result=evaluate_financial_health(data,"2026-09-30")
        self.assertEqual(result['skladniki'][2]['punkty'],4)

    def test_stale_cashflow_rejected(self):
        data=facts(300,100,50)
        data['facts']['us-gaap']['NetCashProvidedByUsedInOperatingActivities']['units']['USD']=[
            {"start":"2023-01-01","end":"2023-12-31","val":5,"form":"10-K"}]
        result=evaluate_financial_health(data,"2026-09-30")
        self.assertEqual(result['oceniono_z'],8)
        self.assertFalse(result['kompletna'])

if __name__=='__main__': unittest.main()
