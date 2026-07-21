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

思路：
    1. 用真实浏览器（SeleniumBase）打开面板页面，负责过 Cloudflare 之类的
       人机校验、建立正常会话，顺带拿到浏览器实际持有的全部 Cookie。
    2. 把这些 Cookie（以及浏览器的 User-Agent、代理设置）原样搬到 Python 的
       requests.Session 里，真正的续期请求用 requests 直接发送 —— 这样出错信息
       是标准的 HTTP 状态码/异常，比在浏览器 JS 里排查要清楚得多。
"""

import os
import sys
import json
import time
import traceback
from urllib.parse import urlparse, unquote

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
MAX_RETRIES = 3
RETRY_DELAY = 8  # 秒

# 接口返回的 body 里出现下列关键字，视为"冷却中/无需续期"，不算错误
COOLDOWN_HINTS = ["already renewed", "cooldown", "please wait", "冷却中", "稍后再试", "not eligible", "too soon"]

# CSRF token 常见的 cookie 命名（不同面板/框架习惯不一样，按顺序依次尝试）
CSRF_COOKIE_CANDIDATES = ["X-XSRF-TOKEN", "XSRF-TOKEN", "XSRF_TOKEN", "csrf_token", "CSRF-TOKEN", "_csrf"]


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
    """支持两种格式：
    1. 浏览器 Network 面板的 Cookie 请求头字符串：'a=1; b=2'
    2. 浏览器插件导出的 JSON 数组：[{"name": "...", "value": "...", ...}, ...]
    """
    cookie_str = cookie_str.strip()
    cookies = []

    if cookie_str.startswith("["):
        try:
            items = json.loads(cookie_str)
        except Exception as e:
            print(f"[WARN] COOKIE 内容看起来像 JSON 数组，但解析失败: {e}")
            return []
        for item in items:
            name = item.get("name")
            value = item.get("value")
            if name and value is not None:
                cookies.append({"name": name, "value": value})
        return cookies

    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({"name": name.strip(), "value": value.strip()})
    return cookies


def build_proxy_arg() -> str | None:
    """把 http://127.0.0.1:1081 转成 SeleniumBase 的 proxy 参数格式 127.0.0.1:1081。"""
    if not PROXY_URL:
        return None
    parsed = urlparse(PROXY_URL)
    if parsed.hostname and parsed.port:
        return f"{parsed.hostname}:{parsed.port}"
    return PROXY_URL.replace("http://", "").replace("https://", "")


def normalize_proxy_for_requests(proxy_url: str) -> str:
    """requests/urllib3 不认识裸的 'socks://' scheme，必须是 socks5:// 或 socks4://。
    这里把常见的 'socks://' 统一改成 'socks5://'，其余 scheme（http/https/socks5/socks4）原样返回。
    """
    if proxy_url.startswith("socks://"):
        fixed = "socks5://" + proxy_url[len("socks://"):]
        print(f"[INFO] 代理 scheme 'socks://' 不被 requests 支持，已自动转换为 'socks5://'")
        return fixed
    return proxy_url


def find_csrf_token(cookie_dict: dict) -> str | None:
    """在浏览器实际持有的 cookie 字典里找 CSRF token：
    先按已知命名精确匹配，找不到再退化为"名字里包含 xsrf/csrf"的模糊匹配。
    """
    for name in CSRF_COOKIE_CANDIDATES:
        if name in cookie_dict and cookie_dict[name]:
            print(f"[INFO] 命中 CSRF cookie 字段名: {name}")
            return unquote(cookie_dict[name])
    for name, value in cookie_dict.items():
        if value and ("xsrf" in name.lower() or "csrf" in name.lower()):
            print(f"[INFO] 模糊匹配到 CSRF cookie 字段名: {name}")
            return unquote(value)
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

    parsed_cookies = parse_cookie_header(COOKIE)
    print(f"[INFO] 从 COOKIE 环境变量解析到 {len(parsed_cookies)} 个字段: {[c['name'] for c in parsed_cookies]}")
    if not parsed_cookies:
        raise RuntimeError(
            "COOKIE 环境变量内容非空，但按 'name=value; name2=value2' 格式解析出 0 个字段，"
            "请确认 Secret 里存的是完整的 Cookie 请求头字符串，而不是单个 token 值或其他内容"
        )

    proxy_arg = build_proxy_arg()
    api_url = f"https://{PANEL_DOMAIN}{RENEWAL_API_PATH}"

    # ----- 第一步：用真实浏览器建立会话，拿到完整 Cookie 和 User-Agent -----
    with SB(uc=True, headless=False, proxy=proxy_arg, page_load_strategy="eager") as sb:
        sb.open(f"https://{PANEL_DOMAIN}")
        sb.sleep(1)

        for cookie in parsed_cookies:
            try:
                sb.driver.add_cookie({**cookie, "domain": PANEL_DOMAIN, "path": "/"})
            except Exception as e:
                print(f"[WARN] 添加 Cookie {cookie['name']} 失败: {e}")

        sb.open(RENEW_URL)
        sb.wait_for_ready_state_complete(timeout=PAGE_LOAD_TIMEOUT)
        sb.sleep(3)  # 给前端 SPA 一点渲染/建立会话的时间

        current_url = sb.get_current_url()
        print(f"[INFO] 当前页面 URL: {current_url}")
        sb.save_screenshot("debug_before_fetch.png")
        if PANEL_DOMAIN not in current_url:
            raise RuntimeError(
                f"页面未停留在面板域名下，当前 URL: {current_url}，"
                f"可能是 Cookie 已失效被跳转到登录页，或被验证页拦截，"
                f"已保存截图 debug_before_fetch.png"
            )

        all_cookies = sb.driver.get_cookies()
        cookie_dict = {c["name"]: c["value"] for c in all_cookies}
        print(f"[INFO] 浏览器当前持有的 Cookie 字段: {sorted(cookie_dict.keys())}")

        user_agent = sb.driver.execute_script("return navigator.userAgent;")

    # ----- 第二步：浏览器关闭后，用 requests 复用这些 Cookie 直接发续期请求 -----
    session = requests.Session()
    for name, value in cookie_dict.items():
        session.cookies.set(name, value, domain=PANEL_DOMAIN, path="/")

    if PROXY_URL:
        proxy_for_requests = normalize_proxy_for_requests(PROXY_URL)
        session.proxies = {"http": proxy_for_requests, "https": proxy_for_requests}

    xsrf_token = find_csrf_token(cookie_dict)
    print(f"[INFO] XSRF token 是否取到: {'found' if xsrf_token else 'NOT FOUND'}")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": RENEW_URL,
        "Origin": f"https://{PANEL_DOMAIN}",
        "User-Agent": user_agent or "Mozilla/5.0",
    }
    if xsrf_token:
        headers["X-XSRF-TOKEN"] = xsrf_token

    try:
        resp = session.post(api_url, headers=headers, json={}, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"请求续期接口时发生网络错误: {e}")

    status_code = resp.status_code
    body = resp.text
    body_lower = body.lower()

    print(f"[INFO] 接口返回状态码: {status_code}")
    print(f"[INFO] 接口返回内容: {body[:500]}")

    if any(hint.lower() in body_lower for hint in COOLDOWN_HINTS):
        return "skipped"

    if 200 <= status_code < 300:
        return "success"

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
