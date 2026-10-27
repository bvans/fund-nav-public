import unittest
from datetime import datetime, timedelta, timezone

from fetch_index_quotes import (
    _market_phase,
    _num,
    _rolling_high,
    _tencent_symbol,
    parse_kline_line,
    parse_tencent_kline_row,
)


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

    def test_tencent_symbol(self):
        self.assertEqual(_tencent_symbol("000852"), "sh000852")
        self.assertEqual(_tencent_symbol("000510"), "sh000510")
        self.assertEqual(_tencent_symbol("399006"), "sz399006")

    def test_parse_tencent_kline(self):
        row = parse_tencent_kline_row(
            ["2026-10-23", "6400.10", "6450.20", "6480.30", "6301.81", "123456", "x", "x", "98765.4"]
        )
        self.assertEqual(row["date"], "2026-10-23")
        self.assertEqual(row["close"], 6450.20)
        self.assertEqual(row["low"], 6301.81)
        self.assertEqual(row["amount"], 987654000.0)

    def test_rolling_high_requires_history(self):
        high, date = _rolling_high([], 6717.99, 120)
        self.assertIsNone(high)
        self.assertIsNone(date)

    def test_rolling_high_uses_history_and_today(self):
        bars = [
            {"date": "2026-09-22", "high": 7852.23},
            {"date": "2026-10-23", "high": 6480.30},
        ]
        high, date = _rolling_high(bars, 6717.99, 120)
        self.assertEqual(high, 7852.23)
        self.assertEqual(date, "2026-09-22")

    def test_market_phase(self):
        cst = timezone(timedelta(hours=8))
        self.assertEqual(_market_phase(datetime(2026, 10, 27, 11, 30, tzinfo=cst)), "lunch")
        self.assertEqual(_market_phase(datetime(2026, 10, 27, 14, 45, tzinfo=cst)), "afternoon")
        self.assertEqual(_market_phase(datetime(2026, 10, 27, 15, 10, tzinfo=cst)), "closed")


if __name__ == "__main__":
    unittest.main()
