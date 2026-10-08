from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
from .models import Bill, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, provider TEXT NOT NULL, month TEXT NOT NULL,
 amount TEXT NOT NULL, currency TEXT NOT NULL, amount_usd TEXT NOT NULL,
 fx_rate TEXT NOT NULL, captured_at TEXT NOT NULL, collected_at TEXT NOT NULL,
 source TEXT NOT NULL, complete INTEGER NOT NULL, basis TEXT NOT NULL DEFAULT 'unbilled_mtd'
);
CREATE INDEX IF NOT EXISTS snapshots_latest ON snapshots(month, provider, captured_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS collections (
 id INTEGER PRIMARY KEY, provider TEXT NOT NULL, month TEXT NOT NULL,
 collected_at TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, month TEXT NOT NULL,
 amount TEXT NOT NULL, threshold TEXT NOT NULL, currency TEXT NOT NULL,
 created_at TEXT NOT NULL, message TEXT NOT NULL, basis TEXT NOT NULL DEFAULT 'unbilled_mtd',
 UNIQUE(scope, month, threshold, currency)
);
CREATE TABLE IF NOT EXISTS deliveries (
 alert_id INTEGER NOT NULL REFERENCES alerts(id), channel TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL, error TEXT,
 PRIMARY KEY(alert_id, channel)
);
"""


class Database:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            # Upgrade existing installations without dropping snapshots or deliveries.
            db.execute("BEGIN IMMEDIATE")
            if "basis" not in {r["name"] for r in db.execute("PRAGMA table_info(alerts)")}:
                db.execute("ALTER TABLE alerts ADD COLUMN basis TEXT NOT NULL DEFAULT 'unbilled_mtd'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, provider: str, bill: Bill, config):
        now = utcnow()
        with self.connect() as db:
            db.execute("""INSERT INTO snapshots(provider,month,amount,currency,amount_usd,fx_rate,
                       captured_at,collected_at,source,complete,basis) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (provider, bill.month, str(bill.amount), bill.currency,
                        str(config.convert(bill.amount, bill.currency)), str(config.fx[bill.currency]),
                        bill.observed_at or now, now, bill.source, int(bill.complete), bill.basis))

    def record_collection(self, provider, month, ok, error=None):
        with self.connect() as db:
            db.execute("INSERT INTO collections(provider,month,collected_at,ok,error) VALUES(?,?,?,?,?)",
                       (provider, month, utcnow(), int(ok), error))

    def latest(self, month):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT * FROM (
              SELECT *, ROW_NUMBER() OVER (PARTITION BY provider ORDER BY captured_at DESC,id DESC) AS rank
              FROM snapshots WHERE month=?) WHERE rank=1 ORDER BY provider""", (month,))]

    def history(self, month, provider=None, limit=500):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT * FROM snapshots WHERE month=?
                       AND (? IS NULL OR provider=?) ORDER BY captured_at DESC,id DESC LIMIT ?""",
                       (month, provider, provider, limit))]

    def months(self):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT month FROM snapshots ORDER BY month DESC")]

    def collections(self, limit=50):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM collections ORDER BY id DESC LIMIT ?", (limit,))]

    def alert(self, scope, month, amount, threshold, currency, message, channels, basis="unbilled_mtd"):
        # IMMEDIATE serializes alert creation across CLI/daemon processes.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO alerts(scope,month,amount,threshold,currency,created_at,message,basis)
                       VALUES(?,?,?,?,?,?,?,?)""", (scope, month, str(amount), str(threshold.normalize()), currency, utcnow(), message, basis))
            row = dict(db.execute("SELECT * FROM alerts WHERE scope=? AND month=? AND threshold=? AND currency=?",
                                  (scope, month, str(threshold.normalize()), currency)).fetchone())
            for channel in channels:
                db.execute("INSERT OR IGNORE INTO deliveries(alert_id,channel,updated_at) VALUES(?,?,?)",
                           (row["id"], channel["name"], utcnow()))
            return row

    def claim(self, alert_id, channel):
        lease_expiry = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(timespec="microseconds")
        with self.connect() as db:
            result = db.execute("""UPDATE deliveries SET status='sending',attempts=attempts+1,updated_at=?,error=NULL
                    WHERE alert_id=? AND channel=? AND (status IN ('pending','failed')
                    OR (status='sending' AND updated_at<?))""", (utcnow(), alert_id, channel, lease_expiry))
            return result.rowcount == 1

    def finish(self, alert_id, channel, error=None):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status=?,updated_at=?,error=? WHERE alert_id=? AND channel=?",
                       ("failed" if error else "sent", utcnow(), error, alert_id, channel))

    def alerts(self, month=None, limit=100):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM alerts WHERE (? IS NULL OR month=?) ORDER BY id DESC LIMIT ?",
                                                (month, month, limit))]
            for row in rows:
                row["deliveries"] = [dict(r) for r in db.execute("SELECT channel,status,attempts,updated_at,error FROM deliveries WHERE alert_id=?", (row["id"],))]
            return rows
