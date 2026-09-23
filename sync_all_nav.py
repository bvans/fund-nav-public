#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import gzip
import io
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "all"
HISTORY_DIR = DATA_DIR / "history"
TZ = ZoneInfo("Asia/Shanghai")

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36",
}


def clean_code(value) -> str:
    text = str(value).strip()
    m = re.search(r"(\d{6})", text)
    return m.group(1) if m else ""


def number(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "---", "nan", "None"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def date_from_nav_column(column: str, suffix: str) -> str | None:
    if not column.endswith(suffix):
        return None
    raw = column[: -len(suffix)].rstrip("-").strip()
    try:
        return pd.to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return None


def write_csv_gz(path: Path, df: pd.DataFrame):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = df.to_csv(index=False, lineterminator="\n")
    # mtime=0 makes repeated output deterministic and avoids meaningless git diffs.
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
            gz.write(text.encode("utf-8-sig"))


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_json_gz(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
            gz.write(payload)


def load_catalog() -> tuple[pd.DataFrame, dict[str, dict]]:
    print("Fetching full fund catalog via AKShare fund_name_em() ...")
    df = ak.fund_name_em().copy()

    rename = {}
    for col in df.columns:
        name = str(col)
        if name == "基金代码":
            rename[col] = "code"
        elif name == "基金简称":
            rename[col] = "name"
        elif name == "基金类型":
            rename[col] = "type"
        elif name == "拼音缩写":
            rename[col] = "pinyin"
    df = df.rename(columns=rename)

    if "code" not in df or "name" not in df:
        raise RuntimeError(f"unexpected fund catalog columns: {list(df.columns)}")

    for optional in ("type", "pinyin"):
        if optional not in df:
            df[optional] = ""

    df = df[["code", "name", "type", "pinyin"]].copy()
    df["code"] = df["code"].map(clean_code)
    df = df[df["code"].str.fullmatch(r"\d{6}")].drop_duplicates("code", keep="last")
    df = df.fillna("").sort_values("code").reset_index(drop=True)

    mapping = {
        row["code"]: {
            "name": str(row["name"]),
            "type": str(row["type"]),
            "pinyin": str(row["pinyin"]),
        }
        for _, row in df.iterrows()
    }
    return df, mapping


def load_bulk_nav(catalog: dict[str, dict]):
    print("Fetching full open-fund NAV table via AKShare fund_open_fund_daily_em() ...")
    df = ak.fund_open_fund_daily_em().copy()
    if "基金代码" not in df or "基金简称" not in df:
        raise RuntimeError(f"unexpected NAV columns: {list(df.columns)}")

    unit_cols: dict[str, str] = {}
    accum_cols: dict[str, str] = {}
    for col in df.columns:
        col_s = str(col)
        d = date_from_nav_column(col_s, "-单位净值")
        if d:
            unit_cols[d] = col
        d = date_from_nav_column(col_s, "-累计净值")
        if d:
            accum_cols[d] = col

    if not unit_cols:
        raise RuntimeError(f"could not find dated unit NAV columns: {list(df.columns)}")

    dated_records: dict[str, dict[str, dict]] = defaultdict(dict)
    latest_by_code: dict[str, dict] = {}

    for _, row in df.iterrows():
        code = clean_code(row.get("基金代码"))
        if not code:
            continue
        name = str(row.get("基金简称") or catalog.get(code, {}).get("name") or code).strip()
        fund_type = catalog.get(code, {}).get("type", "")

        observations = []
        for nav_date, col in unit_cols.items():
            unit_nav = number(row.get(col))
            if unit_nav is None:
                continue
            accum_nav = number(row.get(accum_cols.get(nav_date))) if nav_date in accum_cols else None
            rec = {
                "code": code,
                "name": name,
                "type": fund_type,
                "nav_date": nav_date,
                "unit_nav": unit_nav,
                "accum_nav": accum_nav,
                "source": "eastmoney:bulk-daily",
            }
            observations.append(rec)
            dated_records[nav_date][code] = rec

        if observations:
            latest_by_code[code] = max(observations, key=lambda x: x["nav_date"])

    nav_dates = sorted(unit_cols.keys())
    print(f"Bulk table rows: {len(df)}, dated columns: {nav_dates}")
    return dated_records, latest_by_code, nav_dates


async def get_text(client: httpx.AsyncClient, url: str, attempts: int = 3):
    last = None
    for i in range(attempts):
        try:
            r = await client.get(url, headers=HEADERS, timeout=20.0)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                await asyncio.sleep(0.8 * (2**i))
    raise last


async def fetch_recent_history(client: httpx.AsyncClient, sem: asyncio.Semaphore, code: str, days: int = 10):
    """Fetch recent formal NAV rows without downloading the fund's entire history."""
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    params = {
        "fundCode": code,
        "pageIndex": "1",
        "pageSize": str(days),
        "startDate": "",
        "endDate": "",
        "_": str(int(datetime.now().timestamp() * 1000)),
    }
    headers = dict(HEADERS)
    headers["Referer"] = f"https://fundf10.eastmoney.com/jjjz_{code}.html"

    last = None
    async with sem:
        for i in range(3):
            try:
                r = await client.get(url, params=params, headers=headers, timeout=20.0)
                r.raise_for_status()
                obj = r.json()
                rows = ((obj.get("Data") or {}).get("LSJZList") or [])
                out = []
                for item in rows:
                    nav_date = str(item.get("FSRQ") or "").strip()
                    unit_nav = number(item.get("DWJZ"))
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", nav_date) or unit_nav is None:
                        continue
                    out.append(
                        {
                            "nav_date": nav_date,
                            "unit_nav": unit_nav,
                            "accum_nav": number(item.get("LJJZ")),
                        }
                    )
                return code, None, out
            except Exception as exc:
                last = exc
                if i + 1 < 3:
                    await asyncio.sleep(0.8 * (2**i))
    raise last


async def repair_delayed_funds(
    catalog: dict[str, dict],
    latest_by_code: dict[str, dict],
    bulk_dates: list[str],
    dated_records: dict[str, dict[str, dict]],
):
    if not bulk_dates:
        return {}

    newest_date = max(bulk_dates)
    repair_codes = set()

    # QDII/overseas funds often publish T+1/T+2, so explicitly backfill them.
    for code, meta in catalog.items():
        fund_type = str(meta.get("type", "")).upper()
        if "QDII" in fund_type:
            repair_codes.add(code)

    # Also repair funds present in the bulk table but still lacking the newest dated NAV.
    for code, rec in latest_by_code.items():
        if rec["nav_date"] < newest_date:
            repair_codes.add(code)

    if not repair_codes:
        print("No delayed funds require repair.")
        return {}

    print(f"Backfilling recent history for {len(repair_codes)} delayed/QDII funds ...")
    limits = httpx.Limits(max_connections=16, max_keepalive_connections=12)
    results = {}
    async with httpx.AsyncClient(follow_redirects=True, trust_env=False, limits=limits) as client:
        sem = asyncio.Semaphore(10)
        tasks = [fetch_recent_history(client, sem, code) for code in sorted(repair_codes)]
        fetched = await asyncio.gather(*tasks, return_exceptions=True)

    errors = 0
    for item in fetched:
        if isinstance(item, Exception):
            errors += 1
            continue
        code, name, observations = item
        fund_type = catalog.get(code, {}).get("type", "")
        if observations:
            latest_obs = max(observations, key=lambda x: x["nav_date"])
            results[code] = {
                "code": code,
                "name": name or catalog.get(code, {}).get("name") or code,
                "type": fund_type,
                "nav_date": latest_obs["nav_date"],
                "unit_nav": latest_obs["unit_nav"],
                "accum_nav": None,
                "source": "eastmoney:lsjz-repair",
            }

        for obs in observations:
            nav_date = obs["nav_date"]
            existing = dated_records[nav_date].get(code)
            # Repair source is authoritative for unit NAV on its explicit historical date.
            dated_records[nav_date][code] = {
                "code": code,
                "name": name or catalog.get(code, {}).get("name") or code,
                "type": fund_type,
                "nav_date": nav_date,
                "unit_nav": obs["unit_nav"],
                "accum_nav": obs.get("accum_nav") if obs.get("accum_nav") is not None else (existing.get("accum_nav") if existing else None),
                "source": "eastmoney:lsjz-repair",
            }

    print(f"Backfill completed; request errors: {errors}")
    return results


def merge_history_file(nav_date: str, records: dict[str, dict]):
    if not records:
        return

    year = nav_date[:4]
    path = HISTORY_DIR / year / f"{nav_date}.csv.gz"
    new_df = pd.DataFrame(records.values())

    cols = ["code", "name", "type", "nav_date", "unit_nav", "accum_nav", "source"]
    for col in cols:
        if col not in new_df:
            new_df[col] = None
    new_df = new_df[cols]

    if path.exists():
        try:
            old_df = pd.read_csv(path, dtype={"code": str}, compression="gzip")
            old_df["code"] = old_df["code"].astype(str).str.zfill(6)
            merged = pd.concat([old_df, new_df], ignore_index=True)
            merged = merged.drop_duplicates("code", keep="last")
        except Exception as exc:
            print(f"WARNING: failed reading existing {path}: {exc}; rewriting")
            merged = new_df
    else:
        merged = new_df

    merged["code"] = merged["code"].astype(str).str.zfill(6)
    merged = merged.sort_values("code").reset_index(drop=True)
    write_csv_gz(path, merged)


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    catalog_df, catalog = load_catalog()
    dated_records, latest_by_code, bulk_dates = load_bulk_nav(catalog)
    repaired_latest = await repair_delayed_funds(catalog, latest_by_code, bulk_dates, dated_records)

    for code, repaired in repaired_latest.items():
        old = latest_by_code.get(code)
        if old is None or repaired["nav_date"] >= old["nav_date"]:
            # Keep accumulated NAV from bulk if both refer to the same date.
            if old and old["nav_date"] == repaired["nav_date"] and old.get("accum_nav") is not None:
                repaired["accum_nav"] = old["accum_nav"]
            latest_by_code[code] = repaired

    # Persist actual NAV by NAV date. Re-runs merge into the same day, so delayed
    # funds can backfill prior dates without losing previously stored records.
    for nav_date in sorted(dated_records):
        merge_history_file(nav_date, dated_records[nav_date])

    latest_rows = sorted(latest_by_code.values(), key=lambda x: x["code"])
    now = datetime.now(TZ)
    latest_payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "coverage": "open-ended public funds from Eastmoney/AKShare; delayed/QDII recent history repaired from Eastmoney lsjz",
        "fund_count": len(latest_rows),
        "newest_bulk_nav_date": max(bulk_dates) if bulk_dates else None,
        "funds": latest_rows,
    }

    # Keep a convenient latest file. History itself is partitioned by actual NAV date.
    write_json(DATA_DIR / "latest_nav.json", latest_payload)
    write_csv_gz(DATA_DIR / "latest_nav.csv.gz", pd.DataFrame(latest_rows))

    catalog_payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "fund_count": len(catalog_df),
        "funds": catalog_df.to_dict(orient="records"),
    }
    write_json_gz(DATA_DIR / "fund_catalog.json.gz", catalog_payload)

    print(f"Catalog: {len(catalog_df)} funds")
    print(f"Latest NAV rows: {len(latest_rows)}")
    print(f"History dates touched: {len(dated_records)}")
    print(f"Newest bulk NAV date: {max(bulk_dates) if bulk_dates else '-'}")

    # Fail loudly if the bulk source is clearly incomplete.
    if len(latest_rows) < 5000:
        print("ERROR: suspiciously small full-market NAV result", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
