import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cloudcost.config import load_config
from cloudcost.db import Database
from cloudcost.models import Bill, current_month, money, month_key, next_month, utcnow
from cloudcost.service import check, collect_all, summary
from cloudcost.cli import main
from cloudcost.cli import run_loop


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        self.path.write_text('''[monitor]
database = "cost.sqlite3"
stale_after_hours = 26
[fx]
USD = "1"
CNY = "7.2"
[budget]
total = "100"
[[providers]]
id = "aws"
kind = "aws"
mode = "feed"
currency = "USD"
threshold = "50"
path = "aws.json"
[[providers]]
id = "aliyun"
kind = "aliyun"
mode = "feed"
threshold = "360"
path = "aliyun.json"
[[channels]]
name = "test"
type = "webhook"
url_env = "TEST_WEBHOOK"
''')
        self.config = load_config(self.path)
        self.db = Database(self.config.database)
        self.month = current_month()

    def save(self, name, amount, complete=True, timestamp=None):
        currency = self.config.provider(name)["currency"]
        self.db.save(name, Bill(self.month, money(amount), currency, "test", complete, timestamp or utcnow()), self.config)

    def seed(self, aws="60", aliyun="432"):
        self.save("aws", aws)
        self.save("aliyun", aliyun)


class MoneyTests(unittest.TestCase):
    def test_exact_decimal_and_invalid_values(self):
        self.assertEqual(money("0.1") + money("0.2"), Decimal("0.3"))
        for value in ["NaN", "Infinity", "bad", None]:
            with self.subTest(value=value), self.assertRaises(ValueError): money(value)

    def test_month_validation_and_rollover(self):
        self.assertEqual(next_month("2026-12"), "2027-01")
        for value in ["2026-13", "2026-1", "0000-01", "x"]:
            with self.assertRaises(ValueError): month_key(value)

    def test_only_unbilled_snapshots(self):
        with self.assertRaises(ValueError):
            Bill(current_month(), money(1), "USD", "test", basis="invoiced")

    def test_timezone_normalization(self):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=1)
        local = stamp.astimezone(timezone(timedelta(hours=8))).isoformat()
        bill = Bill(current_month(), money(1), "USD", "test", observed_at=local)
        self.assertEqual(bill.observed_at, stamp.isoformat(timespec="microseconds"))


