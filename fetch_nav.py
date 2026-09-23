#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent
FUNDS_FILE = ROOT / "funds.json"
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
TZ = ZoneInfo("Asia/Shanghai")

# Public-data collector; no portfolio amounts or account data are stored here.
HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36",
}


def positive_float(v):
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def normalize_codes(values):
    """Normalize, validate and de-duplicate 6-digit fund codes."""
    seen = set()
    result = []
    for value in values:
        code = str(value).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"invalid fund code: {value!r}; expected exactly 6 digits")
        if code not in seen:
            seen.add(code)
            result.append(code)
    if not result:
        raise ValueError("no fund codes configured")
    return result


def load_codes():
    """Use workflow/manual FUND_CODES when supplied; otherwise read funds.json."""
    manual = os.getenv("FUND_CODES", "").strip()
    if manual:
        raw = re.split(r"[,;\s]+", manual)
        return normalize_codes(x for x in raw if x), True

    config = json.loads(FUNDS_FILE.read_text(encoding="utf-8"))
    raw_funds = config.get("funds", [])
    # Backward-compatible with the old [{"code": "...", "name": "..."}] format.
    codes = [x.get("code") if isinstance(x, dict) else x for x in raw_funds]
    return normalize_codes(codes), False


async def get_text(client, url, attempts=3):
    last = None
    for i in range(attempts):
        try:
            r = await client.get(url, headers=HEADERS, timeout=15.0)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                await asyncio.sleep(0.8 * (2**i))
    raise last


async def fetch_pingzhong(client, code):
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js?v={int(datetime.now().timestamp() * 1000)}"
    text = await get_text(client, url)

    name_match = re.search(r'var\s+fS_name\s*=\s*["\'](.*?)["\']\s*;', text, re.S)
    name = name_match.group(1).strip() if name_match else None

    trend_match = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
    if not trend_match:
        return None
    trend = json.loads(trend_match.group(1))
    if not trend:
        return None

    latest = trend[-1]
    nav = positive_float(latest.get("y"))
    ts = latest.get("x")
    if nav is None or ts is None:
        return None

    date = datetime.fromtimestamp(float(ts) / 1000.0, TZ).strftime("%Y-%m-%d")
    return {
        "name": name,
        "nav_date": date,
        "unit_nav": nav,
        "source": "eastmoney:pingzhongdata",
    }


async def fetch_fundgz(client, code):
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(datetime.now().timestamp() * 1000)}"
    text = await get_text(client, url)
    m = re.search(r"jsonpgz\((.*?)\)", text, re.S)
    if not m or not m.group(1).strip():
        return None

    inner = m.group(1)
    try:
        obj = json.loads(inner)
    except json.JSONDecodeError:
        obj = {}
        for key in ("name", "dwjz", "jzrq"):
            x = re.search(rf'"{key}"\s*:\s*"([^"]*)"', inner)
            if x:
                obj[key] = x.group(1)

    # Deliberately ignore gsz: it is an intraday estimate, not official NAV.
    nav = positive_float(obj.get("dwjz"))
    date = str(obj.get("jzrq") or "").strip()
    name = str(obj.get("name") or "").strip() or None
    if nav is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return None
    return {
        "name": name,
        "nav_date": date,
        "unit_nav": nav,
        "source": "eastmoney:fundgz-dwjz",
    }


def choose(candidates):
    valid = [x for x in candidates if x]
    if not valid:
        return None

    latest_date = max(x["nav_date"] for x in valid)
    same_day = [x for x in valid if x["nav_date"] == latest_date]
    same_day.sort(key=lambda x: x["source"] == "eastmoney:pingzhongdata", reverse=True)

    chosen = dict(same_day[0])
    values = sorted({round(float(x["unit_nav"]), 8) for x in same_day})
    chosen["same_day_source_values"] = values
    chosen["source_conflict"] = len(values) > 1

    if not chosen.get("name"):
        chosen["name"] = next((x.get("name") for x in valid if x.get("name")), None)
    return chosen


async def fetch_one(client, sem, code):
    async with sem:
        a, b = await asyncio.gather(
            fetch_pingzhong(client, code),
            fetch_fundgz(client, code),
            return_exceptions=True,
        )

    candidates = [None if isinstance(x, Exception) else x for x in (a, b)]
    picked = choose(candidates)
    row = {
        "code": code,
        "name": None,
        "nav_date": None,
        "unit_nav": None,
        "source": None,
        "source_conflict": False,
        "same_day_source_values": [],
        "error": None,
    }

    if picked:
        row.update(picked)
    else:
        errors = [str(x) for x in (a, b) if isinstance(x, Exception)]
        row["error"] = "; ".join(errors) or "no official NAV returned"

    if not row["name"]:
        row["name"] = code
    return row


async def main():
    try:
        codes, manual_query = load_codes()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(exist_ok=True)

    limits = httpx.Limits(max_connections=12, max_keepalive_connections=8)
    async with httpx.AsyncClient(
        follow_redirects=True, trust_env=False, limits=limits
    ) as client:
        sem = asyncio.Semaphore(6)
        rows = await asyncio.gather(*(fetch_one(client, sem, code) for code in codes))

    rows.sort(key=lambda x: x["code"])
    now = datetime.now(TZ)
    ok = [x for x in rows if x["unit_nav"] is not None]
    conflicts = [x for x in rows if x["source_conflict"]]

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "query_mode": "manual" if manual_query else "configured",
        "fund_count": len(rows),
        "official_nav_found": len(ok),
        "source_conflicts": len(conflicts),
        "funds": rows,
    }

    if manual_query:
        latest_json = DATA_DIR / "manual_latest_nav.json"
        latest_csv = DATA_DIR / "manual_latest_nav.csv"
    else:
        latest_json = DATA_DIR / "latest_nav.json"
        latest_csv = DATA_DIR / "latest_nav.csv"

    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    latest_json.write_text(body, encoding="utf-8")

    if not manual_query:
        history_json = HISTORY_DIR / f"{now.strftime('%Y-%m-%d')}.json"
        history_json.write_text(body, encoding="utf-8")

    with latest_csv.open("w", newline="", encoding="utf-8-sig") as f:
        fields = [
            "code",
            "name",
            "nav_date",
            "unit_nav",
            "source",
            "source_conflict",
            "error",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"query mode: {'manual' if manual_query else 'configured'}")
    print(f"official NAV: {len(ok)}/{len(rows)}")
    for row in rows:
        print(
            f"{row['code']} {row['name']} "
            f"{row['nav_date'] or '-'} {row['unit_nav'] if row['unit_nav'] is not None else '-'}"
        )

    if conflicts:
        print("same-day source conflicts:", ", ".join(x["code"] for x in conflicts))
    missing = [x["code"] for x in rows if x["unit_nav"] is None]
    if missing:
        print("missing:", ", ".join(missing))

    # Fail only when the public sources are broadly unavailable.
    if len(ok) < max(1, int(len(rows) * 0.90)):
        print("ERROR: official NAV success ratio below 90%", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
