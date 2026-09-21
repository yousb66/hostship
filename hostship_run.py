#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import platform
from urllib.parse import urljoin, urlparse

import requests
from seleniumbase import SB

# ================== 环境变量 ==================
HOSTSHIP_BASE = "https://panel.host-ship.com"
HOSTSHIP_LOGIN = f"{HOSTSHIP_BASE}/auth/login"
EMAIL = os.getenv("HOSTSHIP_EMAIL")
PASSWORD = os.getenv("HOSTSHIP_PASSWORD")

# 一个账号下可能有多个服务器；原来的实现只点第一个入口就 break，
# 导致多服务器账号只续期了一台。这里枚举后逐个处理。
MAX_SERVERS = 20          # 防御性上限，避免选择器误匹配导致死循环
# 注意 href 的写法面板可能给 "server/x"、"/server/x" 或完整 URL，
# 因此用 'server/' 而非 '/server/' 匹配，两种情况都能命中。
SERVER_ENTRY_SELECTORS = (
    "a[href*='server/']",
    "a:contains('MANAGE SERVER')",
    "button:contains('MANAGE SERVER')",
    "a:contains('Manage Server')",
    "button:contains('Manage Server')",
)

# ================== 辅助功能 ==================
def is_linux() -> bool:
    return platform.system().lower() == "linux"


def send_tg_notification(message, photo_path=None):
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        if photo_path and os.path.exists(photo_path):
            with open(photo_path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": "Host-Ship 实时画面"},
                    files={"photo": f},
                    timeout=20
                )
    except Exception as e:
        print(f"TG 通知异常: {e}")


def _split_selectors(selector):
    """把 "a:contains('X'), button:contains('Y')" 拆成独立选择器列表。

    SeleniumBase 的 is_element_visible() 接受逗号分隔的多选择器，
    但 find_elements() 不接受（会返回 0 个），因此需要调用方自行拆分。
    """
    return [s.strip() for s in selector.split(",") if s.strip()]


def _visible_elements(sb, selector):
    """返回 selector 命中且当前可见的元素列表（失败返回空列表）。

    UC 模式下 WebElement.is_displayed() 返回 None（不可调用），直接用会
    TypeError，因此可见性一律交给 SeleniumBase 的封装判断，不碰原生方法。
    逐个拆分选择器调用，绕开 find_elements 不支持多选择器的问题。
    """
    out = []
    for one in _split_selectors(selector):
        try:
            if not sb.is_element_visible(one):
                continue
            out.extend(sb.find_elements(one))
        except Exception:
            continue
    return out


def _first_visible_selector(sb, selector):
    """返回第一个可见的单个选择器（供 sb.click/get_text 使用）。"""
    for one in _split_selectors(selector):
        try:
            if sb.is_element_visible(one):
                return one
        except Exception:
            continue
    return None


def _element_label(el):
    """取元素可见文本，作为服务器标识。UC 模式下 .text 可能为 None。"""
    try:
        t = (el.text or "").strip()
        if t:
            return re.sub(r"\s+", " ", t)[:60]
    except Exception:
        pass
    for attr in ("innerText", "textContent"):
        try:
            t = (el.get_attribute(attr) or "").strip()
            if t:
                return re.sub(r"\s+", " ", t)[:60]
        except Exception:
            pass
    return ""


# 卡片文本形如：
#   "gghhh's server # 98a56209 Installing CPU 0% RAM Offline Disk ..."
# 取到第一个状态词为止，只保留服务器名部分，避免通知里塞进整段卡片文字。
_SERVER_LABEL_CUT = re.compile(
    r"\s+(Installing|Online|Offline|Suspended|Running|Stopped|Starting|"
    r"CPU|RAM|Disk)\b",
    re.IGNORECASE)


def _clean_server_label(raw, fallback):
    """从卡片文本里截出服务器名，失败则用 fallback。"""
    if not raw:
        return fallback
    text = re.sub(r"\s+", " ", raw).strip()
    m = _SERVER_LABEL_CUT.search(text)
    if m:
        text = text[:m.start()]
    text = text.strip(" -#")
    if 2 <= len(text) <= 60:
        return text
    return fallback


# ================== 发现服务器 ==================
def discover_servers(sb, dashboard_url):
    """枚举面板上所有服务器入口。

    返回 (targets, selector)：
      targets 是 [{"label":..., "href":...}]，href 可能为 None（纯按钮入口）。
    每次调用前都重新打开 dashboard，保证元素引用新鲜、顺序稳定。
    """
    try:
        sb.open(dashboard_url)
    except Exception:
        sb.uc_open(dashboard_url)
    time.sleep(4)

    for selector in SERVER_ENTRY_SELECTORS:
        els = _visible_elements(sb, selector)
        if not els:
            continue

        targets, seen = [], set()
        for idx, el in enumerate(els):
            try:
                href = el.get_attribute("href")
            except Exception:
                href = None

            # href 可能是相对路径；用它做去重键前先统一成绝对路径，
            # 否则 "server/x" 与 "/server/x" 会被当成两台。
            key = urljoin(dashboard_url, href) if href else f"idx:{idx}"
            if key in seen:          # 同一服务器可能被多个选择器命中，去重
                continue
            seen.add(key)

            fallback = f"服务器#{len(targets) + 1}"
            label = _clean_server_label(_element_label(el), fallback)
            targets.append({"label": label, "href": href})

        if targets:
            print(f"✅ 命中选择器 {selector!r}，发现 {len(targets)} 个服务器入口")
            return targets, selector
    return [], None