class BudgetTests(Fixture):
    def test_latest_snapshot_not_sum(self):
        self.save("aws", "10")
        self.seed()
        data = summary(self.config, self.db, self.month)
        self.assertEqual(money(data["total_usd"]), 120)
        self.assertTrue(data["complete"])
        self.assertEqual(self.config.convert("7.2", "CNY"), 1)
        self.assertEqual(str(self.config.convert("1288", "CNY", "CNY")), "1288")

    def test_missing_blocks_total_and_preserves_individual(self):
        self.save("aws", "60")
        data = check(self.config, self.db, self.month, True)
        self.assertEqual([a["scope"] for a in data["detected"]], ["aws"])
        self.assertIn("total", [a["scope"] for a in data["skipped"]])

    def test_partial_coverage_blocks_alerts(self):
        self.seed()
        self.save("aliyun", "500", complete=False)
        result = check(self.config, self.db, self.month, True)
        self.assertEqual([a["scope"] for a in result["detected"]], ["aws"])

    def test_stale_uses_observation_time(self):
        self.seed()
        self.config.stale_after_hours = Decimal("0.00000001")
        self.assertFalse(summary(self.config, self.db, self.month)["complete"])

    def test_strict_greater_than(self):
        self.seed("50", "360")
        self.assertEqual(check(self.config, self.db, self.month, True)["detected"], [])

    def test_cross_currency_individual_threshold(self):
        self.config.provider("aliyun")["threshold_currency"] = "USD"
        self.config.provider("aliyun")["threshold"] = Decimal(50)
        self.seed()
        candidates = check(self.config, self.db, self.month, True)["detected"]
        self.assertEqual(money(next(a for a in candidates if a["scope"] == "aliyun")["amount"]), 60)

    def test_total_budget_in_cny(self):
        self.config.total_currency = "CNY"
        self.config.total_threshold = Decimal(800)
        self.seed()
        total = next(a for a in check(self.config, self.db, self.month, True)["detected"] if a["scope"] == "total")
        self.assertEqual(money(total["amount"]), Decimal(864))

    def test_dry_run_has_no_delivery_or_state_mutation(self):
        self.seed()
        with patch("cloudcost.notify.send") as send:
            self.assertEqual(len(check(self.config, self.db, self.month, True)["detected"]), 3)
            send.assert_not_called()
        self.assertEqual(self.db.alerts(), [])

    def test_idempotence_and_retry_failed_channel_only(self):
        self.seed()
        self.config.channels.append({"name": "second", "type": "console"})
        fail_once = {("test", "total")}
        def send(channel, alert):
            key = (channel["name"], alert["scope"])
            if key in fail_once:
                fail_once.remove(key)
                raise OSError("secret URL must not leak")
        with patch("cloudcost.notify.send", side_effect=send):
            first = check(self.config, self.db, self.month)
            second = check(self.config, self.db, self.month)
            third = check(self.config, self.db, self.month)
        self.assertEqual(len(first["deliveries"]), 6)
        self.assertEqual(len(second["deliveries"]), 1)
        self.assertEqual(third["deliveries"], [])
        self.assertNotIn("secret URL", json.dumps(first))
        total = next(a for a in self.db.alerts() if a["scope"] == "total")
        self.assertEqual({d["channel"]: d["attempts"] for d in total["deliveries"]}, {"test": 2, "second": 1})

    def test_concurrent_claim_has_single_winner(self):
        alert = self.db.alert("total", self.month, Decimal(120), Decimal(100), "USD", "test", self.config.channels)
        with ThreadPoolExecutor(max_workers=4) as pool:
            winners = list(pool.map(lambda _: self.db.claim(alert["id"], "test"), range(4)))
        self.assertEqual(sum(winners), 1)

    def test_changed_threshold_creates_new_alert(self):
        self.seed()
        with patch("cloudcost.notify.send"):
            check(self.config, self.db, self.month)
            self.config.total_threshold = Decimal(110)
            check(self.config, self.db, self.month)
        self.assertEqual(len(self.db.alerts()), 4)

    def test_failed_collection_retains_amount_without_leaking_secrets(self):
        self.seed()
        with patch("cloudcost.providers.collect", side_effect=OSError("private token")):
            result = collect_all(self.config, self.db, self.month)
        self.assertFalse(any(r["ok"] for r in result))
        self.assertEqual(money(summary(self.config, self.db, self.month)["total_usd"]), 120)
        self.assertNotIn("private token", json.dumps(self.db.collections()))

    def test_demo_blocks_external_notifications(self):
        self.config.demo = True
        self.seed()
        with patch("cloudcost.notify.send") as send:
            check(self.config, self.db, self.month)
            send.assert_not_called()

    def test_cli_output_machine_readable(self):
        self.seed()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["check", "-c", str(self.path), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.getvalue())["detected"]), 3)

    def test_daemon_recomputes_month_on_rollover(self):
        from unittest.mock import Mock
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch("cloudcost.cli.current_month", side_effect=["2026-12", "2027-01"]), patch("cloudcost.cli.collect_all") as collector, patch("cloudcost.cli.check") as checker:
            run_loop(self.config, self.db, stop, True)
        self.assertEqual([call.args[2] for call in collector.call_args_list], ["2026-12", "2027-01"])
        self.assertEqual([call.args[2] for call in checker.call_args_list], ["2026-12", "2027-01"])


class ConfigTests(Fixture):
    def test_invalid_fx_and_duplicate_provider_rejected(self):
        text = self.path.read_text()
        self.path.write_text(text.replace('CNY = "7.2"', 'CNY = "0"'))
        with self.assertRaises(ValueError): load_config(self.path)
        self.path.write_text(text + '\n[[providers]]\nid="aws"\nkind="aws"\n')
        with self.assertRaises(ValueError): load_config(self.path)

    def test_currency_defaults_and_relative_database(self):
        self.assertEqual(self.config.provider("aliyun")["currency"], "CNY")
        self.assertEqual(self.config.database, self.root / "cost.sqlite3")

    def test_invoice_native_sources_are_not_available(self):
        for kind in ["vercel", "cloudflare"]:
            self.path.write_text(f'[[providers]]\nid="{kind}"\nmode="native"\n')
            with self.assertRaises(ValueError): load_config(self.path)

    def test_total_scope_is_reserved(self):
        self.path.write_text('[[providers]]\nid="total"\nkind="aws"\n')
        with self.assertRaises(ValueError): load_config(self.path)


if __name__ == "__main__": unittest.main()
