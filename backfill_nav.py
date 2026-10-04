#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent
HISTORY_DIR = ROOT / "data" / "all" / "history"
HEADERS = {
    "Accept": "*/*",
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36",
}

def number(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in {"--", "---", "None", "nan"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None

def load_default_codes() -> list[str]:
    obj = json.loads((ROOT / "funds.json").read_text(encoding="utf-8"))
    return sorted({str(x).zfill(6) for x in obj.get("funds", []) if re.fullmatch(r"\d{1,6}", str(x))})

def parse_codes(raw: str | None) -> list[str]:
    if not raw:
        return load_default_codes()
    codes = re.findall(r"\b\d{6}\b", raw)
    if not codes:
        raise ValueError("codes must contain at least one 6-digit fund code")
    return sorted(set(codes))

def write_csv_gz(path: Path, df: pd.DataFrame):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = df.to_csv(index=False, lineterminator="\n").encode("utf-8-sig")
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
            gz.write(payload)

def merge_history(nav_date: str, records: list[dict]):
    if not records:
        return
    path = HISTORY_DIR / nav_date[:4] / f"{nav_date}.csv.gz"
    cols = ["code", "name", "type", "nav_date", "unit_nav", "accum_nav", "source"]
    new_df = pd.DataFrame(records)
    for col in cols:
        if col not in new_df:
            new_df[col] = None
    new_df = new_df[cols]
    if path.exists():
        old = pd.read_csv(path, dtype={"code": str}, compression="gzip")
        old["code"] = old["code"].astype(str).str.zfill(6)
        merged = pd.concat([old, new_df], ignore_index=True).drop_duplicates("code", keep="last")
    else:
        merged = new_df
    merged["code"] = merged["code"].astype(str).str.zfill(6)
    write_csv_gz(path, merged.sort_values("code").reset_index(drop=True))

async def fetch_window(client, code: str, start: str, end: str):
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    headers = dict(HEADERS)
    headers["Referer"] = f"https://fundf10.eastmoney.com/jjjz_{code}.html"
    params = {
        "fundCode": code, "pageIndex": "1", "pageSize": "100",
        "startDate": start, "endDate": end,
        "_": str(int(datetime.now().timestamp() * 1000)),
    }
    last = None
    for attempt in range(4):
        try:
            r = await client.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data = (r.json().get("Data") or {})
            rows = data.get("LSJZList") or []
            return rows, int(data.get("TotalCount") or len(rows))
        except Exception as exc:
            last = exc
            if attempt == 3:
                raise RuntimeError(f"{code} {start}..{end}: {last}") from last
            await asyncio.sleep(1.2 * (2 ** attempt))

async def fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, code: str, start: str, end: str):
    # Eastmoney lsjz effectively caps broad date-range queries. Split into small
    # calendar windows so early history is not silently truncated to the last ~20 rows.
    begin = date.fromisoformat(start)
    finish = date.fromisoformat(end)
    out_by_date = {}
    async with sem:
        cursor = begin
        while cursor <= finish:
            window_end = min(cursor + timedelta(days=27), finish)
            rows, total = await fetch_window(client, code, cursor.isoformat(), window_end.isoformat())
            if total > 100:
                raise RuntimeError(
                    f"{code} window {cursor}..{window_end} returned TotalCount={total}; "
                    "reduce window size instead of silently truncating"
                )
            for item in rows:
                d = str(item.get("FSRQ") or "")
                nav = number(item.get("DWJZ"))
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and nav is not None:
                    out_by_date[d] = {
                        "code": code,
                        "name": "",
                        "type": "",
                        "nav_date": d,
                        "unit_nav": nav,
                        "accum_nav": number(item.get("LJJZ")),
                        "source": "eastmoney:lsjz-backfill",
                    }
            cursor = window_end + timedelta(days=1)
            await asyncio.sleep(0.12)
    return code, [out_by_date[d] for d in sorted(out_by_date)]

async def main_async(args):
    codes = parse_codes(args.codes)
    start = date.fromisoformat(args.start).isoformat()
    end = date.fromisoformat(args.end).isoformat()
    if start > end:
        raise ValueError("start must be <= end")
    print(f"Backfill {len(codes)} funds from {start} to {end}")

    limits = httpx.Limits(max_connections=10, max_keepalive_connections=8)
    by_date: dict[str, list[dict]] = {}
    failures = []
    async with httpx.AsyncClient(follow_redirects=True, trust_env=False, limits=limits) as client:
        sem = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(
            *(fetch_one(client, sem, code, start, end) for code in codes),
            return_exceptions=True,
        )
    for code, result in zip(codes, results):
        if isinstance(result, Exception):
            failures.append((code, str(result)))
            continue
        _, rows = result
        print(f"{code}: {len(rows)} NAV rows")
        for rec in rows:
            by_date.setdefault(rec["nav_date"], []).append(rec)

    for nav_date in sorted(by_date):
        merge_history(nav_date, by_date[nav_date])

    print(f"History dates written/merged: {len(by_date)}")
    if not by_date:
        raise RuntimeError("backfill produced zero NAV dates; refusing silent success")
    if failures:
        for code, err in failures:
            print(f"ERROR {code}: {err}")
        raise SystemExit(2)

def main():
    p = argparse.ArgumentParser(description="Backfill formal fund NAV history from Eastmoney.")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-07-23")
    p.add_argument("--codes", default="", help="Comma/space separated codes; empty = funds.json")
    p.add_argument("--concurrency", type=int, default=5)
    args = p.parse_args()
    asyncio.run(main_async(args))

if __name__ == "__main__":
    main()
