from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
from .config import load_config
from .db import Database
from .models import Bill, utcnow

VALUES = [("aws", "USD", "486.40", "450"), ("vercel", "USD", "118.90", "150"),
          ("cloudflare", "USD", "43.60", "60"), ("aliyun", "CNY", "1288", "1100"),
          ("alibabacloud", "USD", "82.80", "120")]


def initialize(path: Path):
    text = '''[monitor]
database = "demo.sqlite3"
interval_seconds = 3600
stale_after_hours = 26
demo = true
[fx]
USD = "1"
CNY = "7.2"
[budget]
total = "900"
currency = "USD"
'''
    for name, currency, amount, threshold in VALUES:
        text += f'''\n[[providers]]
id = "{name}"
kind = "{name}"
mode = "feed"
currency = "{currency}"
threshold = "{threshold}"
path = "demo-feeds/{name}.json"
'''
    text += '\n[[channels]]\nname = "terminal"\ntype = "console"\n'
    path.write_text(text, encoding="utf-8")
    config = load_config(path)
    db = Database(config.database)
    feed_dir = path.parent / "demo-feeds"
    feed_dir.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    # Archive six months of MTD snapshots, never finalized invoices.
    for offset in reversed(range(6)):
        month_index = now.year * 12 + now.month - 1 - offset
        year, m0 = divmod(month_index, 12)
        month = f"{year:04d}-{m0 + 1:02d}"
        max_day = now.day if offset == 0 else 25
        for day in range(1, max_day + 1):
            stamp = datetime(year, m0 + 1, day, tzinfo=timezone.utc)
            if offset == 0 and day == now.day:
                stamp = now - timedelta(seconds=1)
            for name, currency, amount, _ in VALUES:
                factor = Decimal(day) / max_day * (Decimal(1) - Decimal(offset) * Decimal("0.06"))
                bill = Bill(month, (Decimal(amount) * factor).quantize(Decimal("0.01")), currency,
                            "demo:unbilled_mtd", observed_at=stamp.isoformat())
                db.save(name, bill, config)
    for name, currency, amount, _ in VALUES:
        row = {"month": now.strftime("%Y-%m"), "amount": amount, "currency": currency,
               "basis": "unbilled_mtd", "complete": True, "observed_at": utcnow()}
        (feed_dir / f"{name}.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
    return config
