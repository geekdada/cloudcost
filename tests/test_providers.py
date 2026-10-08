from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import httpx
from cloudcost.models import current_month, utcnow
from cloudcost.providers import ProviderError, aliyun, aws, collect, parse_feed, request
from test_core import Fixture


class FeedTests(unittest.TestCase):
    def row(self, **kwargs):
        return {"month": current_month(), "amount": "18.42", "currency": "USD", "basis": "unbilled_mtd", "complete": True, "observed_at": utcnow(), **kwargs}

    def parse(self, row):
        return parse_feed(json.dumps(row), current_month(), "USD", "json")

    def test_valid_json_precise_amount(self):
        self.assertEqual(self.parse(self.row()).amount, Decimal("18.42"))

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


if __name__ == "__main__": unittest.main()
