from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import httpx
from cloudcost.models import current_month, next_month, utcnow
from cloudcost.providers import ProviderError, aliyun, aws, cloudflare, collect, parse_feed, request, vercel
from test_core import Fixture


class FeedTests(unittest.TestCase):
    def row(self, **kwargs):
        return {"month": current_month(), "amount": "18.42", "currency": "USD", "basis": "unbilled_mtd", "complete": True, "observed_at": utcnow(), **kwargs}

    def parse(self, row):
        return parse_feed(json.dumps(row), current_month(), "USD", "json")

    def test_valid_json_precise_amount(self):
        self.assertEqual(self.parse(self.row()).amount, Decimal("18.42"))

    def test_calendar_feeds_and_mixed_csv_rejected(self):
        self.assertEqual(self.parse(self.row(basis="calendar_mtd")).basis, "calendar_mtd")
        month, stamp = current_month(), utcnow()
        csv = f"month,amount,currency,basis,complete,observed_at\n{month},1,USD,calendar_mtd,true,{stamp}\n{month},2,USD,calendar_mtd,true,{stamp}\n"
        bill = parse_feed(csv, month, "USD", "csv")
        self.assertEqual(bill.amount, Decimal(3))
        self.assertEqual(bill.basis, "calendar_mtd")
        with self.assertRaises(ProviderError):
            parse_feed(csv.replace(",2,USD,calendar_mtd", ",2,USD,unbilled_mtd"), month, "USD", "csv")

    def test_invoices_and_ambiguous_focus_costs_are_rejected(self):
        for row in [self.row(basis="invoiced"), {"month": current_month(), "BilledCost": 99}]:
            with self.assertRaises(ProviderError): self.parse(row)

    def test_wrong_month_duplicate_month_missing_time_and_partial_coverage(self):
        with self.assertRaises(ProviderError): self.parse(self.row(month="1999-01"))
        with self.assertRaises(ProviderError): self.parse([self.row(), self.row()])
        row = self.row(); del row["observed_at"]
        with self.assertRaises(ProviderError): self.parse(row)
        self.assertFalse(self.parse(self.row(complete=False)).complete)

    def test_nonfinite_currency_future_naive_time_and_string_boolean_rejected(self):
        future = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        for row in [self.row(amount="NaN"), self.row(currency="EUR"), self.row(observed_at=future),
                    self.row(observed_at=datetime.now().isoformat()), self.row(complete="false")]:
            with self.assertRaises((ValueError, ProviderError)): self.parse(row)

    def test_csv_sums_line_items_not_snapshots(self):
        month, stamp = current_month(), utcnow()
        text = f"month,amount,currency,basis,complete,observed_at\n{month},12.30,USD,unbilled_mtd,true,{stamp}\n{month},-2.20,USD,unbilled_mtd,true,{stamp}\n"
        self.assertEqual(parse_feed(text,month,"USD","csv").amount, Decimal("10.10"))
        with self.assertRaises(ProviderError): parse_feed(text.replace("-2.20,USD","-2.20,CNY"),month,"USD","csv")
        with self.assertRaises(ProviderError): parse_feed("BilledCost,BillingCurrency\n12,USD",month,"USD","csv")

    def test_http_retry_on_429_without_leaking_body(self):
        calls=[]
        def handler(req):
            calls.append(req)
            return httpx.Response(429 if len(calls)<3 else 200, text="private-body", request=req)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch("cloudcost.providers.time.sleep"):
            self.assertEqual(request(client,"GET","https://example.com").status_code,200)
        self.assertEqual(len(calls),3)


