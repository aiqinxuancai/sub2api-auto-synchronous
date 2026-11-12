"""Exercise the UI in Chrome against isolated local Sub2API fixtures."""

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relaydesk.service import Service
from relaydesk.web import create_app
from test_web import LocalSites


def main():
    root = Path(__file__).resolve().parent.parent
    artifacts = root / ".cache" / "browser"
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        sites = LocalSites()
        base = Path(folder)
        service = Service(base / "web", start_scheduler=False)
        sites.configure(service)
        service.save_config({"sync_enabled": False, "max_new_keys": 30,
                             "sync_platform_limits": {"openai": 0, "anthropic": 0, "gemini": 0}})
        names = ["Claude · 稳定专线", "GPT · 团队标准", "Gemini · 高速通道", "Claude · 代码助手", "GPT · 轻量任务", "Gemini · 长文本", "Claude · 研发团队", "GPT · 通用推理", "Gemini · 共享池"]
        for offset, name in enumerate(names, 10):
            sites.fake.groups.append({"id": offset, "name": name, "platform": ["anthropic", "openai", "gemini"][offset % 3], "status": "active", "rate_multiplier": round(0.1 * (offset - 8), 2)})
            sites.fake.healthy_group_ids.add(offset)
        app = create_app(service=service)
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline: time.sleep(.05)
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True)
                page = browser.new_page(viewport={"width": 1512, "height": 1000}, device_scale_factor=1)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                expect(page.locator("#auth-title")).to_have_text("从这里，开始流畅")
                page.screenshot(path=str(artifacts / "login.png"), full_page=True)
                page.fill("#auth-password", "browser-test-password")
                page.fill("#auth-confirm", "browser-test-password")
                page.click(".auth-submit")
                expect(page.locator("#workspace")).to_be_visible()
                page.click('[data-page="settings"]')
                page.click('[data-test="upstream"]')
                expect(page.locator("#test-upstream-result")).to_contain_text("连接成功", timeout=15000)
                page.click('[data-test="target"]')
                expect(page.locator("#test-target-result")).to_contain_text("连接成功", timeout=15000)
                page.screenshot(path=str(artifacts / "settings.png"), full_page=True)
                page.click('[data-page="overview"]')
                page.click("#preview-sync")
                expect(page.locator("#job-dialog")).to_be_visible(timeout=15000)
                expect(page.locator("#job-detail-content")).to_contain_text("本次为预览")
                page.locator("#job-dialog [data-close]").click()
                assert not sites.fake.keys
                page.click("#sync-now")
                expect(page.locator("#stat-active")).to_have_text("10", timeout=15000)
                assert len(sites.fake.keys) == 10
                page.click('[data-page="settings"]')
                expect(page.locator('#stable-rules')).to_contain_text("还没有优选分组")
                page.locator('[data-platform="anthropic"]').uncheck()
                page.locator('[data-platform="gemini"]').uncheck()
                expect(page.locator('[data-platform-limit="gemini"]')).to_be_disabled()
                page.fill('[data-platform-limit="openai"]', "2")
                page.select_option("#stable-add-platform", "openai")
                page.select_option("#stable-add-strategy", "low_cost")
                page.click("#stable-add")
                rule = page.locator('.stable-rule[data-strategy="low_cost"][data-platform="openai"]')
                expect(rule.locator('[data-field="name"]')).to_have_value("最低价 OpenAI")
                page.click("#stable-add")
                expect(page.locator("#toasts")).to_contain_text("只能添加一次")
                rule.locator('[data-field="score_minutes"]').fill("20")
                rule.locator('[data-field="health_minutes"]').fill("3")
                rule.locator('[data-field="retry_minutes"]').fill("2")
                rule.locator('[data-field="exclude_keywords"]').fill("Alpha，测试, 公益")
                rule.locator('[data-field="top_n"]').fill("8")
                rule.locator('[data-field="rank_tolerance"]').fill("1")
                rule.locator('[data-weight="price"]').fill("60")
                expect(rule.locator('[data-weight-share="price"]')).to_have_text("50%")
                rule.get_by_role("button", name="恢复默认").click()
                expect(rule.locator('[data-weight="price"]')).to_have_value("40")
                rule.locator('[data-weight="latency"]').fill("0")
                page.locator('button[form="settings-form"]').click()
                expect(page.locator('#settings-dirty')).to_have_text("已保存")
                saved = service.public_config()
                low = saved["stable_groups"][0]
                assert (low["enabled"], low["top_n"], low["score_minutes"], low["rank_tolerance"], low["retry_minutes"],
                        low["weights"]["price"], low["weights"]["latency"]) == (True, 8, 20, 1, 2, 40, 0), low
                assert low["exclude_keywords"] == ["Alpha", "测试", "公益"]
                expect(rule.locator('[data-field="exclude_keywords"]')).to_have_value("Alpha, 测试, 公益")
                assert saved["sync_platform_limits"] == {"openai": 2}, saved["sync_platform_limits"]
                page.screenshot(path=str(artifacts / "settings-selection.png"), full_page=True)
                page.click('[data-page="overview"]')
                page.click('#sync-now')
                expect(page.locator('#stat-active')).to_have_text('3', timeout=15000)
                card = page.locator('[data-key="low_cost:openai"]')
                expect(card).to_contain_text('Claude · 稳定专线')
                expect(card).to_contain_text('查看前 8 名候选评分')
                expect(card).to_contain_text('跌出前 2 名时更换')
                card.locator('summary').click()
                expect(card).to_contain_text('名称命中排除关键字')
                expect(card).to_contain_text('0.2 倍')
                page.wait_for_timeout(4500)  # survives a poll that rebuilds the cards
                expect(card.locator('details')).to_have_attribute('open', '')
                card.get_by_role("button", name="立即检测").click()
                expect(page.locator('#job-dialog')).to_be_visible(timeout=15000)
                expect(page.locator('#job-detail-title')).to_have_text('最低价渠道检测 · OpenAI')
                expect(page.locator('#job-detail-content')).to_contain_text('保持当前渠道')
                page.locator('#job-dialog [data-close]').click()
                assert len(sites.fake.keys) == 10
                report = service.store.get("report")
                report["warnings"] = [{"upstream_id": 99, "warning": "HTTP 429 API_KEY_CREATE_RATE_LIMITED"}]
                service.store.put("report", report)
                expect(page.locator("#sync-notice")).to_contain_text("上游暂时限制新增 Key", timeout=10000)
                assert "[object Object]" not in page.locator("#sync-notice").inner_text()
                page.locator(".model-button").first.click()
                expect(page.locator("#model-list")).to_contain_text("model-a")
                page.fill("#model-search", "nonexistent")
                expect(page.locator("#model-list")).to_contain_text("没有匹配模型")
                page.locator("#group-dialog [data-close]").click()
                page.fill("#group-search", "Alpha")
                expect(page.locator("#group-rows tr")).to_have_count(1)
                page.fill("#group-search", "")
                sites.fake.health = "failed"
                page.click("#sync-now")
                expect(page.locator("#stat-inactive")).to_have_text("11", timeout=15000)
                sites.fake.health = "healthy"
                page.click("#sync-now")
                expect(page.locator("#stat-active")).to_have_text("3", timeout=15000)
                assert len(sites.fake.keys) == 10
                page.click('[data-page="members"]')
                page.click("#refresh-users")
                expect(page.locator("#user-total")).to_have_text("3", timeout=15000)
                page.click("#open-batch")
                page.fill("#batch-text", "alice@example.com, Alice\nnew1@example.com, 新成员一\nnew2@example.com, 新成员二")
                page.fill("#batch-balance", "25")
                page.click("#batch-preview")
                expect(page.locator("#batch-preview-count")).to_have_text("3")
                page.screenshot(path=str(artifacts / "batch.png"), full_page=True)
                page.locator('#batch-form button[type="submit"]').click()
                expect(page.locator("#user-total")).to_have_text("5", timeout=15000)
                expect(page.locator("#job-dialog")).to_be_visible(timeout=15000)
                expect(page.locator("#job-detail-content")).to_contain_text("创建 2 位")
                with page.expect_download() as info:
                    page.locator("#job-dialog a[download]").click()
                info.value.save_as(str(artifacts / "test-members.csv"))
                page.locator("#job-dialog [data-close]").click()
                page.get_by_role("checkbox", name="选择 bob@example.com", exact=True).check()
                page.fill("#quota-amount", "100")
                page.locator("#quota-enabled").check()
                page.locator('#quota-form button[type="submit"]').click()
                expect(page.locator("#quota-next")).not_to_have_text("尚未启用")
                assert sites.fake.users[2]["balance"] == 23
                page.click("#reset-now")
                expect(page.locator("#reset-review")).to_contain_text("1 位成员")
                page.click("#confirm-reset")
                expect(page.locator("#confirm-dialog")).not_to_be_visible()
                expect(page.locator("#user-rows")).to_contain_text("$ 100", timeout=15000)
                assert sites.fake.users[0]["balance"] == 500
                page.screenshot(path=str(artifacts / "members.png"), full_page=True)
                page.click('[data-page="logs"]')
                expect(page.locator("#log-rows")).to_contain_text("余额已重置", timeout=15000)
                assert "test-password" not in page.locator("#log-rows").inner_text()
                page.select_option("#log-level", "ERROR")
                expect(page.locator("#log-rows")).to_contain_text("暂无匹配日志")
                page.select_option("#log-level", "")
                page.screenshot(path=str(artifacts / "logs.png"), full_page=True)
                page.click('[data-page="settings"]')
                page.select_option("#stable-add-platform", "gemini")
                page.select_option("#stable-add-strategy", "best_source")
                page.click("#stable-add")
                best = page.locator('.stable-rule[data-strategy="best_source"][data-platform="gemini"]')
                best.locator('[data-field="keep_top"]').fill("5")
                best.locator('[data-field="check_minutes"]').fill("15")
                page.locator('button[form="settings-form"]').click()
                expect(page.locator('#settings-dirty')).to_have_text("已保存")
                gemini = service.public_config()["stable_groups"][1]
                assert (gemini["platform"], gemini["enabled"], gemini["keep_top"], gemini["check_minutes"]) == ("gemini", True, 5, 15), gemini
                page.screenshot(path=str(artifacts / "settings-stable.png"), full_page=True)
                page.click('[data-page="overview"]')
                card = page.locator('[data-key="best_source:gemini"]')
                card.get_by_role("button", name="立即检测").click()
                expect(page.locator('#job-dialog')).to_be_visible(timeout=15000)
                expect(page.locator('#job-detail-title')).to_have_text('最优源站检测 · Gemini')
                expect(page.locator('#job-detail-content')).to_contain_text('默认排序 #')
                page.locator('#job-dialog [data-close]').click()
                expect(card).to_contain_text('跌出前 5 名时更换')
                expect(card).to_contain_text('GPT · 团队标准')  # fixture names do not follow platforms; group 11 is Gemini
                # Removing a rule stops its local group on the next check.
                page.click('[data-page="settings"]')
                best.get_by_role("button", name="删除").click()
                page.locator('button[form="settings-form"]').click()
                expect(page.locator('#settings-dirty')).to_have_text("已保存")
                assert service.store.get("stable_retired") == ["best_source:gemini"]
                page.click('[data-page="overview"]')
                expect(page.locator('[data-key="best_source:gemini"]')).to_have_count(0)
                service.save_config({"sync_enabled": True})
                page.wait_for_timeout(4500)
                page.locator("#toasts").evaluate("node => node.replaceChildren()")
                page.screenshot(path=str(artifacts / "overview.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                expect(page.locator("#sync-now")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
                page.screenshot(path=str(artifacts / "mobile.png"), full_page=True)
                page.click('[data-page="members"]')
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Members overflow"
                page.screenshot(path=str(artifacts / "mobile-members.png"), full_page=True)
                page.click('[data-page="settings"]')
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Settings overflow"
                expect(page.locator('[data-platform-limit="openai"]')).to_have_value('2')
                page.screenshot(path=str(artifacts / "mobile-settings.png"), full_page=True)
                page.set_viewport_size({"width": 1512, "height": 1000})
                page.click("#logout")
                expect(page.locator("#auth-title")).to_have_text("欢迎回来")
                page.fill("#auth-password", "browser-test-password")
                page.click(".auth-submit")
                expect(page.locator("#workspace")).to_be_visible()
                browser.close()
        finally:
            server.should_exit = True; thread.join(timeout=10); service.close(); sites.close()
        assert not errors, errors
        print(json.dumps({"browser": "Chrome", "result": "passed", "screenshots": str(artifacts),
                          "flows": ["setup/login/logout", "connection tests", "preview/sync", "key retention/recovery",
                                    "model search", "batch create/download", "monthly rule", "selected balance reset", "logs", "mobile"]}, ensure_ascii=False))


if __name__ == "__main__": main()
