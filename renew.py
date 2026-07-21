#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
renew.py — LumixCore 服务器自动续期脚本

配套 .github/workflows/LumixCore.yml 使用。
工作流会通过 `xvfb-run --auto-servernum python renew.py` 调用本脚本，
并注入以下环境变量：
    COOKIE        面板登录后的 Cookie 字符串（浏览器 F12 -> Network -> 复制请求头 Cookie）
    TG_BOT_TOKEN  Telegram 机器人 Token（可选，用于推送结果通知）
    TG_CHAT_ID    Telegram Chat ID（可选）
    PROXY_URL     形如 http://127.0.0.1:1081 的本地代理地址（可选，由工作流里的
                  sing-box / 外部 SOCKS5 步骤设置）

需要按你的面板实际页面结构调整的地方已用 "# TODO" 标注 —— 主要是
“续期”按钮的选择器，以及续期成功/冷却/失败提示的判定文字，因为
这些内容在登录态的 JS 渲染页面里，我这边拿不到真实 DOM，需要你
打开面板按 F12 看一下再改。
"""

import os
import sys
import json
import time
import traceback
from urllib.parse import urlparse

import requests
from seleniumbase import SB

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

RENEW_URL = "https://panel.lumixcore.com/server/0e9c0e74"
PANEL_DOMAIN = urlparse(RENEW_URL).netloc  # panel.lumixcore.com

# 面板前端点击"续期"按钮时，实际调用的接口（抓包确认）
RENEWAL_API_PATH = "/api/client/store/generate-renewal"

COOKIE = os.environ.get("COOKIE", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

PAGE_LOAD_TIMEOUT = 30
API_CALL_TIMEOUT_MS = 20000  # execute_async_script 的超时（毫秒）
MAX_RETRIES = 3
RETRY_DELAY = 8  # 秒

# 接口返回的 body 里出现下列关键字，视为"冷却中/无需续期"，不算错误
COOLDOWN_HINTS = ["already renewed", "cooldown", "please wait", "冷却中", "稍后再试", "not eligible", "too soon"]

# 在浏览器页面上下文里执行的 JS：读取 CSRF token cookie（不同面板命名不一样，
# 这里按常见命名依次尝试），带上对应请求头去 POST 续期接口，
# 最后把 {status, body} 回传给 Python。
# 在浏览器页面上下文里执行的 JS：接收 Python 传进来的 CSRF token（不在页面 JS
# 里读 document.cookie —— 一是有些面板的 token cookie 是 HttpOnly 读不到，
# 二是页面处于跳转/校验等特殊状态时读 document.cookie 会直接报安全错误），
# 只负责发请求，最后把 {status, body} 回传给 Python。
RENEW_FETCH_JS = """
var callback = arguments[arguments.length - 1];
var apiUrl = arguments[0];
var xsrfToken = arguments[1];
fetch(apiUrl, {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'X-XSRF-TOKEN': xsrfToken || ''
    },
    credentials: 'same-origin',
    body: JSON.stringify({})
}).then(function(resp) {
    return resp.text().then(function(text) {
        callback(JSON.stringify({status: resp.status, body: text, tokenUsed: xsrfToken ? 'found' : 'NOT FOUND'}));
    });
}).catch(function(err) {
    callback(JSON.stringify({status: 0, body: String(err)}));
});
"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def notify(message: str) -> None:
    """发送 Telegram 通知；未配置则只打印到日志。"""
    print(message)
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TG_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[WARN] Telegram 通知发送失败: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[WARN] Telegram 通知异常: {e}")


def parse_cookie_header(cookie_str: str) -> list[dict]:
    """把 'a=1; b=2' 形式的 Cookie 请求头字符串解析成 selenium add_cookie 需要的字典列表。"""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": PANEL_DOMAIN,
            "path": "/",
        })
    return cookies


def build_proxy_arg() -> str | None:
    """把 http://127.0.0.1:1081 转成 SeleniumBase 的 proxy 参数格式 127.0.0.1:1081。"""
    if not PROXY_URL:
        return None
    parsed = urlparse(PROXY_URL)
    if parsed.hostname and parsed.port:
        return f"{parsed.hostname}:{parsed.port}"
    # 兜底：如果传进来的本身就是 host:port 形式
    return PROXY_URL.replace("http://", "").replace("https://", "")


CSRF_COOKIE_CANDIDATES = ["X-XSRF-TOKEN", "XSRF-TOKEN", "XSRF_TOKEN", "csrf_token", "CSRF-TOKEN"]


