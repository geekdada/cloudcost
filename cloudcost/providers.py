"""Read-only MTD collectors, using charge dates rather than payment history."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import base64
import csv
import hashlib
import hmac
import io
import json
import time
from urllib.parse import quote, urlparse
import uuid
import httpx
from .config import env_value
from .models import Bill, current_month, money, month_key, next_month, utcnow


class ProviderError(Exception):
    """Safe user-facing error with no credentials or response bodies."""


class ProviderHTTPError(ProviderError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"账单数据源返回 HTTP {status_code}")


def request(client, method, url, **kwargs):
    for attempt in range(3):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt == 2:
                raise ProviderError("网络连接失败或超时") from None
            time.sleep(0.25 * 2**attempt)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 2:
                time.sleep(0.25 * 2**attempt)
                continue
        if response.is_error:
            raise ProviderHTTPError(response.status_code)
        return response
    raise ProviderError("网络请求失败")


def require_https(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderError("远程数据源必须是 HTTPS，认证信息请使用环境变量")


def parse_feed(text, month, currency, fmt, observed_at=None):
    """Canonical JSON snapshots or line-item CSV with an explicit cost basis.

    Metadata is mandatory: generic invoice/FOCUS BilledCost CSVs are rejected.
    """
    month_key(month)
    if fmt == "json":
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            raise ProviderError("费用数据不是合法 JSON") from None
        rows = raw if isinstance(raw, list) else [raw]
        matches = [r for r in rows if isinstance(r, dict) and r.get("month") == month]
        if len(matches) != 1:
            raise ProviderError("JSON 必须包含且仅包含一个目标月份的累计费用快照")
        row = matches[0]
        if row.get("basis") not in {"unbilled_mtd", "calendar_mtd"}:
            raise ProviderError("数据源须明确 basis=unbilled_mtd 或 calendar_mtd")
        timestamp = row.get("observed_at") or observed_at
        if not timestamp:
            raise ProviderError("费用数据缺少 observed_at，无法判断新鲜度")
        if not isinstance(row.get("complete"), bool):
            raise ProviderError("数据源必须声明 complete=true/false（是否覆盖该账号全部费用）")
        if "amount" not in row or "currency" not in row:
            raise ProviderError("费用快照缺少 amount/currency")
        return Bill(month, money(row["amount"]), row["currency"], "feed:json", row["complete"], timestamp, row["basis"])
    if fmt != "csv":
        raise ProviderError("数据源格式必须为 json/csv")
    reader = csv.DictReader(io.StringIO(text))
    required = {"month", "amount", "currency", "basis", "complete", "observed_at"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ProviderError("CSV 缺少 month,amount,currency,basis,complete,observed_at 列")
    rows = [r for r in reader if r["month"] == month]
    if not rows:
        raise ProviderError("CSV 没有目标月份记录，不能推断费用为零")
    bases = {r["basis"] for r in rows}
    if len(bases) != 1 or not bases.issubset({"unbilled_mtd", "calendar_mtd"}):
        raise ProviderError("CSV 费用口径必须一致且为 unbilled_mtd/calendar_mtd")
    currencies = {r["currency"] for r in rows}
    if len(currencies) != 1:
        raise ProviderError("同一账号月份的 CSV 不允许混合币种")
    if any(r["complete"].lower() not in {"true", "false"} for r in rows):
        raise ProviderError("CSV complete 须为 true/false")
    stamps = [datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00")) for r in rows]
    if any(s.tzinfo is None for s in stamps):
        raise ProviderError("CSV observed_at 必须包含时区")
    if any(s > datetime.now(timezone.utc) or s.astimezone(timezone.utc).strftime("%Y-%m") != month for s in stamps):
        raise ProviderError("CSV 数据时间须在对应月份内且不能是未来时间")
    # All rows constitute ONE full export, never a list of repeated MTD snapshots.
    return Bill(month, sum((money(r["amount"]) for r in rows), Decimal(0)), currencies.pop(),
                "feed:csv", all(r["complete"].lower() == "true" for r in rows), min(stamps).isoformat(), bases.pop())


def feed(provider, config, month, client):
    if bool(provider.get("path")) == bool(provider.get("url")):
        raise ProviderError("feed 必须配置且仅配置 path 或 url")
    fmt = provider.get("format", "json")
    if provider.get("path"):
        path = Path(provider["path"].replace("{month}", month)).expanduser()
        if not path.is_absolute():
            path = config.path.parent / path
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ProviderError("费用文件超过 20 MiB")
        return parse_feed(path.read_text(encoding="utf-8-sig"), month, provider["currency"], fmt)
    url = provider["url"].replace("{month}", month)
    require_https(url)
    headers = {"Accept": "application/json" if fmt == "json" else "text/csv"}
    if provider.get("token_env"):
        headers["Authorization"] = "Bearer " + env_value(provider["token_env"])
    response = request(client, "GET", url, headers=headers)
    if len(response.content) > 20 * 1024 * 1024:
        raise ProviderError("费用数据超过 20 MiB")
    return parse_feed(response.text, month, provider["currency"], fmt)


def aws(provider, month, client=None):
    try:
        import boto3
    except ImportError:
        raise ProviderError('AWS 原生采集需安装：pip install "cloudcost-monitor[aws]"（源码可用 .[aws]）') from None
    if month != current_month():
        raise ProviderError("原生采集仅读取当月费用；历史月份请查询已保存的快照")
    # SDK default credential chain supports profiles, SSO, roles and env variables.
    session = boto3.Session(profile_name=provider.get("profile"))
    ce = session.client("ce", region_name=provider.get("region", "us-east-1"))
    end = min(next_month(month) + "-01", (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat())
    params = {"TimePeriod": {"Start": month + "-01", "End": end},
              "Granularity": "MONTHLY", "Metrics": [provider.get("metric", "UnblendedCost")]}
    if params["Metrics"][0] not in {"UnblendedCost", "NetUnblendedCost", "AmortizedCost", "NetAmortizedCost"}:
        raise ProviderError("不支持的 AWS 成本指标")
    amounts, units, estimated = [], set(), False
    tokens = set()
    while True:
        data = ce.get_cost_and_usage(**params)
        for result in data.get("ResultsByTime", []):
            if not result["TimePeriod"]["Start"].startswith(month):
                continue
            total = result.get("Total", {}).get(params["Metrics"][0])
            if not total:
                raise ProviderError("AWS 返回结果缺少成本金额")
            amounts.append(money(total["Amount"]))
            units.add(total["Unit"])
            estimated |= bool(result.get("Estimated"))
        token = data.get("NextPageToken")
        if not token:
            break
        if token in tokens:
            raise ProviderError("AWS 分页令牌重复")
        tokens.add(token)
        params["NextPageToken"] = token
    if not amounts or len(units) != 1:
        raise ProviderError("AWS 尚未提供该月份费用数据")
    source = f"aws:{params['Metrics'][0]}" + (":estimated" if estimated else "")
    return Bill(month, sum(amounts, Decimal(0)), units.pop(), source, observed_at=utcnow(), basis="calendar_mtd")


def aliyun(provider, month, client):
    if month != current_month():
        raise ProviderError("原生采集仅读取当月费用；历史月份请查询已保存的快照")
    international = provider["kind"] == "alibabacloud"
    prefix = "ALIBABACLOUD" if international else "ALIYUN"
    key = env_value(provider.get("access_key_env", prefix + "_ACCESS_KEY_ID"))
    secret = env_value(provider.get("secret_key_env", prefix + "_ACCESS_KEY_SECRET"))
    token = env_value(provider.get("security_token_env", prefix + "_SECURITY_TOKEN"), required=False)
    endpoint = provider.get("endpoint", "https://business.ap-southeast-1.aliyuncs.com" if international else "https://business.aliyuncs.com")
    require_https(endpoint)
    amounts, currencies, page, total_count = [], set(), 1, None
    while True:
        params = {"Action": "QueryAccountBill", "Version": "2017-12-14", "Format": "JSON",
                  "AccessKeyId": key, "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
                  "SignatureNonce": str(uuid.uuid4()), "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "BillingCycle": month, "IsGroupByProduct": "true", "PageNum": str(page), "PageSize": "300"}
        if token:
            params["SecurityToken"] = token
        canonical = "&".join(quote(k, safe="~") + "=" + quote(v, safe="~") for k, v in sorted(params.items()))
        to_sign = "GET&%2F&" + quote(canonical, safe="~")
        params["Signature"] = base64.b64encode(hmac.new((secret + "&").encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
        response = request(client, "GET", endpoint, params=params)
        data = response.json()
        if data.get("Success") is not True or data.get("Code") != "Success":
            raise ProviderError("阿里云费用 API 拒绝请求；检查账单只读权限与 AK 所属站点")
        body = data.get("Data", {})
        if str(body.get("BillingCycle", month)) != month:
            raise ProviderError("阿里云返回月份与请求不匹配")
        items = body.get("Items", {}).get("Item", [])
        total_count = int(body.get("TotalCount", len(items)))
        for item in items:
            # PretaxAmount is after discounts, before tax. PaymentAmount would omit unpaid costs.
            if "PretaxAmount" not in item or not item.get("Currency"):
                raise ProviderError("阿里云返回结果缺少 PretaxAmount/Currency")
            amounts.append(money(item["PretaxAmount"]))
            currencies.add(item["Currency"])
        if len(amounts) >= total_count:
            break
        if not items or page >= 1000:
            raise ProviderError("阿里云费用分页不完整")
        page += 1
    if not amounts:
        # A missing current bill is NOT evidence of zero usage.
        raise ProviderError("阿里云尚未生成当月费用数据，不能推断为零")
    if len(currencies) != 1:
        raise ProviderError("阿里云返回混合币种")
    return Bill(month, sum(amounts, Decimal(0)), currencies.pop(), provider["kind"] + ":PretaxAmount", observed_at=utcnow(), basis="calendar_mtd")


def calendar_window(month):
    month_key(month)
    if month != current_month():
        raise ProviderError("原生采集仅读取当月费用；历史月份请查询已保存的快照")
    # Both APIs have daily usage records. Include today's available records.
    end = min(next_month(month) + "-01", (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat())
    return month + "-01T00:00:00Z", end + "T00:00:00Z"


def rated_total(rows, month, end):
    """Sum individual FOCUS charges, never cumulative or amortized cost fields."""
    if not isinstance(rows, list) or not rows:
        raise ProviderError("平台尚未提供当月金额明细，不能推断为零（接口也可能尚未开放）")
    lower = datetime.fromisoformat(month + "-01T00:00:00+00:00")
    upper = datetime.fromisoformat(end.replace("Z", "+00:00"))
    amounts, currencies = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError("费用明细结构无效")
        try:
            start = datetime.fromisoformat(row["ChargePeriodStart"].replace("Z", "+00:00"))
            stop = datetime.fromisoformat(row["ChargePeriodEnd"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("费用明细缺少有效的 ChargePeriodStart/End") from None
        if start.tzinfo is None or stop.tzinfo is None or stop <= start:
            raise ProviderError("费用明细的时间范围或时区无效")
        if stop <= lower or start >= upper:
            continue
        if start < lower or stop > upper:
            raise ProviderError("费用明细跨越查询边界，无法精确归属自然月")
        if row.get("BilledCost") is None or not row.get("BillingCurrency"):
            raise ProviderError("费用明细缺少 BilledCost/BillingCurrency；仅有用量不能推断金额")
        amounts.append(money(row["BilledCost"]))
        currencies.add(row["BillingCurrency"])
    if not amounts or len(currencies) != 1:
        raise ProviderError("目标月份没有金额明细或包含混合币种")
    return sum(amounts, Decimal(0)), currencies.pop()


def vercel(provider, month, client):
    start, end = calendar_window(month)
    params = {"from": start, "to": end}
    team = provider.get("team_id") or env_value(provider.get("team_id_env", "VERCEL_TEAM_ID"), required=False)
    if team:
        params["teamId"] = team
    elif provider.get("slug"):
        params["slug"] = provider["slug"]
    else:
        raise ProviderError("Vercel 请配置 team_id、team_id_env 或 slug")
    token = env_value(provider.get("token_env", "VERCEL_TOKEN"))
    response = request(client, "GET", "https://api.vercel.com/v1/billing/charges", params=params,
                       headers={"Authorization": "Bearer " + token, "Accept": "application/jsonl"})
    if len(response.content) > 20 * 1024 * 1024:
        raise ProviderError("费用数据超过 20 MiB")
    try:
        rows = [json.loads(line, parse_float=Decimal) for line in response.text.splitlines() if line.strip()]
    except (ValueError, json.JSONDecodeError):
        raise ProviderError("Vercel 未返回合法 JSONL 费用明细") from None
    amount, currency = rated_total(rows, month, end)
    return Bill(month, amount, currency, "vercel:FOCUS:BilledCost", observed_at=utcnow(), basis="calendar_mtd")


def cloudflare(provider, month, client):
    start, end = calendar_window(month)
    account = provider.get("account_id") or env_value(provider.get("account_id_env", "CLOUDFLARE_ACCOUNT_ID"))
    if not isinstance(account, str) or len(account) != 32 or any(c not in "0123456789abcdefABCDEF" for c in account):
        raise ProviderError("Cloudflare account_id 应为 32 位十六进制 ID")
    token = env_value(provider.get("token_env", "CLOUDFLARE_API_TOKEN"))
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}"
    headers = {"Authorization": "Bearer " + token}
    # V1 returns rated daily costs for PayGo accounts. A query must include each
    # subscription's billing anchor, which may precede the calendar month.
    # Look back 31 days, then filter CHARGE dates when summing below. Do not sum
    # CumulatedContractedCost or the whole subscription billing period.
    lookback = (datetime.fromisoformat(start.replace("Z", "+00:00")).date() - timedelta(days=31)).isoformat()
    api = "v1"
    try:
        response = request(client, "GET", base + "/billable-usage", headers=headers,
                           params={"from": lookback, "to": end[:10]})
    except ProviderHTTPError as first:
        if first.status_code not in {403, 404, 405}:
            raise
        # The restricted V2 cost query is available on some accounts only.
        api = "v2"
        try:
            response = request(client, "POST", base + "/billable/usage", headers=headers,
                               json={"Metric": "cost", "TimePeriod": {"From": start, "To": end}})
        except ProviderHTTPError as second:
            raise ProviderError(f"Cloudflare 费用接口不可用（v1 HTTP {first.status_code}，v2 HTTP {second.status_code}）；检查 Billing Read 权限及账号接口开放状态") from None
    if len(response.content) > 20 * 1024 * 1024:
        raise ProviderError("费用数据超过 20 MiB")
    data = json.loads(response.text, parse_float=Decimal)
    if not isinstance(data, dict) or data.get("success") is not True or data.get("errors"):
        raise ProviderError("Cloudflare 费用查询失败；检查 Billing Read 权限及受限接口开放状态")
    amount, currency = rated_total(data.get("result"), month, end)
    # Metered costs exclude fixed subscriptions. Explicit configuration declares
    # their monthly total; absence means coverage is partial, never complete zero.
    fixed = provider.get("fixed_monthly_cost")
    if fixed is not None:
        if currency != provider["currency"]:
            raise ProviderError("返回币种与固定月费配置不一致")
        amount += money(fixed)
    source = f"cloudflare:{api}:FOCUS:BilledCost" + (":configured-fixed-fees" if fixed is not None else ":usage-only")
    return Bill(month, amount, currency, source, complete=fixed is not None, observed_at=utcnow(), basis="calendar_mtd")


def collect(provider, config, month, client=None):
    own_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        if provider["mode"] == "feed":
            bill = feed(provider, config, month, client)
        elif provider["kind"] == "aws":
            bill = aws(provider, month)
        elif provider["kind"] in {"aliyun", "alibabacloud"}:
            bill = aliyun(provider, month, client)
        elif provider["kind"] == "vercel":
            bill = vercel(provider, month, client)
        elif provider["kind"] == "cloudflare":
            bill = cloudflare(provider, month, client)
        else:
            raise ProviderError("未知平台")
        if bill.currency != provider["currency"]:
            raise ProviderError("返回币种与账号配置不一致；请检查站点和 currency 配置")
        return bill
    finally:
        if own_client:
            client.close()
