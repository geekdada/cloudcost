from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import os
import re
import tomllib
from .models import money

KINDS = {"aws", "vercel", "cloudflare", "aliyun", "alibabacloud"}
CHANNELS = {"console", "webhook", "slack", "discord", "telegram", "dingtalk", "feishu", "wecom", "email", "ntfy"}


class MissingEnvironmentError(ValueError):
    """A missing variable name is safe to display; its value never is."""


@dataclass
class Config:
    path: Path
    database: Path
    interval_seconds: int
    stale_after_hours: Decimal
    fx: dict[str, Decimal]
    total_threshold: Decimal | None
    total_currency: str
    providers: list[dict]
    channels: list[dict]
    demo: bool = False

    def convert(self, amount, source: str, target: str = "USD") -> Decimal:
        if source == target:
            return money(amount)
        return money(amount) / self.fx[source] * self.fx[target]

    def provider(self, name: str) -> dict:
        for provider in self.providers:
            if provider["id"] == name:
                return provider
        raise ValueError(f"未配置供应商：{name}")

    @property
    def enabled(self):
        return [p for p in self.providers if p["enabled"]]


def env_value(name: str | None, required: bool = True) -> str | None:
    if name and (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)):
        raise ValueError("环境变量名格式无效")
    value = os.environ.get(name) if name else None
    if required and not value:
        raise MissingEnvironmentError(f"缺少环境变量：{name or '(未配置变量名)'}")
    return value


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    monitor, budget = raw.get("monitor", {}), raw.get("budget", {})
    fx = {k: money(v) for k, v in raw.get("fx", {"USD": "1", "CNY": "7.2"}).items()}
    if set(fx) != {"USD", "CNY"} or fx["USD"] != 1 or any(v <= 0 for v in fx.values()):
        raise ValueError("fx 必须含 USD=1 和正数 CNY（1 USD 对应的人民币）")
    total_currency = budget.get("currency", "USD")
    if total_currency not in fx:
        raise ValueError("总预算币种仅支持 USD/CNY")
    total_threshold = money(budget["total"]) if "total" in budget else None
    if total_threshold is not None and total_threshold < 0:
        raise ValueError("总预算不能小于零")
    providers, ids = [], set()
    for entry in raw.get("providers", []):
        p = dict(entry)
        kind = p.get("kind", p.get("id"))
        name = p.get("id", kind)
        if kind not in KINDS or not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name in ids or name == "total":
            raise ValueError("供应商 kind 无效或 id 重复/无效")
        ids.add(name)
        p.update(id=name, kind=kind, currency=p.get("currency", "CNY" if kind == "aliyun" else "USD"), enabled=p.get("enabled", True))
        if not isinstance(p["enabled"], bool):
            raise ValueError("enabled 必须是布尔值")
        if p["currency"] not in fx:
            raise ValueError(f"{name} 的币种无效")
        p["threshold_currency"] = p.get("threshold_currency", p["currency"])
        if p["threshold_currency"] not in fx:
            raise ValueError(f"{name} 的阈值币种无效")
        if "threshold" in p:
            p["threshold"] = money(p["threshold"])
            if p["threshold"] < 0:
                raise ValueError(f"{name} 的阈值不能小于零")
        p["mode"] = p.get("mode", "native")
        if p["mode"] not in {"native", "feed"}:
            raise ValueError(f"{name} mode 必须为 native/feed")
        if "fixed_monthly_cost" in p:
            if kind != "cloudflare" or p["mode"] != "native":
                raise ValueError("fixed_monthly_cost 仅用于 Cloudflare 原生采集")
            p["fixed_monthly_cost"] = money(p["fixed_monthly_cost"])
            if p["fixed_monthly_cost"] < 0:
                raise ValueError("固定月费不能小于零")
        providers.append(p)
    channels, names = [], set()
    for entry in raw.get("channels", []):
        c = dict(entry)
        name = c.get("name", c.get("type"))
        if c.get("type") not in CHANNELS or not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name in names:
            raise ValueError("通知类型无效或 name 重复/无效")
        names.add(name)
        c["name"] = name
        channels.append(c)
    interval = monitor.get("interval_seconds", 3600)
    stale = money(monitor.get("stale_after_hours", 26))
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 30 or stale <= 0:
        raise ValueError("采集间隔至少 30 秒，过期时间必须为正数")
    database = Path(monitor.get("database", "cloudcost.sqlite3")).expanduser()
    if not database.is_absolute():
        database = path.parent / database
    return Config(path, database, interval, stale, fx, total_threshold, total_currency, providers, channels, bool(monitor.get("demo", False)))
