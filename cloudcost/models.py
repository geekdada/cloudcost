from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re


def money(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("金额必须是有限十进制数") from None
    if not result.is_finite():
        raise ValueError("金额必须是有限十进制数")
    return result


def month_key(value: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) or int(value[:4]) < 1:
        raise ValueError("月份格式应为 YYYY-MM")
    return value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def next_month(month: str) -> str:
    year, number = map(int, month_key(month).split("-"))
    return f"{year + (number == 12):04d}-{1 if number == 12 else number + 1:02d}"


@dataclass(frozen=True)
class Bill:
    month: str
    amount: Decimal
    currency: str
    source: str
    complete: bool = True
    observed_at: str | None = None
    basis: str = "unbilled_mtd"

    def __post_init__(self):
        month_key(self.month)
        object.__setattr__(self, "amount", money(self.amount))
        if self.currency not in {"USD", "CNY"}:
            raise ValueError("当前仅支持 USD 和 CNY")
        if not isinstance(self.complete, bool):
            raise ValueError("complete 必须是布尔值")
        if self.basis != "unbilled_mtd":
            raise ValueError("只接受当月未出账累计费用（basis=unbilled_mtd）")
        if self.observed_at:
            timestamp = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("observed_at 必须带时区")
            if timestamp > datetime.now(timezone.utc):
                raise ValueError("observed_at 不能是未来时间")
            if timestamp.astimezone(timezone.utc).strftime("%Y-%m") != self.month:
                raise ValueError("未出账快照的数据时间必须在对应月份内")
            object.__setattr__(self, "observed_at", timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds"))
