import os
import json
import unittest
from unittest.mock import Mock, patch
import httpx
from fastapi.testclient import TestClient
from cloudcost.api import create_app
from cloudcost.notify import NotifyError, send
from test_core import Fixture


class APITests(Fixture):
    def test_authenticated_api_query_validation_and_readonly(self):
        self.seed()
        with TestClient(create_app(self.config,"test-secret")) as client:
            self.assertEqual(client.get("/").status_code,200)
            self.assertEqual(client.get("/api/summary").status_code,401)
            headers={"Authorization":"Bearer test-secret"}
            response=client.get("/api/summary",headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["total_usd"],"120")
            self.assertNotIn("TEST_WEBHOOK",response.text)
            self.assertEqual(response.headers["cache-control"],"no-store")
            self.assertIn("script-src 'self'",response.headers["content-security-policy"])
            self.assertEqual(client.get("/api/summary?month=bad",headers=headers).status_code,422)
            self.assertEqual(client.get("/api/history?limit=99999",headers=headers).status_code,422)
            self.assertEqual(client.get("/api/history?provider=unknown",headers=headers).status_code,404)
            self.assertEqual(client.post("/api/summary",headers=headers).status_code,405)

    def test_empty_db_does_not_report_complete_zero(self):
        with TestClient(create_app(self.config)) as client:
            data=client.get("/api/summary").json()
            self.assertFalse(data["complete"])
            self.assertTrue(all(p["amount"] is None for p in data["providers"]))


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.alert={"id":1,"scope":"total","month":"2026-10","message":"CloudCost budget alert","amount":"120","threshold":"100","currency":"USD"}

    def test_webhook_preserves_calendar_basis(self):
        self.alert["basis"]="calendar_mtd"
        seen=[]
        with patch.dict(os.environ,{"TEST_URL":"https://example.com/hook"}), httpx.Client(transport=httpx.MockTransport(lambda r: (seen.append(json.loads(r.content)), httpx.Response(200))[1])) as client:
            send({"type":"webhook","url_env":"TEST_URL"},self.alert,client)
        self.assertEqual(seen[0]["basis"],"calendar_mtd")
        self.assertEqual(seen[0]["alert"]["basis"],"calendar_mtd")

    def test_webhook_slack_discord_and_ntfy(self):
        for kind in ["webhook","slack","discord","ntfy"]:
            seen=[]
            def handler(req):
                seen.append(req)
                return httpx.Response(200,text="ok")
            with patch.dict(os.environ,{"TEST_URL":"https://example.com/hook"}), httpx.Client(transport=httpx.MockTransport(handler)) as client:
                send({"type":kind,"url_env":"TEST_URL"},self.alert,client)
            self.assertEqual(len(seen),1)
            if kind=="ntfy": self.assertIn(b"CloudCost",seen[0].content)

    def test_bot_payload_and_application_level_errors(self):
        for kind,response in [("dingtalk",{"errcode":0}),("wecom",{"errcode":0}),("feishu",{"code":0}),("telegram",{"ok":True})]:
            seen=[]
            def handler(req):
                seen.append(req)
                return httpx.Response(200,json=response)
            env={"TEST_URL":"https://example.com/hook","TEST_SECRET":"sign-secret","TELEGRAM_BOT_TOKEN":"fake-token","TELEGRAM_CHAT_ID":"123"}
            with patch.dict(os.environ,env), httpx.Client(transport=httpx.MockTransport(handler)) as client:
                send({"type":kind,"url_env":"TEST_URL","secret_env":"TEST_SECRET"},self.alert,client)
            self.assertEqual(len(seen),1)
            with patch.dict(os.environ,env), httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={"ok":False,"errcode":1,"code":1}))) as client:
                with self.assertRaises(NotifyError): send({"type":kind,"url_env":"TEST_URL"},self.alert,client)

    def test_http_error_single_attempt_and_no_url_leak(self):
        seen=[]
        def handler(req):
            seen.append(req)
            return httpx.Response(500,text="private-response")
        with patch.dict(os.environ,{"TEST_URL":"https://example.com/hook?token=private-key"}), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(NotifyError) as error: send({"type":"webhook","url_env":"TEST_URL"},self.alert,client)
        self.assertEqual(len(seen),1)
        self.assertNotIn("private",str(error.exception))

    def test_smtp_starttls_auth_and_send(self):
        smtp=Mock(); smtp.__enter__=Mock(return_value=smtp); smtp.__exit__=Mock(return_value=False)
        smtp.send_message.return_value={}
        with patch.dict(os.environ,{"SMTP_USER":"user","SMTP_PASSWORD":"password"}),patch("cloudcost.notify.smtplib.SMTP",return_value=smtp):
            send({"type":"email","host":"smtp.example.com","from":"cost@example.com","to":["ops@example.com"],"username_env":"SMTP_USER","password_env":"SMTP_PASSWORD"},self.alert)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("user","password")
        smtp.send_message.assert_called_once()


if __name__ == "__main__": unittest.main()