def get_xsrf_token(sb) -> str | None:
    """通过 WebDriver API（而不是页面 JS 的 document.cookie）读取 CSRF token。
    这样即便 token cookie 是 HttpOnly，或者页面处于跳转/校验等特殊状态导致
    document.cookie 被拒绝访问，也不受影响——因为走的是 CDP 层面的接口。
    """
    for name in CSRF_COOKIE_CANDIDATES:
        try:
            cookie = sb.driver.get_cookie(name)
        except Exception:
            cookie = None
        if cookie and cookie.get("value"):
            from urllib.parse import unquote
            print(f"[INFO] 命中 CSRF cookie 字段名: {name}")
            return unquote(cookie["value"])
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_once() -> str:
    """
    执行一次续期流程，返回状态字符串：
        "success"  -> 接口返回成功状态码
        "skipped"  -> 接口返回"冷却中/无需续期"一类的提示，不算失败
    出错（网络异常、接口返回非预期错误码等）直接抛异常，交给外层重试。
    """
    if not COOKIE:
        raise RuntimeError("未设置 COOKIE，环境变量为空")

    proxy_arg = build_proxy_arg()
    api_url = f"https://{PANEL_DOMAIN}{RENEWAL_API_PATH}"

    with SB(uc=True, headless=False, proxy=proxy_arg, page_load_strategy="eager") as sb:
        # 先访问一次目标域名，才能设置该域名下的 Cookie
        sb.open(f"https://{PANEL_DOMAIN}")
        sb.sleep(1)

        for cookie in parse_cookie_header(COOKIE):
            try:
                sb.driver.add_cookie(cookie)
            except Exception as e:
                print(f"[WARN] 添加 Cookie {cookie['name']} 失败: {e}")

        # 带着 Cookie 打开服务器详情页：一是让面板正常建立会话/过 Cloudflare 校验，
        # 二是这个页面才是 CSRF token cookie 的正确 Referer/Origin 来源
        sb.open(RENEW_URL)
        sb.wait_for_ready_state_complete(timeout=PAGE_LOAD_TIMEOUT)
        sb.sleep(3)  # 给前端 SPA 一点渲染/建立会话的时间

        # 调试信息：确认浏览器真的停在了面板页面，而不是被跳转到登录页/验证页
        current_url = sb.get_current_url()
        print(f"[INFO] 当前页面 URL: {current_url}")
        sb.save_screenshot("debug_before_fetch.png")
        if PANEL_DOMAIN not in current_url:
            raise RuntimeError(
                f"页面未停留在面板域名下，当前 URL: {current_url}，"
                f"可能是 Cookie 已失效被跳转到登录页，或被验证页拦截，"
                f"已保存截图 debug_before_fetch.png"
            )

        xsrf_token = get_xsrf_token(sb)
        print(f"[INFO] XSRF token 是否取到: {'found' if xsrf_token else 'NOT FOUND'}")

        # 在页面的 JS 上下文里直接 fetch 续期接口（自动带上 Cookie，token 由
        # Python 传入，不在 JS 里读 document.cookie）
        sb.driver.set_script_timeout(API_CALL_TIMEOUT_MS / 1000)
        raw_result = sb.driver.execute_async_script(RENEW_FETCH_JS, api_url, xsrf_token or "")

        try:
            result = json.loads(raw_result)
        except Exception:
            sb.save_screenshot("renew_result.png")
            raise RuntimeError(f"接口返回内容无法解析: {raw_result!r}")

        status_code = result.get("status", 0)
        body = str(result.get("body", ""))
        body_lower = body.lower()

        print(f"[INFO] 接口返回状态码: {status_code}")
        print(f"[INFO] 接口返回内容: {body[:500]}")

        if any(hint.lower() in body_lower for hint in COOLDOWN_HINTS):
            return "skipped"

        if 200 <= status_code < 300:
            return "success"

        sb.save_screenshot("renew_result.png")
        raise RuntimeError(f"接口返回非成功状态码 {status_code}: {body[:300]}")


def main() -> None:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"[INFO] 第 {attempt}/{MAX_RETRIES} 次尝试...")
            status = run_once()
            if status == "success":
                notify("✅ LumixCore 服务器续期成功")
            elif status == "skipped":
                notify("ℹ️ LumixCore 服务器暂不需要续期（未到期或冷却中）")
            else:
                notify("⚠️ LumixCore 续期结果未知，请人工检查一次")
            return
        except Exception as e:
            last_error = e
            print(f"[ERROR] 第 {attempt} 次尝试失败: {e}")
            traceback.print_exc()
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    notify(f"❌ LumixCore 续期失败（已重试 {MAX_RETRIES} 次）: {last_error}")
    sys.exit(1)


if __name__ == "__main__":
    main()
