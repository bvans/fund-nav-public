#!/usr/bin/env python3
"""Fetch mainland China index quotes from Eastmoney push2 APIs.

Primary use case: GitHub Actions creates a small, auditable market-data cache
that can be read directly by monitoring/analysis tools without scraping pages.

Data sources:
- Realtime snapshot (authoritative quote): Eastmoney /api/qt/ulist.np/get
- Daily K-line for drawdown context: Eastmoney push2his, with Tencent index
  K-line fallback when push2his is unavailable from GitHub-hosted runners.

The endpoint/field mapping follows the same public interfaces used by open-source
projects such as AKShare. Realtime quotes fail closed: missing/invalid Eastmoney
data never gets replaced by a different realtime source or fabricated values.
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
_EM_HISTORY_DISABLED = False

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
        history_secid = str(item.get("history_secid") or secid).strip()
        if not (code and name and secid and "." in secid and "." in history_secid):
            raise RuntimeError(f"invalid index config: {item!r}")
        if code in seen:
            raise RuntimeError(f"duplicate index code: {code}")
        seen.add(code)
        out.append({"code": code, "name": name, "secid": secid, "history_secid": history_secid})
    return out


def _get_json(
    hosts: tuple[str, ...],
    path: str,
    params: dict[str, Any],
    attempts: int = 3,
) -> tuple[dict[str, Any], str]:
    last_error: Exception | None = None
    trust_env = os.getenv("EASTMONEY_TRUST_ENV", "0") == "1"
    with httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True, trust_env=trust_env) as client:
        for attempt in range(attempts):
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
            if attempt < attempts - 1:
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
        source_epoch = int(r["f124"]) if str(r.get("f124", "")).isdigit() else None
        source_time = (
            datetime.fromtimestamp(source_epoch, CST).isoformat(timespec="seconds")
            if source_epoch
            else None
        )
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
                "source_epoch": source_epoch,
                "source_time": source_time,
                "source_date": source_time[:10] if source_time else None,
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


def fetch_history_eastmoney(meta: dict[str, str], limit: int = 130) -> tuple[list[dict[str, Any]], str]:
    payload, host = _get_json(
        PUSH2HIS_HOSTS,
        "/api/qt/stock/kline/get",
        {
            "secid": meta.get("history_secid") or meta["secid"],
            "klt": "101",
            "fqt": "0",
            "lmt": str(limit),
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "ut": EM_UT,
        },
        attempts=1,
    )
    lines = (payload.get("data") or {}).get("klines") or []
    if not isinstance(lines, list) or not lines:
        raise RuntimeError(f"empty Eastmoney kline history for {meta['code']}")
    bars = [parse_kline_line(str(x)) for x in lines]
    if not any(b.get("close") for b in bars):
        raise RuntimeError(f"invalid Eastmoney kline history for {meta['code']}")
    return bars, host


def _tencent_symbol(code: str) -> str:
    # Official mainland indices in this config follow the conventional Tencent
    # symbols also used by AKShare: sh000852 / sh000510 / sz399006, etc.
    return ("sz" if code.startswith("399") else "sh") + code


def parse_tencent_kline_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, (list, tuple)) or len(row) < 5:
        raise ValueError(f"bad Tencent kline row: {row!r}")
    close = _num(row[2])
    if close is None or close <= 0:
        raise ValueError(f"bad Tencent kline close: {row!r}")
    # Tencent index rows vary by endpoint/version. OHLC positions are stable.
    # Index 5 is volume in commonly returned rows; index 8, when present, is
    # turnover amount in ten-thousand yuan. Neither is needed for drawdown.
    amount = _num(row[8]) if len(row) > 8 else None
    return {
        "date": str(row[0])[:10],
        "open": _num(row[1]),
        "close": close,
        "high": _num(row[3]),
        "low": _num(row[4]),
        "volume": _num(row[5]) if len(row) > 5 else None,
        "amount": amount * 10000 if amount is not None else None,
        "amplitude_pct": None,
        "pct_change": None,
        "change": None,
        "turnover_pct": None,
    }


def fetch_history_tencent(meta: dict[str, str], limit: int = 130) -> tuple[list[dict[str, Any]], str]:
    symbol = _tencent_symbol(meta["code"])
    hosts = (
        "proxy.finance.qq.com",
        "web.ifzq.gtimg.cn",
    )
    last_error: Exception | None = None
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://gu.qq.com/",
        "Accept": "application/json,text/plain,*/*",
    }
    with httpx.Client(headers=headers, timeout=10.0, follow_redirects=True, trust_env=False) as client:
        for host in hosts:
            url = f"https://{host}/ifzqgtimg/appstock/app/newfqkline/get"
            try:
                r = client.get(url, params={"param": f"{symbol},day,,,{max(limit, 1)},"})
                r.raise_for_status()
                payload = r.json()
                node = ((payload.get("data") or {}).get(symbol) or {})
                rows = node.get("day") or node.get("qfqday") or []
                bars = []
                for row in rows:
                    try:
                        bars.append(parse_tencent_kline_row(row))
                    except ValueError:
                        continue
                if not bars:
                    raise RuntimeError(f"empty Tencent kline history for {meta['code']}")
                return bars[-limit:], host
            except Exception as exc:  # noqa: BLE001
                last_error = exc
    raise RuntimeError(f"all Tencent history hosts failed: {last_error}")


def fetch_history(meta: dict[str, str], limit: int = 130) -> tuple[list[dict[str, Any]], str, str, str | None]:
    global _EM_HISTORY_DISABLED
    em_error: str | None = None

    # Circuit breaker: GitHub-hosted runners may be rejected by push2his.
    # Probe it once per job; after the first failure use Tencent for the rest
    # instead of multiplying timeouts across every tracked index.
    if not _EM_HISTORY_DISABLED:
        try:
            bars, host = fetch_history_eastmoney(meta, limit=limit)
            return bars, "eastmoney:push2his", host, None
        except Exception as exc:  # noqa: BLE001
            em_error = str(exc)[:500]
            _EM_HISTORY_DISABLED = True
    else:
        em_error = "Eastmoney push2his disabled after an earlier failure in this run"

    bars, host = fetch_history_tencent(meta, limit=limit)
    return bars, "tencent:newfqkline", host, em_error


def _rolling_high(
    bars: list[dict[str, Any]],
    realtime_high: float | None,
    days: int = 120,
) -> tuple[float | None, str | None]:
    sample = bars[-days:]
    candidates: list[tuple[float, str]] = []
    for b in sample:
        h = _num(b.get("high"))
        if h is not None:
            candidates.append((h, str(b.get("date") or "")))

    # Never claim today's high is a "120-day high" when history is unavailable.
    if not candidates:
        return None, None

    if realtime_high is not None:
        candidates.append((realtime_high, datetime.now(CST).date().isoformat()))
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
    history_errors: dict[str, str] = {}

    for quote in realtime:
        config_meta = next(x for x in indices if x["code"] == quote["code"])
        meta = {
            "code": quote["code"],
            "name": quote["name"],
            "secid": quote["secid"],
            "history_secid": config_meta["history_secid"],
        }

        bars: list[dict[str, Any]] = []
        hist_host: str | None = None
        hist_source: str | None = None
        history_status = "fresh"
        history_note: str | None = None
        try:
            bars, hist_source, hist_host, em_error = fetch_history(meta)
            history_hosts[quote["code"]] = hist_host
            if em_error:
                history_note = "Eastmoney history unavailable; Tencent fallback used"
                history_errors[quote["code"]] = em_error
            _atomic_write_json(
                HISTORY_DIR / f"{quote['code']}.json",
                {
                    "code": quote["code"],
                    "name": quote["name"],
                    "secid": quote["secid"],
                    "history_secid": meta["history_secid"],
                    "source": hist_source,
                    "source_host": hist_host,
                    "checked_at": checked_at,
                    "bars": bars,
                },
            )
        except Exception as exc:  # history is context only; realtime remains Eastmoney-authoritative
            history_errors[quote["code"]] = str(exc)[:500]
            cached_path = HISTORY_DIR / f"{quote['code']}.json"
            if cached_path.exists():
                try:
                    cached = json.loads(cached_path.read_text(encoding="utf-8"))
                    bars = cached.get("bars") or []
                    history_status = "cached"
                    hist_source = str(cached.get("source") or "cached")
                    hist_host = str(cached.get("source_host") or "cached")
                    history_hosts[quote["code"]] = hist_host
                except Exception:
                    history_status = "unavailable"
            else:
                history_status = "unavailable"

        high_120d, high_date = _rolling_high(bars, _num(quote.get("high")), 120)
        price = _num(quote.get("price"))
        drawdown = None
        if high_120d and price is not None and high_120d > 0:
            drawdown = (price / high_120d - 1.0) * 100.0

        item = {
            **quote,
            "history_status": history_status,
            "history_source": hist_source,
            "history_host": hist_host,
            "history_note": history_note,
            "high_120d": _round(high_120d),
            "high_120d_date": high_date,
            "drawdown_from_120d_high_pct": _round(drawdown, 3),
        }
        latest_items.append(item)

    payload = {
        "source": "eastmoney:push2",
        "source_host": realtime_host,
        "checked_at": checked_at,
        "timezone": "Asia/Shanghai",
        "market_phase": _market_phase(now),
        "indices": latest_items,
        "history_hosts": history_hosts,
        "history_errors": history_errors,
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