# ================== 单台服务器续期 ==================
def renew_server(sb, target, index, dashboard_url):
    """处理一台服务器，返回结果字典（不抛异常，失败也返回状态）。"""
    result = {"index": index, "label": target.get("label") or f"服务器#{index}",
              "days": "未知", "status": "unknown", "detail": ""}
    shot = f"hostship_{index}.png"

    try:
        href = target.get("href")
        if href:
            # 面板给的是相对路径（如 "server/98a56209"）。直接交给 sb.open 时
            # Chrome 会把首段当成主机名，请求打到 http://server/... 上并报
            # ERR_CONNECTION_CLOSED —— 必须先补成绝对 URL。
            url = urljoin(dashboard_url, href)
            sb.open(url)
        else:
            # 没有 href 的入口只能按下标点击；先回 dashboard 再点
            sb.open(dashboard_url)
            time.sleep(3)
            for selector in SERVER_ENTRY_SELECTORS:
                els = _visible_elements(sb, selector)
                if els and index < len(els):
                    els[index].click()
                    break
        time.sleep(5)
    except Exception as e:
        result["status"] = "nav_failed"
        result["detail"] = f"无法打开页面: {e}"
        return result

    # 导航有效性校验：Chrome 的错误页也会被当作正常页面加载，
    # 若不一早识破，后面所有判断都会在错误页上做，得到误导性的 no_button。
    try:
        body = sb.get_text("body") or ""
        landed = sb.get_current_url()
        if ("This site can't be reached" in body
                or "ERR_CONNECTION" in body
                or "ERR_NAME_NOT_RESOLVED" in body):
            result["status"] = "nav_failed"
            result["detail"] = f"页面未加载成功（{landed}）"
            sb.save_screenshot(shot)
            return result
        if urlparse(landed).netloc != urlparse(dashboard_url).netloc:
            # 被重定向到别的域（或仍是相对 URL 造成的错误域），视为失败
            result["status"] = "nav_failed"
            result["detail"] = f"跳转到了异常域名: {landed}"
            sb.save_screenshot(shot)
            return result
    except Exception:
        pass

    # 读续期倒计时。注意 "1 Day" 是合法的单数形式，原正则要求 Days 会漏掉。
    try:
        page_text = sb.get_text("body")
        m = re.search(r"RENEWAL IN\s*(\d+\s*Days?)", page_text, re.IGNORECASE)
        if m:
            result["days"] = re.sub(r"\s+", " ", m.group(1))
    except Exception as e:
        result["detail"] = f"读取倒计时失败: {e}"

    print(f"  📅 [{index}] {result['label']} — 倒计时 {result['days']}")

    # 找续期按钮。sb.click/get_text 只接受单个选择器，先解析出可见的那个。
    renew_selector = "button:contains('Renew'), button:contains('RENEW'), a:contains('Renew')"
    hit = _first_visible_selector(sb, renew_selector)
    if not hit:
        result["status"] = "no_button"
        result["detail"] = "未找到 Renew 按钮"
        sb.save_screenshot(shot)
        return result

    btn_text = ""
    try:
        btn_text = sb.get_text(hit).strip()
    except Exception:
        pass

    if "limit reached" in btn_text.lower():
        result["status"] = "limit_reached"
        result["detail"] = btn_text
        print(f"  ⏳ [{index}] 还没到可续期时间（{btn_text}）")
        sb.save_screenshot(shot)
        return result

    # 点击续期 + 二次确认
    try:
        print(f"  🖱️ [{index}] 点击续期按钮（{btn_text}）")
        sb.click(hit)
        time.sleep(3)
        confirm_selector = ("button:contains('Renew now'), button:contains('Confirm'), "
                            "button.btn-primary")
        confirm_hit = _first_visible_selector(sb, confirm_selector)
        if confirm_hit:
            sb.click(confirm_hit)
            time.sleep(3)
            result["status"] = "renewed"
        else:
            # 没弹窗时，第一次点击本身可能已经生效
            result["status"] = "clicked_no_confirm"
            result["detail"] = "未见二次确认弹窗"
        sb.save_screenshot(shot)
    except Exception as e:
        result["status"] = "click_failed"
        result["detail"] = f"点击异常: {e}"
        try:
            sb.save_screenshot(shot)
        except Exception:
            pass

    return result


