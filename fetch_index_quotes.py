#!/usr/bin/env python3
"""Fetch mainland China index quotes from Eastmoney push2 APIs.

Primary use case: GitHub Actions creates a small, auditable market-data cache
that can be read directly by monitoring/analysis tools without scraping pages.

Data sources:
- Realtime snapshot: /api/qt/ulist.np/get
- Daily K-line:      /api/qt/stock/kline/get

The endpoint/field mapping is the same family used by open-source projects such
as AKShare and other Eastmoney providers.  We deliberately fail closed: if the
source returns missing/invalid data, the script exits non-zero instead of
writing zeroes or fabricating a quote.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "indices.json"
DATA_DIR = ROOT / "data" / "indices"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_DIR = DATA_DIR / "history"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

CST = timezone(timedelta(hours=8))
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
PUSH2_HOSTS = ("push2delay.eastmoney.com", "push2.eastmoney.com")
PUSH2HIS_HOSTS = ("push2his.eastmoney.com", "push2delay.eastmoney.com")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
}


def _num(v: Any) -> float | None:
    if v in (None, "", "-"):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _round(v: float | None, n: int = 4) -> float | None:
    return None if v is None else round(v, n)


def _market_phase(now: datetime) -> str:
    if now.weekday() >= 5:
        return "closed"
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 15:
        return "preopen"
    if hm < 11 * 60 + 30:
        return "morning"
    if hm < 13 * 60:
        return "lunch"
    if hm < 15 * 60:
        return "afternoon"
    return "closed"


def _load_indices() -> list[dict[str, str]]:
    rows = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("indices.json must be a non-empty list")
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in rows:
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        secid = str(item.get("secid", "")).strip()
        if not (code and name and secid and "." in secid):
            raise RuntimeError(f"invalid index config: {item!r}")
        if code in seen:
            raise RuntimeError(f"duplicate index code: {code}")
        seen.add(code)
        out.append({"code": code, "name": name, "secid": secid})
    return out


def _get_json(hosts: tuple[str, ...], path: str, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    last_error: Exception | None = None
    trust_env = os.getenv("EASTMONEY_TRUST_ENV", "0") == "1"
    with httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True, trust_env=trust_env) as client:
        for attempt in range(3):
            for host in hosts:
                url = f"https://{host}{path}"
                try:
                    r = client.get(url, params=params)
                    r.raise_for_status()
                    payload = r.json()
                    if not isinstance(payload, dict):
                        raise RuntimeError("response is not a JSON object")
                    if payload.get("data") is None:
                        raise RuntimeError(f"Eastmoney returned empty data: {str(payload)[:200]}")
                    return payload, host
                except Exception as exc:  # noqa: BLE001 - preserve source failures
                    last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"all Eastmoney hosts failed: {last_error}")


def _normalise_diff(diff: Any) -> list[dict[str, Any]]:
    if isinstance(diff, list):
        return [x for x in diff if isinstance(x, dict)]
    if isinstance(diff, dict):
        return [x for x in diff.values() if isinstance(x, dict)]
    return []


def fetch_realtime(indices: list[dict[str, str]]) -> tuple[list[dict[str, Any]], str]:
    fields = "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f124"
    payload, host = _get_json(
        PUSH2_HOSTS,
        "/api/qt/ulist.np/get",
        {
            "fltt": "2",
            "invt": "2",
            "fields": fields,
            "secids": ",".join(x["secid"] for x in indices),
            "ut": EM_UT,
        },
    )
    rows = _normalise_diff((payload.get("data") or {}).get("diff"))
    by_code = {str(r.get("f12", "")): r for r in rows}

    out: list[dict[str, Any]] = []
    missing: list[str] = []
    for meta in indices:
        r = by_code.get(meta["code"])
        if not r:
            missing.append(meta["code"])
            continue
        price = _num(r.get("f2"))
        prev_close = _num(r.get("f18"))
        if price is None or price <= 0:
            raise RuntimeError(f"invalid realtime price for {meta['code']}: {r.get('f2')!r}")
        out.append(
            {
                **meta,
                "source_name": str(r.get("f14") or meta["name"]),
                "price": _round(price),
                "pct_change": _round(_num(r.get("f3"))),
                "change": _round(_num(r.get("f4"))),
                "open": _round(_num(r.get("f17"))),
                "high": _round(_num(r.get("f15"))),
                "low": _round(_num(r.get("f16"))),
                "prev_close": _round(prev_close),
                "volume": _num(r.get("f5")),
                "amount": _num(r.get("f6")),
                "source_epoch": int(r["f124"]) if str(r.get("f124", "")).isdigit() else None,
            }
        )

    if missing:
        raise RuntimeError(f"Eastmoney realtime response missing indices: {','.join(missing)}")
    return out, host


def parse_kline_line(line: str) -> dict[str, Any]:
    p = line.split(",")
    if len(p) < 10:
        raise ValueError(f"bad kline row: {line!r}")
    return {
        "date": p[0],
        "open": _num(p[1]),
        "close": _num(p[2]),
        "high": _num(p[3]),
        "low": _num(p[4]),
        "volume": _num(p[5]),
        "amount": _num(p[6]),
        "amplitude_pct": _num(p[7]),
        "pct_change": _num(p[8]),
        "change": _num(p[9]),
        "turnover_pct": _num(p[10]) if len(p) > 10 else None,
    }


def fetch_history(meta: dict[str, str], limit: int = 130) -> tuple[list[dict[str, Any]], str]:
    payload, host = _get_json(
        PUSH2HIS_HOSTS,
        "/api/qt/stock/kline/get",
        {
            "secid": meta["secid"],
            "klt": "101",
            "fqt": "0",
            "lmt": str(limit),
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "ut": EM_UT,
        },
    )
    lines = (payload.get("data") or {}).get("klines") or []
    if not isinstance(lines, list) or not lines:
        raise RuntimeError(f"empty kline history for {meta['code']}")
    bars = [parse_kline_line(str(x)) for x in lines]
    if not any(b.get("close") for b in bars):
        raise RuntimeError(f"invalid kline history for {meta['code']}")
    return bars, host


def _rolling_high(bars: list[dict[str, Any]], realtime_high: float | None, days: int = 120) -> tuple[float | None, str | None]:
    sample = bars[-days:]
    candidates: list[tuple[float, str]] = []
    for b in sample:
        h = _num(b.get("high"))
        if h is not None:
            candidates.append((h, str(b.get("date") or "")))
    if realtime_high is not None:
        candidates.append((realtime_high, datetime.now(CST).date().isoformat()))
    if not candidates:
        return None, None
    return max(candidates, key=lambda x: x[0])


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    indices = _load_indices()
    now = datetime.now(CST)
    checked_at = now.isoformat(timespec="seconds")

    realtime, realtime_host = fetch_realtime(indices)
    latest_items: list[dict[str, Any]] = []
    history_hosts: dict[str, str] = {}

    for quote in realtime:
        meta = {"code": quote["code"], "name": quote["name"], "secid": quote["secid"]}
        bars, hist_host = fetch_history(meta)
        history_hosts[quote["code"]] = hist_host
        high_120d, high_date = _rolling_high(bars, _num(quote.get("high")), 120)
        price = _num(quote.get("price"))
        drawdown = None
        if high_120d and price is not None and high_120d > 0:
            drawdown = (price / high_120d - 1.0) * 100.0

        item = {
            **quote,
            "high_120d": _round(high_120d),
            "high_120d_date": high_date,
            "drawdown_from_120d_high_pct": _round(drawdown, 3),
        }
        latest_items.append(item)

        _atomic_write_json(
            HISTORY_DIR / f"{quote['code']}.json",
            {
                "code": quote["code"],
                "name": quote["name"],
                "secid": quote["secid"],
                "source": "eastmoney:push2his",
                "source_host": hist_host,
                "checked_at": checked_at,
                "bars": bars,
            },
        )

    payload = {
        "source": "eastmoney:push2",
        "source_host": realtime_host,
        "checked_at": checked_at,
        "timezone": "Asia/Shanghai",
        "market_phase": _market_phase(now),
        "indices": latest_items,
        "history_hosts": history_hosts,
    }
    _atomic_write_json(LATEST_PATH, payload)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOT_DIR / f"{now.date().isoformat()}.jsonl"
    with snapshot_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
