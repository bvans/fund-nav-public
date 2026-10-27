import unittest

from fetch_index_quotes import _market_phase, _num, parse_kline_line
from datetime import datetime, timezone, timedelta


class IndexQuoteHelpersTest(unittest.TestCase):
    def test_num_preserves_missing(self):
        self.assertIsNone(_num("-"))
        self.assertIsNone(_num(None))
        self.assertEqual(_num("6376.12"), 6376.12)

    def test_parse_kline(self):
        row = parse_kline_line(
            "2026-10-23,6400.10,6450.20,6480.30,6301.81,123456,987654321,2.80,-1.95,-128.00,1.23"
        )
        self.assertEqual(row["date"], "2026-10-23")
        self.assertEqual(row["close"], 6450.20)
        self.assertEqual(row["low"], 6301.81)
        self.assertEqual(row["pct_change"], -1.95)

    def test_market_phase(self):
        cst = timezone(timedelta(hours=8))
        self.assertEqual(_market_phase(datetime(2026, 10, 27, 11, 30, tzinfo=cst)), "lunch")
        self.assertEqual(_market_phase(datetime(2026, 10, 27, 14, 45, tzinfo=cst)), "afternoon")
        self.assertEqual(_market_phase(datetime(2026, 10, 27, 15, 10, tzinfo=cst)), "closed")


if __name__ == "__main__":
    unittest.main()