# ================== 主流程 ==================
def process_all_renewals(sb, dashboard_url):
    """遍历账号下所有服务器，逐台续期，返回结果列表。"""
    targets, selector = discover_servers(sb, dashboard_url)
    if not targets:
        print("⚠️ 未发现任何服务器入口")
        sb.save_screenshot("hostship_no_server.png")
        send_tg_notification(
            "⚠️ <b>Host-Ship 状态</b>\n登录成功，但未找到 Manage Server 入口。",
            "hostship_no_server.png")
        return []

    print(f"🚀 共 {len(targets)} 台服务器待处理（选择器: {selector}）")
    results = []
    for i, t in enumerate(targets[:MAX_SERVERS]):
        results.append(renew_server(sb, t, i, dashboard_url))

    # 汇总一条通知，避免多服务器时刷屏
    renewed = [r for r in results if r["status"] == "renewed"]
    limits = [r for r in results if r["status"] == "limit_reached"]
    others = [r for r in results if r not in renewed and r not in limits]

    lines = [f"🔁 <b>Host-Ship 续期检查</b>（共 {len(results)} 台）"]
    for r in results:
        icon = {"renewed": "🎉", "limit_reached": "⏳"}.get(r["status"], "⚠️")
        lines.append(f"{icon} {r['label'][:30]} — 剩余 <code>{r['days']}</code>"
                     f"（{r['status']}）")
    message = "\n".join(lines)

    # 有成功续期的，附图；否则附第一台的截图作为凭证
    photo = None
    if renewed:
        photo = f"hostship_{renewed[0]['index']}.png"
    elif results:
        photo = f"hostship_{results[0]['index']}.png"

    print("\n" + message)
    send_tg_notification(message, photo)
    return results


def run():
    if not EMAIL or not PASSWORD:
        print("❌ 错误: 缺少 HOSTSHIP_EMAIL 或 HOSTSHIP_PASSWORD")
        return 1

    sb_kwargs = dict(
        uc=True,
        test=True,
        locale="en",
        headed=not is_linux(),
        chromium_arg="--disable-blink-features=AutomationControlled",
    )

    # ================== 代理（可选） ==================
    # 代理由 workflow 里调用的 setup_proxy.sh 启动（sing-box 监听本地 1080），
    # 成功后它会导出 PROXY_SERVER=socks5://127.0.0.1:1080 与 IS_PROXY=true。
    # 这里直接读回来用，所以换节点只需改 GitHub Secret 的 NODE_LINK，脚本无需改动。
    # 未配置 NODE_LINK 时 PROXY_SERVER 为空 -> 退回直连，不会因为代理缺失而失败。
    proxy = os.getenv("PROXY_SERVER", "").strip()
    if proxy:
        sb_kwargs["proxy"] = proxy
        # 只打印形态，不打印节点信息（本值是本地回环地址，但仍保持最小暴露）
        print("🌐 已启用代理，浏览器流量经本地 socks5 转发")
    else:
        print("ℹ️ 未检测到 PROXY_SERVER，浏览器直连")

    try:
        with SB(**sb_kwargs) as sb:
            print(f"🚀 打开登录页: {HOSTSHIP_LOGIN}")
            sb.uc_open_with_reconnect(HOSTSHIP_LOGIN, reconnect_time=8)
            time.sleep(5)

            user_selector = ("input[name='user'], input[name='username'], "
                             "input[type='text'], input[type='email']")

            if "login" in sb.get_current_url() or sb.is_element_visible(user_selector):
                try:
                    print("📝 正在输入账号密码...")
                    sb.wait_for_element_visible(user_selector, timeout=10)
                    sb.clear(user_selector)
                    sb.type(user_selector, EMAIL)

                    pwd_selector = "input[name='password'], input[type='password']"
                    sb.wait_for_element_visible(pwd_selector, timeout=5)
                    sb.clear(pwd_selector)
                    sb.type(pwd_selector, PASSWORD)
                    time.sleep(1)

                    print("➡️ 寻找并点击 Sign In 按钮...")
                    submit_selector = "button:contains('Sign In'), button[type='submit']"
                    if sb.is_element_visible(submit_selector):
                        sb.click(submit_selector)
                    else:
                        print("未找到 Sign In 按钮，尝试回车提交...")
                        sb.type(pwd_selector, "\n")
                    time.sleep(10)

                except Exception as e:
                    print(f"❌ 登录动作异常: {e}")
                    sb.save_screenshot("hostship_login_error.png")
                    send_tg_notification("❌ <b>Host-Ship 异常</b>\n执行填表时发生错误。",
                                         "hostship_login_error.png")
                    return 1

            print("🔍 检查登录结果...")
            time.sleep(3)
            current_url = sb.get_current_url()

            if "login" in current_url or sb.is_element_visible(user_selector):
                print("❌ 登录失败！账号密码错误或被系统阻断。")
                sb.save_screenshot("hostship_login_failed.png")
                send_tg_notification("❌ <b>Host-Ship 登录失败</b>\n请查看截图排查原因。",
                                     "hostship_login_failed.png")
                return 1

            print(f"✅ 登录成功，当前 URL: {current_url}")
            process_all_renewals(sb, current_url)
            return 0

    except Exception as e:
        print(f"脚本致命异常: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(run())