class CollectorTests(Fixture):
    def test_local_feed_and_currency_mismatch(self):
        row = {"month":self.month,"amount":"7.20","currency":"USD","basis":"unbilled_mtd","complete":True,"observed_at":utcnow()}
        (self.root/"aws.json").write_text(json.dumps(row))
        bill=collect(self.config.provider("aws"),self.config,self.month)
        self.assertEqual(bill.amount,Decimal("7.20"))
        row["currency"]="CNY"; (self.root/"aws.json").write_text(json.dumps(row))
        with self.assertRaises(ProviderError): collect(self.config.provider("aws"),self.config,self.month)

    def test_aws_uses_current_cost_explorer_not_invoices(self):
        ce=Mock()
        ce.get_cost_and_usage.return_value={"ResultsByTime":[{"TimePeriod":{"Start":self.month+"-01"},"Total":{"UnblendedCost":{"Amount":"22.12","Unit":"USD"}},"Estimated":True}]}
        sdk=SimpleNamespace(Session=Mock(return_value=SimpleNamespace(client=Mock(return_value=ce))))
        with patch.dict(sys.modules,{"boto3":sdk}): bill=aws({"profile":"readonly"},self.month)
        self.assertEqual(bill.amount,Decimal("22.12"))
        self.assertEqual(bill.basis,"calendar_mtd")
        self.assertIn("estimated",bill.source)
        params=ce.get_cost_and_usage.call_args.kwargs
        self.assertEqual(params["TimePeriod"]["Start"],self.month+"-01")
        self.assertEqual(params["Metrics"],["UnblendedCost"])

    def test_aws_empty_data_is_not_zero_and_past_month_rejected(self):
        ce=Mock(); ce.get_cost_and_usage.return_value={"ResultsByTime":[]}
        sdk=SimpleNamespace(Session=Mock(return_value=SimpleNamespace(client=Mock(return_value=ce))))
        with patch.dict(sys.modules,{"boto3":sdk}):
            with self.assertRaises(ProviderError): aws({},self.month)
            with self.assertRaises(ProviderError): aws({},"1999-01")

    def test_aliyun_signing_pagination_and_unpaid_amount(self):
        seen=[]
        def handler(req):
            seen.append(req)
            page=int(req.url.params["PageNum"])
            self.assertEqual(req.url.params["Action"],"QueryAccountBill")
            self.assertEqual(req.url.params["BillingCycle"],self.month)
            self.assertTrue(req.url.params.get("Signature"))
            return httpx.Response(200,json={"Success":True,"Code":"Success","Data":{"TotalCount":2,"BillingCycle":self.month,"Items":{"Item":[{"PretaxAmount":"72" if page==1 else "36","PaymentAmount":"0","Currency":"CNY"}]}}})
        env={"ALIYUN_ACCESS_KEY_ID":"fake-id","ALIYUN_ACCESS_KEY_SECRET":"fake-secret"}
        with patch.dict(os.environ,env), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            bill=aliyun({"kind":"aliyun"},self.month,client)
        self.assertEqual(bill.amount,Decimal(108))
        self.assertEqual(bill.basis,"calendar_mtd")
        self.assertEqual(len(seen),2)
        self.assertEqual(seen[0].url.host,"business.aliyuncs.com")

    def test_international_endpoint_and_default_currency(self):
        def handler(req):
            self.assertEqual(req.url.host,"business.ap-southeast-1.aliyuncs.com")
            return httpx.Response(200,json={"Success":True,"Code":"Success","Data":{"TotalCount":1,"Items":{"Item":[{"PretaxAmount":"12","Currency":"USD"}]}}})
        with patch.dict(os.environ,{"ALIBABACLOUD_ACCESS_KEY_ID":"id","ALIBABACLOUD_ACCESS_KEY_SECRET":"secret"}), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(aliyun({"kind":"alibabacloud"},self.month,client).currency,"USD")

    def test_aliyun_empty_bill_is_not_zero(self):
        with patch.dict(os.environ,{"ALIYUN_ACCESS_KEY_ID":"id","ALIYUN_ACCESS_KEY_SECRET":"secret"}), httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={"Success":True,"Code":"Success","Data":{"TotalCount":0,"Items":{"Item":[]}}}))) as client:
            with self.assertRaises(ProviderError): aliyun({"kind":"aliyun"},self.month,client)


