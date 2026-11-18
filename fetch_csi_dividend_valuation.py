#!/usr/bin/env python3
"""CSI Dividend 000922 valuation collector. Historical PE is NOT historical dividend yield."""
import csv
import io
import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("data/indices/valuation/000922")
URL = "https://www.csindex.com.cn/csindex-home/perf/indexCsiDsPe"
CST = timezone(timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.csindex.com.cn/"}


def get_official_pe(start="20100104"):
    end = datetime.now(CST).strftime("%Y%m%d")
    url = URL + "?" + urllib.parse.urlencode({"indexCode": "000922", "startDate": start, "endDate": end})
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45) as response:
        payload = json.load(response)
    rows = payload.get("data") or []
    if not rows:
        raise RuntimeError("Official CSI historic PE endpoint returned no data")
    result = {}
    for row in rows:
        date = str(row.get("tradeDate", ""))
        try:
            datetime.strptime(date, "%Y%m%d")
            val = float(row["peg"])
        except (ValueError, TypeError, KeyError):
            continue
        if 0 < val < 1000 and math.isfinite(val):
            result[f"{date[:4]}-{date[4:6]}-{date[6:]}"] = {
                "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
                "pe": val,
                "source": "csindex:indexCsiDsPe.peg",
            }
    if len(result) < 1000:
        raise RuntimeError(f"Official PE history suspiciously short: {len(result)}")
    return sorted(result.values(), key=lambda x: x["date"])


def get_recent_yield():
    """Official recent indicator export via AKShare. Never extrapolate backwards."""
    try:
        import akshare as ak
        df = ak.stock_zh_index_value_csindex(symbol="000922")
        names = {"日期": "date", "市盈率1": "pe1", "市盈率2": "pe2",
                 "股息率1": "yield1_pct", "股息率2": "yield2_pct"}
        if not set(names).issubset(df.columns):
            raise ValueError(f"Unexpected indicator schema: {list(df.columns)}")
        result = []
        for _, row in df.iterrows():
            dt = str(row["日期"])[:10]
            try:
                datetime.strptime(dt, "%Y-%m-%d")
                y1 = float(row["股息率1"])
                y2 = float(row["股息率2"])
                if not all(math.isfinite(v) and 0 < v < 100 for v in (y1, y2)):
                    continue
            except (ValueError, TypeError):
                continue
            result.append({"date": dt, "yield1_pct": y1, "yield2_pct": y2,
                           "source": "csindex:000922indicator.xls"})
        return sorted(result, key=lambda x: x["date"])
    except Exception as exc:
        print(f"WARNING: recent official yield unavailable: {exc}", flush=True)
        return []


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def main():
    pe = get_official_pe()
    write_csv(BASE / "pe_history.csv", pe, ["date", "pe", "source"])
    observed = get_recent_yield()
    path = BASE / "yield_observed.csv"
    saved = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as fh:
            saved = {row["date"]: row for row in csv.DictReader(fh)}
    for row in observed:
        saved[row["date"]] = row
    if saved:
        write_csv(path, [saved[k] for k in sorted(saved)],
                  ["date", "yield1_pct", "yield2_pct", "source"])
    meta = {"as_of": datetime.now(CST).isoformat(), "index_code": "000922",
            "historical_pe_rows": len(pe), "pe_first": pe[0]["date"],
            "pe_last": pe[-1]["date"], "observed_yield_rows": len(saved),
            "yield_last": max(saved) if saved else None,
            "note": "PE is historical; dividend yield is only observed official snapshots. No hindsight payout-ratio imputation."}
    (BASE / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
