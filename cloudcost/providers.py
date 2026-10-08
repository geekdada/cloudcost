"""Read-only MTD collectors. Invoices and payment history are never cost inputs."""
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
            raise ProviderError(f"账单数据源返回 HTTP {response.status_code}")
        return response
    raise ProviderError("网络请求失败")


def require_https(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderError("远程数据源必须是 HTTPS，认证信息请使用环境变量")


def parse_feed(text, month, currency, fmt, observed_at=None):
    """Canonical JSON snapshots or canonical unbilled line-item CSV.

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
        if row.get("basis") != "unbilled_mtd":
            raise ProviderError("数据源须明确 basis=unbilled_mtd；不接受已出账金额")
        timestamp = row.get("observed_at") or observed_at
        if not timestamp:
            raise ProviderError("费用数据缺少 observed_at，无法判断新鲜度")
        if not isinstance(row.get("complete"), bool):
            raise ProviderError("数据源必须声明 complete=true/false（是否覆盖该账号全部费用）")
        if "amount" not in row or "currency" not in row:
            raise ProviderError("费用快照缺少 amount/currency")
        return Bill(month, money(row["amount"]), row["currency"], "feed:json", row["complete"], timestamp)
    if fmt != "csv":
        raise ProviderError("数据源格式必须为 json/csv")
    reader = csv.DictReader(io.StringIO(text))
    required = {"month", "amount", "currency", "basis", "complete", "observed_at"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ProviderError("CSV 缺少 month,amount,currency,basis,complete,observed_at 列")
    rows = [r for r in reader if r["month"] == month]
    if not rows:
        raise ProviderError("CSV 没有目标月份记录，不能推断费用为零")
    if any(r["basis"] != "unbilled_mtd" for r in rows):
        raise ProviderError("CSV 含非未出账费用记录")
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
                "feed:csv", all(r["complete"].lower() == "true" for r in rows), min(stamps).isoformat())


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
        raise ProviderError("原生采集仅读取当月未结算费用；历史月份请查询已保存的快照")
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
    return Bill(month, sum(amounts, Decimal(0)), units.pop(), source, observed_at=utcnow())


def aliyun(provider, month, client):
    if month != current_month():
        raise ProviderError("原生采集仅读取当月未结算费用；历史月份请查询已保存的快照")
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
    return Bill(month, sum(amounts, Decimal(0)), currencies.pop(), provider["kind"] + ":PretaxAmount", observed_at=utcnow())


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
        else:
            raise ProviderError("此平台需接入未出账费用 feed")
        if bill.currency != provider["currency"]:
            raise ProviderError("返回币种与账号配置不一致；请检查站点和 currency 配置")
        return bill
    finally:
        if own_client:
            client.close()