class CalendarCollectorTests(unittest.TestCase):
    def setUp(self):
        self.month = current_month()
        self.env = {"VERCEL_TOKEN": "fake-token", "VERCEL_TEAM_ID": "team_test",
                    "CLOUDFLARE_API_TOKEN": "fake-cf-token", "CLOUDFLARE_ACCOUNT_ID": "a" * 32}

    def row(self, amount="12.30", **kwargs):
        # Daily rows: don't depend on tests running after the first of a month.
        return {"BilledCost": amount, "BillingCurrency": "USD", "ChargePeriodStart": self.month+"-01T00:00:00Z",
                "ChargePeriodEnd": self.month+"-02T00:00:00Z", "EffectiveCost": "999", **kwargs}

    def test_vercel_jsonl_exact_cost_credits_and_utc_window(self):
        def handler(req):
            self.assertEqual(req.method, "GET")
            self.assertEqual(req.url.path, "/v1/billing/charges")
            self.assertEqual(req.url.params["teamId"], "team_test")
            self.assertEqual(req.url.params["from"], self.month+"-01T00:00:00Z")
            self.assertEqual(req.headers["Authorization"], "Bearer fake-token")
            return httpx.Response(200,text='\n'.join(json.dumps(r) for r in [self.row(0.1), self.row(0.2), self.row(-0.05)]))
        with patch.dict(os.environ,self.env), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            bill = vercel({},self.month,client)
        self.assertEqual(bill.amount,Decimal("0.25"))
        self.assertEqual(bill.basis,"calendar_mtd")
        self.assertTrue(bill.complete)

    def test_vercel_uses_charge_month_not_invoice_month(self):
        row = self.row(BillingPeriodStart="1999-01-01T00:00:00Z")
        foreign = self.row(100,ChargePeriodStart="1999-01-01T00:00:00Z",ChargePeriodEnd="1999-01-02T00:00:00Z")
        with patch.dict(os.environ,self.env), httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,text=json.dumps(row)+'\n'+json.dumps(foreign)))) as client:
            self.assertEqual(vercel({},self.month,client).amount,Decimal("12.30"))

    def test_vercel_empty_unrated_mixed_currency_and_cross_month_rejected(self):
        rows = [[], [self.row(BilledCost=None)], [self.row(),self.row(BillingCurrency="CNY")],
                [self.row(ChargePeriodEnd=next_month(self.month)+"-02T00:00:00Z")]]
        for items in rows:
            with patch.dict(os.environ,self.env), httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,text='\n'.join(json.dumps(r) for r in items)))) as client:
                with self.assertRaises(ProviderError): vercel({},self.month,client)

    def test_cloudflare_v1_cost_uses_lookback_and_excludes_previous_month(self):
        def handler(req):
            self.assertEqual(req.method,"GET")
            self.assertEqual(req.url.path,"/client/v4/accounts/"+"a"*32+"/billable-usage")
            first=datetime.fromisoformat(self.month+"-01").date()
            self.assertEqual(req.url.params["from"],(first-timedelta(days=31)).isoformat())
            prior=first-timedelta(days=1)
            previous=self.row("999",ChargePeriodStart=prior.isoformat()+"T00:00:00Z",ChargePeriodEnd=self.month+"-01T00:00:00Z")
            rows=[previous,self.row(CumulatedContractedCost="999"),self.row("-2.20",BillingPeriodStart=prior.isoformat()+"T00:00:00Z")]
            return httpx.Response(200,json={"success":True,"result":rows})
        with patch.dict(os.environ,self.env), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            partial=cloudflare({"currency":"USD"},self.month,client)
            total=cloudflare({"currency":"USD","fixed_monthly_cost":"5"},self.month,client)
        self.assertEqual(partial.amount,Decimal("10.10"))
        self.assertFalse(partial.complete)
        self.assertEqual(total.amount,Decimal("15.10"))
        self.assertTrue(total.complete)
        self.assertEqual(total.basis,"calendar_mtd")
        self.assertIn("cloudflare:v1:",total.source)
        self.assertIn("configured-fixed-fees",total.source)

    def test_cloudflare_v2_fallback_if_v1_is_unavailable(self):
        seen=[]
        def handler(req):
            seen.append(req)
            if len(seen)==1:
                return httpx.Response(403,json={"success":False})
            self.assertEqual(req.method,"POST")
            self.assertTrue(req.url.path.endswith('/billable/usage'))
            body=json.loads(req.content)
            self.assertEqual(body["Metric"],"cost")
            self.assertEqual(body["TimePeriod"]["From"],self.month+"-01T00:00:00Z")
            return httpx.Response(200,json={"success":True,"result":[self.row()]})
        with patch.dict(os.environ,self.env), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            bill=cloudflare({"currency":"USD","fixed_monthly_cost":"0"},self.month,client)
        self.assertEqual(len(seen),2)
        self.assertEqual(bill.amount,Decimal("12.30"))
        self.assertIn("cloudflare:v2:",bill.source)

    def test_cloudflare_natural_month_combines_two_subscription_cycles(self):
        rows=[
            self.row("99",ChargePeriodStart="2026-11-28T00:00:00Z",ChargePeriodEnd="2026-11-29T00:00:00Z",BillingPeriodStart="2026-11-26T00:00:00Z"),
            self.row("12.30",ChargePeriodStart="2026-12-01T00:00:00Z",ChargePeriodEnd="2026-12-02T00:00:00Z",BillingPeriodStart="2026-11-26T00:00:00Z",CumulatedContractedCost="999"),
            self.row("4.20",ChargePeriodStart="2026-12-27T00:00:00Z",ChargePeriodEnd="2026-12-28T00:00:00Z",BillingPeriodStart="2026-12-26T00:00:00Z",CumulatedContractedCost="999")]
        with patch.dict(os.environ,self.env), patch("cloudcost.providers.current_month",return_value="2026-12"), patch("cloudcost.providers.utcnow",return_value="2026-12-31T12:00:00+00:00"), patch("cloudcost.providers.datetime") as clock, patch("cloudcost.models.datetime") as model_clock, httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={"success":True,"result":rows}))) as client:
            clock.now.return_value=datetime(2026,12,31,12,tzinfo=timezone.utc)
            clock.fromisoformat.side_effect=datetime.fromisoformat
            model_clock.now.return_value=clock.now.return_value
            model_clock.fromisoformat.side_effect=datetime.fromisoformat
            bill=cloudflare({"currency":"USD","fixed_monthly_cost":"0"},"2026-12",client)
        self.assertEqual(bill.amount,Decimal("16.50"))
        self.assertTrue(bill.complete)

    def test_cloudflare_both_unavailable_has_actionable_safe_error(self):
        def handler(req):
            return httpx.Response(403 if req.method=='GET' else 405,json={"errors":[{"message":"private-token"}]})
        with patch.dict(os.environ,self.env), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderError) as caught:
                cloudflare({"currency":"USD"},self.month,client)
        self.assertIn("v1 HTTP 403",str(caught.exception))
        self.assertIn("v2 HTTP 405",str(caught.exception))
        self.assertIn("Billing Read",str(caught.exception))
        self.assertNotIn("private-token",str(caught.exception))

    def test_cloudflare_invalid_token_does_not_try_other_endpoints(self):
        seen=[]
        def handler(req):
            seen.append(req)
            return httpx.Response(401)
        with patch.dict(os.environ,self.env), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderError): cloudflare({"currency":"USD"},self.month,client)
        self.assertEqual(len(seen),1)

    def test_cloudflare_empty_unrated_and_failed_responses_are_not_zero(self):
        for data in [{"success":True,"result":[]},{"success":True,"result":[self.row(BilledCost=None)]},
                     {"success":False,"errors":[{"message":"secret"}]}]:
            with patch.dict(os.environ,self.env), httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=data))) as client:
                with self.assertRaises(ProviderError) as caught: cloudflare({"currency":"USD","fixed_monthly_cost":"0"},self.month,client)
                self.assertNotIn("secret",str(caught.exception))

    def test_last_day_query_stops_at_next_month_and_keeps_31_day_limit(self):
        observed=[]
        def handler(req):
            observed.append(dict(req.url.params))
            return httpx.Response(200,json={"success":True,"result":[self.row(ChargePeriodStart="2026-12-31T00:00:00Z",ChargePeriodEnd="2027-01-01T00:00:00Z")]})
        with patch.dict(os.environ,self.env), patch("cloudcost.providers.current_month",return_value="2026-12"), patch("cloudcost.providers.utcnow",return_value="2026-12-31T12:00:00+00:00"), patch("cloudcost.providers.datetime") as clock, patch("cloudcost.models.datetime") as model_clock, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            clock.now.return_value=datetime(2026,12,31,12,tzinfo=timezone.utc)
            clock.fromisoformat.side_effect=datetime.fromisoformat
            model_clock.now.return_value=clock.now.return_value
            model_clock.fromisoformat.side_effect=datetime.fromisoformat
            cloudflare({"currency":"USD","fixed_monthly_cost":"0"},"2026-12",client)
        self.assertEqual(observed[0]["to"],"2027-01-01")
        self.assertEqual(observed[0]["from"],"2026-10-31")


if __name__ == "__main__": unittest.main()
