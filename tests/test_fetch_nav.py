"""Offline regression tests for NAV freshness and per-source audit metadata."""

import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import fetch_nav


def observation(date, nav, source="eastmoney:pingzhongdata"):
    return {"name": "test fund", "nav_date": date, "unit_nav": nav, "source": source}


class NavFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_result_no_extra_fetches(self):
        today = datetime.now(fetch_nav.TZ).date().isoformat()
        with (
            patch.object(fetch_nav, "fetch_pingzhong", new_callable=AsyncMock) as primary,
            patch.object(fetch_nav, "fetch_fundgz", new_callable=AsyncMock) as backup,
        ):
            primary.return_value = observation(today, 2.3456)
            backup.return_value = observation(today, 2.3456, "eastmoney:fundgz-dwjz")
            row = await fetch_nav.fetch_one(None, asyncio.Semaphore(1), "016532")
        self.assertEqual(row["retry_count"], 0)
        self.assertEqual(row["freshness_status"], "recent")
        self.assertEqual(len(row["source_checks"]), 2)
        self.assertTrue(all(x["status"] == "ok" for x in row["source_checks"]))

    async def test_retries_and_chooses_newer_official_nav(self):
        today = datetime.now(fetch_nav.TZ).date()
        old = (today - timedelta(days=9)).isoformat()
        fresh = today.isoformat()
        with (
            patch.object(fetch_nav, "fetch_pingzhong", new_callable=AsyncMock) as primary,
            patch.object(fetch_nav, "fetch_fundgz", new_callable=AsyncMock) as backup,
            patch.object(fetch_nav.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            primary.side_effect = [observation(old, 2.2234), observation(fresh, 2.2262)]
            backup.return_value = None
            row = await fetch_nav.fetch_one(None, asyncio.Semaphore(1), "016532")
        self.assertEqual(primary.await_count, 2)
        self.assertEqual(sleep.await_count, 1)
        self.assertEqual(row["nav_date"], fresh)
        self.assertEqual(row["unit_nav"], 2.2262)
        self.assertFalse(row["stale_warning"])
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(len(row["source_checks"]), 4)
        self.assertEqual([x["attempt"] for x in row["source_checks"]], [1, 1, 2, 2])

    async def test_holiday_does_not_fabricate_fresh_nav(self):
        old = (datetime.now(fetch_nav.TZ).date() - timedelta(days=9)).isoformat()
        with (
            patch.object(fetch_nav, "fetch_pingzhong", new_callable=AsyncMock) as primary,
            patch.object(fetch_nav, "fetch_fundgz", new_callable=AsyncMock) as backup,
            patch.object(fetch_nav.asyncio, "sleep", new_callable=AsyncMock),
        ):
            primary.return_value = observation(old, 2.2234)
            backup.return_value = observation(old, 2.2234, "eastmoney:fundgz-dwjz")
            row = await fetch_nav.fetch_one(None, asyncio.Semaphore(1), "016532")
        self.assertEqual(row["nav_date"], old)
        self.assertTrue(row["stale_warning"])
        self.assertEqual(row["freshness_status"], "delayed_or_holiday")
        self.assertEqual(row["retry_count"], fetch_nav.STALE_RETRIES)
        self.assertEqual(primary.await_count, fetch_nav.STALE_RETRIES + 1)

    async def test_errors_recorded_per_source(self):
        with (
            patch.object(fetch_nav, "fetch_pingzhong", new_callable=AsyncMock) as primary,
            patch.object(fetch_nav, "fetch_fundgz", new_callable=AsyncMock) as backup,
            patch.object(fetch_nav.asyncio, "sleep", new_callable=AsyncMock),
        ):
            primary.side_effect = RuntimeError("upstream unavailable")
            backup.return_value = None
            row = await fetch_nav.fetch_one(None, asyncio.Semaphore(1), "016532")
        self.assertEqual(row["freshness_status"], "no_nav")
        self.assertIsNone(row["unit_nav"])
        self.assertIn("upstream unavailable", row["error"])
        self.assertEqual(row["source_checks"][0]["status"], "error")
        self.assertEqual(row["source_checks"][1]["status"], "no_data")


if __name__ == "__main__":
    unittest.main()
