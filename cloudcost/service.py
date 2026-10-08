from datetime import datetime, timezone
from decimal import Decimal
import logging
from . import notify, providers
from .config import MissingEnvironmentError
from .models import BASES, money, utcnow

log = logging.getLogger("cloudcost")


def safe_error(error):
    if isinstance(error, (providers.ProviderError, notify.NotifyError, MissingEnvironmentError)):
        return str(error)
    if isinstance(error, FileNotFoundError):
        return "费用数据文件不存在"
    if isinstance(error, ValueError):
        # Parse/SDK exceptions can contain input values; avoid propagating secrets.
        return "配置或费用格式无效；检查金额、时区、完整性和环境变量配置"
    return f"操作失败（{type(error).__name__}）；详细响应已隐藏"


def collect_all(config, db, month):
    results = []
    for provider in config.enabled:
        try:
            bill = providers.collect(provider, config, month)
            db.save(provider["id"], bill, config)
            db.record_collection(provider["id"], month, True)
            results.append({"provider": provider["id"], "ok": True, "amount": str(bill.amount), "currency": bill.currency,
                            "basis": bill.basis, "complete": bill.complete})
        except Exception as error:
            text = safe_error(error)
            db.record_collection(provider["id"], month, False, text)
            results.append({"provider": provider["id"], "ok": False, "error": text})
            log.warning("%s: %s", provider["id"], text)
    return results


def summary(config, db, month):
    latest = {r["provider"]: r for r in db.latest(month)}
    result, total = [], Decimal(0)
    for provider in config.enabled:
        row = latest.get(provider["id"])
        base = {"id": provider["id"], "kind": provider["kind"], "currency": provider["currency"],
                "threshold": str(provider["threshold"]) if "threshold" in provider else None,
                "threshold_currency": provider["threshold_currency"], "mode": provider["mode"]}
        if not row:
            result.append({**base, "status": "missing", "amount": None, "amount_usd": None, "complete": False,
                           "captured_at": None, "source": None, "budget_percent": None, "basis": None})
            continue
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["captured_at"])).total_seconds()
        # Archived MTD snapshots remain queryable without pretending to be current data.
        stale = age > float(config.stale_after_hours * 3600)
        valid_currency = row["currency"] == provider["currency"]
        status = "currency_mismatch" if not valid_currency else "stale" if stale else "partial" if not row["complete"] else "ok"
        amount = money(row["amount"])
        usd = config.convert(amount, row["currency"])
        total += usd
        comparable = config.convert(amount, row["currency"], provider["threshold_currency"])
        percent = comparable / provider["threshold"] * 100 if provider.get("threshold", 0) > 0 else None
        result.append({**base, "currency": row["currency"], "amount": str(amount), "amount_usd": str(usd),
                       "complete": bool(row["complete"]), "status": status, "captured_at": row["captured_at"],
                       "source": row["source"], "budget_percent": str(percent) if percent is not None else None,
                       "basis": row["basis"]})
    complete = bool(result) and all(p["status"] == "ok" for p in result)
    threshold_usd = config.convert(config.total_threshold, config.total_currency) if config.total_threshold is not None else None
    bases = {p["basis"] for p in result if p["basis"]}
    basis = next(iter(bases)) if len(bases) == 1 else "mixed_mtd" if bases else None
    return {"month": month, "basis": basis, "demo": config.demo, "providers": result,
            "total_usd": str(total), "complete": complete,
            "total_threshold": str(config.total_threshold) if config.total_threshold is not None else None,
            "total_threshold_currency": config.total_currency,
            "total_threshold_usd": str(threshold_usd) if threshold_usd is not None else None,
            "budget_percent": str(total / threshold_usd * 100) if threshold_usd and threshold_usd > 0 else None,
            "fx": {k: str(v) for k, v in config.fx.items()}, "as_of": utcnow()}


def check(config, db, month, dry_run=False):
    data, candidates, skipped = summary(config, db, month), [], []
    for row in data["providers"]:
        if row["threshold"] is None:
            continue
        if row["status"] != "ok":
            skipped.append({"scope": row["id"], "reason": row["status"]})
            continue
        amount = config.convert(row["amount"], row["currency"], row["threshold_currency"])
        if amount > money(row["threshold"]):
            candidates.append((row["id"], amount, money(row["threshold"]), row["threshold_currency"], row["basis"]))
    if config.total_threshold is not None:
        if not data["complete"]:
            skipped.append({"scope": "total", "reason": "数据缺失、过期或覆盖不完整"})
        else:
            amount = config.convert(data["total_usd"], "USD", config.total_currency)
            if amount > config.total_threshold:
                candidates.append(("total", amount, config.total_threshold, config.total_currency, data["basis"]))
    detected, deliveries = [], []
    for scope, amount, threshold, currency, basis in candidates:
        message = f"CloudCost 预算报警｜{month} {BASES[basis]}｜{scope}：{amount:.2f} {currency} > {threshold:.2f} {currency}"
        detected.append({"scope": scope, "month": month, "amount": str(amount), "threshold": str(threshold), "currency": currency, "message": message, "basis": basis})
        if dry_run:
            continue
        # Demonstration datasets must never trigger a real external notification.
        channels = [c for c in config.channels if not config.demo or c["type"] == "console"]
        alert = db.alert(scope, month, amount, threshold, currency, message, channels, basis)
        for channel in channels:
            if not db.claim(alert["id"], channel["name"]):
                continue
            try:
                notify.send(channel, alert)
                db.finish(alert["id"], channel["name"])
                deliveries.append({"scope": scope, "channel": channel["name"], "ok": True})
            except Exception as error:
                text = safe_error(error)
                db.finish(alert["id"], channel["name"], text)
                deliveries.append({"scope": scope, "channel": channel["name"], "ok": False, "error": text})
                log.warning("%s/%s: %s", scope, channel["name"], text)
    return {"detected": detected, "deliveries": deliveries, "skipped": skipped, "dry_run": dry_run}
