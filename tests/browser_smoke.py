"""Manual end-to-end check against `cloudcost init --demo` + `serve`.

Usage: python tests/browser_smoke.py --url http://127.0.0.1:8765
Uses an installed Chromium when available, otherwise Playwright Chromium.
"""
import argparse
import json
from pathlib import Path
import shutil
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", default="/tmp/cloudcost-preview")
    args = parser.parse_args()
    with sync_playwright() as p:
        executable = shutil.which("chromium") or shutil.which("google-chrome")
        browser = p.chromium.launch(headless=True, executable_path=executable, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(args.url)
        expect(page.locator("#total-value")).to_have_text("$910.59")
        assert page.locator("#total-value").inner_text() == "$910.59"
        assert page.locator("#overview-provider-table tbody tr").count() == 5
        assert page.locator("#trend-chart circle").count() > 0
        page.screenshot(path=f"{args.output}-desktop.png", full_page=True)

        page.locator("#display-currency").select_option("CNY")
        assert "6,556.24" in page.locator("#total-value").inner_text()
        page.locator("#display-currency").select_option("USD")
        page.locator('[data-series="aws"]').click()
        assert page.locator("#trend-chart svg").get_attribute("aria-label") == "aws累计费用趋势"

        for view in ["providers", "history", "alerts", "settings", "overview"]:
            page.locator(f'nav a[data-view="{view}"]').click()
            page.locator(f"#view-{view}").wait_for(state="visible")
            assert page.locator(f"#view-{view}").is_visible()
        page.locator('nav a[data-view="history"]').click()
        page.locator("#view-history").wait_for(state="visible")
        page.locator("#history-provider").select_option("aliyun")
        assert page.locator("#history-table tbody tr").count() > 0
        assert all(v == "aliyun" for v in page.locator("#history-table tbody tr td:nth-child(2)").all_text_contents())
        with page.expect_download() as event:
            page.locator("#export").click()
        assert "cloudcost-" in event.value.suggested_filename
        page.locator('nav a[data-view="overview"]').click()
        page.locator("#view-overview").wait_for(state="visible")

        old_month = page.locator("#month option").nth(1).get_attribute("value")
        page.locator("#month").select_option(old_month)
        expect(page.locator("#notice")).to_contain_text("历史月份")
        assert page.locator("#total-status").inner_text() == "合计不完整"
        page.locator("#month").select_option(index=0)
        expect(page.locator("#total-value")).to_have_text("$910.59")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=f"{args.output}-mobile.png", full_page=True)

        # Same shell, but all providers lack data. A known subtotal of zero must
        # not be presented as a complete zero-dollar month.
        data = page.request.get(args.url + "/api/summary").json()
        data["complete"] = False; data["total_usd"] = "0"
        for provider in data["providers"]:
            provider.update(amount=None, amount_usd=None, captured_at=None, complete=False, status="missing", budget_percent=None)
        page.route("**/api/summary?*", lambda route: route.fulfill(json=data))
        page.route("**/api/history?*", lambda route: route.fulfill(json={"snapshots": []}))
        page.locator("#refresh").click()
        expect(page.locator("#total-value")).to_have_text("—")
        assert page.locator("#total-status").inner_text() == "合计不完整"
        assert page.locator("#trend-chart").inner_text().find("暂无采集快照") >= 0
        assert not errors, errors
        browser.close()
        print(json.dumps({"status": "ok", "checks": ["desktop", "mobile", "navigation", "currencies", "provider_filter", "month_switch", "csv_export", "empty_state", "no_console_errors"], "screenshots": [f"{args.output}-desktop.png", f"{args.output}-mobile.png"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
