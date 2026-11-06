#!/usr/bin/env python3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
PATH = Path("data/indices/latest.json")


def main():
    data = json.loads(PATH.read_text(encoding="utf-8"))
    if data.get("source") != "eastmoney:push2":
        raise SystemExit("invalid source: expected eastmoney:push2")

    row = next((x for x in data.get("indices", []) if x.get("code") == "000852"), None)
    if not row:
        raise SystemExit("missing CSI1000 quote")

    price = row.get("price")
    pct = row.get("pct_change")
    source_time = row.get("source_time")
    source_date = row.get("source_date")
    if price is None or pct is None or not source_time or not source_date:
        raise SystemExit("missing required quote fields")

    now = datetime.now(CST)
    t = datetime.fromisoformat(source_time)
    if source_date != now.date().isoformat():
        raise SystemExit(f"stale quote date: {source_date}, today={now.date().isoformat()}")

    age = (now - t).total_seconds()
    hm = now.hour * 60 + now.minute

    # Intraday runs should be reasonably fresh.
    if 9 * 60 <= hm < 15 * 60 + 30:
        if age < -300 or age > 3600:
            raise SystemExit(f"intraday quote too old: age={age:.0f}s")
    else:
        # After close, same-day final quote is valid even if GitHub's scheduled
        # runner starts hours late. Require the source itself to be post-close.
        source_hm = t.hour * 60 + t.minute
        if source_hm < 15 * 60:
            raise SystemExit(f"after-close run has non-close quote: {source_time}")

    print(
        f"Verified CSI1000 price={price}, pct={pct}, "
        f"source_time={source_time}, checked_at={data.get('checked_at')}"
    )


if __name__ == "__main__":
    main()
