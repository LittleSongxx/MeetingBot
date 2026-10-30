"""部署后最终验收（Lead 侧）：全路由、移动端、XSS、坏 token、静态头。

跑在修复后的运行栈（http://127.0.0.1:18083）上。
"""

import json
import pathlib
import re
import subprocess

import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18083"
REPO = pathlib.Path(r"\\wsl.localhost\Ubuntu-22.04\home\song\code\Agent\media_agent\MeetingBot")
OUT = pathlib.Path(r"C:\Users\admin\AppData\Local\Temp\fix-backend")
OUT.mkdir(parents=True, exist_ok=True)

env = {}
for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()

ADMIN = {"username": env["APP_ADMIN_USERNAME"], "password": env["APP_ADMIN_PASSWORD"]}
ROUTES = [
    "/manager/home", "/manager/meeting", "/manager/meetingMaterial", "/manager/transcription",
    "/manager/meetingMinutes", "/manager/agent", "/manager/observability", "/manager/aiModel",
    "/manager/promptTemplate", "/manager/user", "/manager/department", "/manager/departmentMembers",
    "/manager/person", "/manager/password",
]

results = {}


def api(method, path, token=None, **kwargs):
    headers = {"token": token} if token else {}
    response = requests.request(method, BASE + "/api" + path, headers=headers, timeout=30, **kwargs)
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:200]}


def main():
    login = api("POST", "/login", json=ADMIN)
    token = login["data"]["token"]

    # 创建一个恶意标题会议，用于 XSS 回归（结束后删除）
    meeting = api("POST", "/meeting/add", token=token, json={
        "title": "<img src=x onerror=alert('xss')> 复核XSS",
        "start_time": "2026-11-01 09:00:00",
        "end_time": "2026-11-01 10:00:00",
        "host_id": login["data"]["id"],
        "participant_ids": [],
    })
    xss_ok = False
    xss_meeting_id = None
    if meeting.get("code") == "200":
        # 查出新会议 ID
        page = api("GET", "/meeting/selectPage?page_num=1&page_size=100", token=token)
        rows = (page.get("data") or {}).get("list") or []
        row = next((item for item in rows if "复核XSS" in (item.get("title") or "")), None)
        if row:
            xss_meeting_id = row["id"]

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="zh-CN")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: errors.append(f"[console.{msg.type}] {msg.text}") if msg.type == "error" else None)

        page.goto(BASE + "/login", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        page.get_by_placeholder("账号").fill(ADMIN["username"])
        page.get_by_placeholder("密码").fill(ADMIN["password"])
        page.get_by_role("button", name="登录").click()
        page.wait_for_url(re.compile(r".*/manager/home"), timeout=30000)
        page.wait_for_timeout(1500)

        titles = {}
        route_errors = {}
        for route in ROUTES:
            before = len(errors)
            page.goto(BASE + route, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            titles[route] = page.title()
            route_errors[route] = errors[before:]

        # XSS 回归：打开会议页，点恶意标题那行的删除，确认框内容应为纯文本
        if xss_meeting_id:
            page.goto(BASE + "/manager/meeting", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.get_by_role("button", name="查询").click()
            page.wait_for_timeout(800)
            rows = page.locator(".el-table__row")
            target = None
            for index in range(rows.count()):
                if "复核XSS" in rows.nth(index).inner_text():
                    target = index
                    break
            if target is not None:
                page.locator(".el-table__row").nth(target).get_by_role("button", name="删除").click()
                page.wait_for_timeout(800)
                dialog = page.locator(".el-message-box")
                xss_ok = dialog.count() > 0 and dialog.locator("img").count() == 0 and "复核XSS" in dialog.inner_text()
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)

        # 坏 token：清 localStorage 后写入坏 token，访问管理端应回登录并清缓存
        page.evaluate(
            "localStorage.setItem('xm-user', JSON.stringify({id: 1, token: 'bad.token'}))"
        )
        page.goto(BASE + "/manager/home", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        bad_token = {
            "url": page.url,
            "xm_user": page.evaluate("localStorage.getItem('xm-user')"),
        }

        # 登录后测移动端 375px：14 路由文档宽度
        page.goto(BASE + "/login", wait_until="domcontentloaded")
        page.wait_for_timeout(600)
        page.get_by_placeholder("账号").fill(ADMIN["username"])
        page.get_by_placeholder("密码").fill(ADMIN["password"])
        page.get_by_role("button", name="登录").click()
        page.wait_for_url(re.compile(r".*/manager/home"), timeout=30000)
        page.wait_for_timeout(1200)
        mobile = {}
        for route in ROUTES:
            page.set_viewport_size({"width": 375, "height": 812})
            page.goto(BASE + route, wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            mobile[route] = page.evaluate("document.documentElement.scrollWidth")
        browser.close()

    # 清理恶意会议（管理员删除）
    if xss_meeting_id:
        api("DELETE", f"/meeting/delete/{xss_meeting_id}", token=token)

    # 静态头检查
    curl = subprocess.run(
        ["curl.exe", "-sI", "-H", "Accept-Encoding: gzip", BASE + "/assets/index-BpezWhk-.js"],
        capture_output=True, text=True,
    ).stdout
    headers = {
        "gzip": "content-encoding: gzip" in curl.lower(),
        "immutable": "immutable" in curl.lower(),
        "csp": "content-security-policy" in curl.lower(),
        "nosniff": "x-content-type-options: nosniff" in curl.lower(),
    }

    results.update(
        {
            "login_code": login.get("code"),
            "route_titles": titles,
            "route_errors": route_errors,
            "xss_ok": xss_ok,
            "bad_token": bad_token,
            "mobile_widths": mobile,
            "static_headers": headers,
        }
    )
    (OUT / "lead-verify.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
