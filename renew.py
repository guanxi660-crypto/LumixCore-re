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

COOKIE = os.environ.get("COOKIE", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

PAGE_LOAD_TIMEOUT = 30
CLICK_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_DELAY = 8  # 秒

# 续期按钮可能出现的文案，按顺序尝试（大小写不敏感）
RENEW_BUTTON_TEXTS = ["Renew", "续期", "续费", "Renew Server", "立即续期"]

# 判断“已续期成功 / 无需续期”的页面关键字（出现即视为成功，不再点击）
SUCCESS_HINTS = ["renewed", "续期成功", "expires", "到期时间", "next renewal"]

# 判断“冷却中，暂不能续期”的关键字（视为正常，非错误）
COOLDOWN_HINTS = ["already renewed", "cooldown", "please wait", "冷却中", "稍后再试"]


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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_once() -> str:
    """
    执行一次续期流程，返回状态字符串：
        "success"  -> 点击了续期按钮，判定为成功
        "skipped"  -> 未到续期时间 / 已经是最新状态，无需操作
        "unknown"  -> 页面未识别出明确状态（建议人工检查一次）
    出错时直接抛异常，交给外层重试。
    """
    if not COOKIE:
        raise RuntimeError("未设置 COOKIE，环境变量为空")

    proxy_arg = build_proxy_arg()

    with SB(uc=True, headless=False, proxy=proxy_arg, page_load_strategy="eager") as sb:
        # 先访问一次目标域名，才能设置该域名下的 Cookie
        sb.open(f"https://{PANEL_DOMAIN}")
        sb.sleep(1)

        for cookie in parse_cookie_header(COOKIE):
            try:
                sb.driver.add_cookie(cookie)
            except Exception as e:
                print(f"[WARN] 添加 Cookie {cookie['name']} 失败: {e}")

        # 带着 Cookie 打开真正的续期页面
        sb.open(RENEW_URL)
        sb.wait_for_ready_state_complete(timeout=PAGE_LOAD_TIMEOUT)
        sb.sleep(3)  # 给前端 SPA 一点渲染时间

        page_text = sb.get_text("body").lower()

        # 情况一：页面已经表明续期成功 / 显示了新的到期时间，无需再点
        if any(hint.lower() in page_text for hint in SUCCESS_HINTS):
            print("[INFO] 页面已显示到期时间/续期成功信息，跳过点击")
            return "skipped"

        # 情况二：尝试点击“续期”按钮
        # TODO: 如果下面这几种定位方式都点不到，打开面板按 F12 找到按钮的
        #       id / class / data-* 属性，换成 sb.click("#your-selector")
        clicked = False
        for text in RENEW_BUTTON_TEXTS:
            try:
                if sb.is_text_visible(text, "body"):
                    sb.click(f'button:contains("{text}")', timeout=CLICK_TIMEOUT)
                    clicked = True
                    print(f"[INFO] 已点击按钮: {text}")
                    break
            except Exception:
                continue

        if not clicked:
            # 兜底：尝试常见的按钮 class/id 命名
            fallback_selectors = [
                "button[class*='renew' i]",
                "button[id*='renew' i]",
                "[data-action='renew']",
            ]
            for sel in fallback_selectors:
                try:
                    if sb.is_element_visible(sel):
                        sb.click(sel, timeout=CLICK_TIMEOUT)
                        clicked = True
                        print(f"[INFO] 已通过选择器点击: {sel}")
                        break
                except Exception:
                    continue

        if not clicked:
            sb.save_screenshot("renew_button_not_found.png")
            raise RuntimeError("未找到续期按钮，已保存截图 renew_button_not_found.png，需要人工核对页面结构")

        sb.sleep(3)
        result_text = sb.get_text("body").lower()

        if any(hint.lower() in result_text for hint in COOLDOWN_HINTS):
            return "skipped"

        sb.save_screenshot("renew_result.png")
        return "success"


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
