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
# Calendar days, not exchange trading days: a warning/retry, not an assertion
# that a delayed QDII NAV is wrong (holidays can exceed this interval).
STALE_AFTER_DAYS = max(1, int(os.getenv("NAV_RETRY_AGE_DAYS", "5")))
STALE_RETRIES = min(3, max(0, int(os.getenv("NAV_STALE_RETRIES", "2")))

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


def nav_age_days(nav_date, today=None):
    """Elapsed calendar days; do not assume QDII market holidays are trading days."""
    if not nav_date:
        return None
    try:
        nav_day = datetime.strptime(nav_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return max(0, ((today or datetime.now(TZ).date()) - nav_day).days)


async def observe_source(client, fetcher, source, code, attempt):
    """Record both success and failure for every source and every attempt."""
    checked_at = datetime.now(TZ).isoformat(timespec="seconds")
    observation = {
        "source": source,
        "attempt": attempt,
        "checked_at": checked_at,
        "status": "no_data",
        "nav_date": None,
        "unit_nav": None,
        "error": None,
    }
    try:
        candidate = await fetcher(client, code)
        if candidate:
            observation.update(
                status="ok",
                nav_date=candidate["nav_date"],
                unit_nav=candidate["unit_nav"],
            )
        return candidate, observation
    except Exception as exc:
        observation.update(status="error", error=str(exc)[:300])
        return None, observation


async def fetch_one(client, sem, code):
    candidates = []
    observations = []
    attempts = 0

    for attempt in range(1, STALE_RETRIES + 2):
        attempts = attempt
        async with sem:
            checks = await asyncio.gather(
                observe_source(
                    client, fetch_pingzhong, "eastmoney:pingzhongdata", code, attempt
                ),
                observe_source(
                    client, fetch_fundgz, "eastmoney:fundgz-dwjz", code, attempt
                ),
            )
        candidates.extend(candidate for candidate, _ in checks if candidate)
        observations.extend(observation for _, observation in checks)

        picked = choose(candidates)
        age = nav_age_days(picked["nav_date"]) if picked else None
        if picked and age is not None and age < STALE_AFTER_DAYS:
            break
        # Extra fetches can detect late updates, but cannot force a holiday NAV.
        if attempt <= STALE_RETRIES:
            await asyncio.sleep(1.5 * attempt)

    picked = choose(candidates)
    age = nav_age_days(picked["nav_date"]) if picked else None
    delayed = age is not None and age >= STALE_AFTER_DAYS
    row = {
        "code": code,
        "name": None,
        "nav_date": None,
        "unit_nav": None,
        "source": None,
        "source_conflict": False,
        "same_day_source_values": [],
        "error": None,
        "checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "days_since_nav": age,
        "stale_warning": delayed,
        "freshness_status": (
            "no_nav" if picked is None else "delayed_or_holiday" if delayed else "recent"
        ),
        "retry_count": attempts - 1,
        "source_checks": observations,
    }
    if picked:
        row.update(picked)
    else:
        errors = [x["error"] for x in observations if x["error"]]
        row["error"] = "; ".join(errors[-2:]) or "no official NAV returned"
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
    delayed = [x for x in rows if x["stale_warning"]]

    request_id = os.getenv("REQUEST_ID", "").strip() or None

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "query_mode": "manual" if manual_query else "configured",
        "request_id": request_id if manual_query else None,
        "fund_count": len(rows),
        "official_nav_found": len(ok),
        "source_conflicts": len(conflicts),
        "stale_warning_count": len(delayed),
        "stale_after_calendar_days": STALE_AFTER_DAYS,
        "funds": rows,
    }

    if manual_query:
        latest_json = DATA_DIR / "manual_latest_nav.json"
        latest_csv = DATA_DIR / "manual_latest_nav.csv"
    else:
        latest_json = DATA_DIR / "latest_nav.json"
        latest_csv = DATA_DIR / "latest_nav.csv"

    # Audit every run; manual and scheduled paths are separate for safe Git pushes.
    audit_dir = DATA_DIR / "fetch_logs" / ("manual" if manual_query else "configured")
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_file = audit_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
    audit_entry = {
        "collected_at": payload["generated_at"],
        "query_mode": payload["query_mode"],
        "request_id": payload["request_id"],
        "stale_after_calendar_days": STALE_AFTER_DAYS,
        "funds": [
            {
                "code": x["code"],
                "nav_date": x["nav_date"],
                "unit_nav": x["unit_nav"],
                "source": x["source"],
                "freshness_status": x["freshness_status"],
                "retry_count": x["retry_count"],
                "source_checks": x["source_checks"],
            }
            for x in rows
        ],
    }
    with audit_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")

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
            "checked_at",
            "days_since_nav",
            "stale_warning",
            "freshness_status",
            "retry_count",
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
    if delayed:
        print(
            f"WARNING: delayed/holiday NAV (calendar days >= {STALE_AFTER_DAYS}):",
            ", ".join(f"{x['code']}({x['days_since_nav']}d)" for x in delayed),
        )
    print(f"source audit: {audit_file.relative_to(ROOT)}")

    # Fail only when the public sources are broadly unavailable.
    if len(ok) < max(1, int(len(rows) * 0.90)):
        print("ERROR: official NAV success ratio below 90%", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
