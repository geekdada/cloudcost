"""Notification adapters. Credentials stay in environment variables."""
import base64
from email.message import EmailMessage
import hashlib
import hmac
import smtplib
import ssl
import sys
import time
import httpx
from .config import env_value
from .providers import require_https


class NotifyError(Exception):
    pass


def send(channel, alert, client=None):
    kind, message = channel["type"], alert["message"]
    if kind == "console":
        print(message, file=sys.stderr)
        return
    if kind == "email":
        email(channel, message)
        return
    own_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        headers, params = {}, {}
        if kind == "telegram":
            url = "https://api.telegram.org/bot" + env_value(channel.get("token_env", "TELEGRAM_BOT_TOKEN")) + "/sendMessage"
            payload = {"chat_id": env_value(channel.get("chat_id_env", "TELEGRAM_CHAT_ID")), "text": message}
        else:
            url = env_value(channel.get("url_env"))
            require_https(url)
            if kind == "slack":
                payload = {"text": message}
            elif kind == "discord":
                payload = {"content": message}
            elif kind in {"wecom", "dingtalk"}:
                payload = {"msgtype": "text", "text": {"content": message}}
                if kind == "dingtalk" and channel.get("secret_env"):
                    stamp = str(int(time.time() * 1000))
                    secret = env_value(channel["secret_env"])
                    signature = base64.b64encode(hmac.new(secret.encode(), (stamp + "\n" + secret).encode(), hashlib.sha256).digest()).decode()
                    params = {"timestamp": stamp, "sign": signature}
            elif kind == "feishu":
                payload = {"msg_type": "text", "content": {"text": message}}
                if channel.get("secret_env"):
                    stamp = str(int(time.time()))
                    secret = env_value(channel["secret_env"])
                    key = (stamp + "\n" + secret).encode()
                    payload.update(timestamp=stamp, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
            elif kind == "ntfy":
                headers = {"Title": "CloudCost budget alert", "Priority": "high", "Tags": "money_with_wings,warning"}
                payload = None
            else:
                payload = {"event": "budget.exceeded", "basis": "unbilled_mtd", "alert": {k: v for k, v in alert.items() if k != "deliveries"}}
            if channel.get("token_env"):
                headers["Authorization"] = "Bearer " + env_value(channel["token_env"])
        kwargs = {"headers": headers, "params": params}
        if kind == "ntfy":
            kwargs["content"] = message.encode("utf-8")
        else:
            kwargs["json"] = payload
        # One HTTP attempt per check: retrying a POST immediately could double-send.
        response = client.post(url, **kwargs)
        if not 200 <= response.status_code < 300:
            raise NotifyError(f"通知返回 HTTP {response.status_code}")
        if kind in {"telegram", "dingtalk", "wecom", "feishu"}:
            data = response.json()
            if kind == "telegram":
                success = data.get("ok") is True
            elif kind == "feishu":
                success = data.get("code", data.get("StatusCode")) == 0
            else:
                success = data.get("errcode") == 0
            if not success:
                raise NotifyError("通知渠道拒绝消息（请检查机器人配置/签名/关键词）")
        if kind == "slack" and response.text.strip() != "ok":
            raise NotifyError("Slack 未确认消息投递")
    except (httpx.HTTPError, ValueError, OSError):
        raise NotifyError("通知网络/配置错误；凭据和响应正文已隐藏") from None
    finally:
        if own_client:
            client.close()


def email(channel, message):
    recipients = channel.get("to", [])
    if isinstance(recipients, str):
        recipients = [recipients]
    if not recipients:
        raise NotifyError("邮件渠道缺少 to")
    mail = EmailMessage()
    mail["Subject"] = "CloudCost 当月未出账费用超过预算"
    mail["From"] = channel["from"]
    mail["To"] = ", ".join(recipients)
    mail.set_content(message)
    mode = channel.get("tls", "starttls")
    if mode not in {"starttls", "ssl"}:
        raise NotifyError("邮件 TLS 必须为 starttls 或 ssl")
    smtp_type = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
    options = {"timeout": 30}
    if mode == "ssl":
        options["context"] = ssl.create_default_context()
    try:
        with smtp_type(channel["host"], int(channel.get("port", 465 if mode == "ssl" else 587)), **options) as smtp:
            if mode == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if channel.get("username_env"):
                smtp.login(env_value(channel["username_env"]), env_value(channel.get("password_env")))
            refused = smtp.send_message(mail)
            if refused:
                raise NotifyError("部分邮件收件人被 SMTP 拒绝")
    except (smtplib.SMTPException, OSError, ValueError):
        raise NotifyError("SMTP 投递失败；检查 TLS、账号和收件人配置") from None
