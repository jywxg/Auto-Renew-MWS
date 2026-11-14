#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import subprocess
import base64
import urllib.parse
from datetime import datetime, timezone, timedelta
from curl_cffi import requests

# ================= 环境变量配置(可私库直接填写在双引号里) =================
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱仅通知使用，可随意填写
SERVER_IDS    = os.environ.get("SERVER_IDS") or ""      # server id，多个用逗号分隔
COOKIE        = os.environ.get("COOKIE") or ""          # __Host-mrtcloud_token 的值，有效期约1个月，失效后自动用 Discord 登录刷新
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token，COOKIE 失效时用它自动重新登录
GH_TOKEN      = os.environ.get("GH_TOKEN") or ""        # GitHub PAT，用于把新 COOKIE 写回仓库 Secrets（留空则跳过写入）
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG 通知，可选
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG 通知，可选

# ------------- 代理配置 -------------
IS_PROXY     = os.environ.get("IS_PROXY", "false").lower() == "true"   # 是否启用代理, 节点配置NODE_LINK节点自动开启
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1081"
# socks5h 让代理远端解析域名：runner 本机解析可能拿到 IPv6，导致代理链挂起超时；
# 实测 socks5(本地解析) 全灭、socks5h(远端解析) 全通 (2026-10-10)
if PROXY_SERVER.startswith("socks5://"):
    PROXY_SERVER = "socks5h://" + PROXY_SERVER[len("socks5://"):]

# ------------- MWS 站点 / Discord OAuth 配置 -------------
SITE_ORIGIN = "https://cloud.m-ws.cc"          # 控制台前端（续期 API 入口）
API_ORIGIN  = "https://cloud-api.m-ws.cc"      # 后端 auth 所在域
COOKIE_NAME = "__Host-mrtcloud_token"
DISCORD_CLIENT_ID     = "1508034084377464903"
OAUTH_REDIRECT_URI    = f"{API_ORIGIN}/auth/callback"
OAUTH_SCOPE           = "identify"
DISCORD_AUTHORIZE_API = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# ==========================================
def build_proxies():
    """按配置返回 curl_cffi 需要的代理字典，未启用则返回 None"""
    if IS_PROXY:
        return {"http": PROXY_SERVER, "https": PROXY_SERVER}
    return None

def send_tg_notification(message: str):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 TG 通知。")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}

    try:
        resp = requests.post(
            url, json=payload, timeout=10, impersonate="chrome", proxies=build_proxies()
        )
        if resp.status_code == 200:
            print("✅ TG 通知发送成功")
        else:
            print(f"❌ TG 通知发送失败: {resp.text}")
    except Exception as e:
        print(f"❌ TG 通知发送异常: {e}")

def mask_secret(value: str) -> str:
    if not value:
        return "***"
    return value[:4] + "..." + value[-4:] if len(value) > 8 else "***"

def update_github_secret(secret_name: str, new_value: str) -> bool:
    """调用 gh cli 更新仓库 Secrets；未配置 GH_TOKEN 时直接跳过"""
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    if not GH_TOKEN:
        print(f"ℹ️ 未配置 GH_TOKEN，跳过写入 {secret_name}（新值: {mask_secret(new_value)}）")
        return False

    print(f"🔄 更新 Secret: {secret_name} (新值: {mask_secret(new_value)})")
    try:
        env = os.environ.copy()
        env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_value],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
        if proc.returncode == 0:
            print(f"✅ Secret {secret_name} 更新成功")
            return True
        print(f"❌ 更新 Secret {secret_name} 失败: {proc.stderr.strip()}")
        return False
    except Exception as e:
        print(f"❌ 更新 Secret {secret_name} 异常: {e}")
        return False

def login_headers():
    """带 COOKIE 的 API 请求头"""
    return {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "Cookie": f"{COOKIE_NAME}={COOKIE}",
        "Origin": SITE_ORIGIN,
        "Referer": f"{SITE_ORIGIN}/",
    }

def get_expiry_from_cookie(value: str):
    """从 JWT 里解析过期时间（JST 展示），解析失败返回 None"""
    try:
        payload_b64 = value.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not exp:
            return None
        # 站点是日本服务，按 JST (UTC+9) 展示
        return datetime.fromtimestamp(exp, tz=timezone.utc).astimezone(
            timezone(timedelta(hours=9))
        )
    except Exception:
        return None

def renew_server(server_id: str):
    """调用续期 API，返回 (是否成功, HTTP 状态码, 剩余小时数, 错误代码)"""
    url = f"{SITE_ORIGIN}/api/bots/{server_id}/renew"

    try:
        response = requests.post(
            url,
            headers=login_headers(),
            timeout=15,
            impersonate="chrome",
            proxies=build_proxies(),
        )

        if response.status_code == 200:
            try:
                remaining_hours = response.json().get("timer", {}).get("remaining_hours", "未知")
            except json.JSONDecodeError:
                remaining_hours = "未知"

            print(f"✅ 服务器 [{server_id}] 续期成功，剩余 {remaining_hours} 小时")
            return True, response.status_code, remaining_hours, ""

        err_code = ""
        try:
            err_code = response.json().get("code", "") or ""
        except Exception:
            pass
        print(f"❌ 服务器 [{server_id}] 续期失败，状态码: {response.status_code}, 响应: {response.text[:300]}")
        return False, response.status_code, None, err_code

    except Exception as e:
        print(f"❌ 服务器 [{server_id}] 请求发生异常, 请检查服务器是否被删除: {e}")
        return False, None, None, ""

# ================= 浏览器续期（续期接口被 Turnstile 拦截时才用，登录方式不变） =================
# 站点给续期接口加了 Cloudflare Turnstile：纯 HTTP 请求返回 403 captcha_required，
# token 只能由真实浏览器现场生成。做法与 XServer 续期脚本一致：Chromium（Xvfb 有头）
# 注入现有 COOKIE 打开 Bot 详情页，点续期按钮，在弹窗的 Turnstile 小组件里完成验证，
# 由前端自己发出续期请求。COOKIE 的获取/刷新仍走后面的纯 HTTP 流程，本节不碰登录。

TURNSTILE_TOKEN_POLL_SECONDS = int(os.environ.get("TURNSTILE_TOKEN_POLL_SECONDS", "30"))

def _browser_proxy():
    """Chromium 用的代理地址，与 build_proxies 同源。
    DrissionPage 不支持 socks 代理设置；sing-box 本地监听是 mixed 口，
    同一端口可走 HTTP 代理，Chromium 改用 http:// 方案（端口不变）"""
    if not IS_PROXY:
        return ""
    for prefix in ("socks5h://", "socks5://"):
        if PROXY_SERVER.startswith(prefix):
            return "http://" + PROXY_SERVER[len(prefix):]
    return PROXY_SERVER

def _build_browser_options():
    from DrissionPage import ChromiumOptions
    co = ChromiumOptions()
    browser_path = os.getenv("BROWSER_PATH", "/usr/bin/google-chrome")
    if os.path.exists(browser_path):
        co.set_browser_path(browser_path)
    co.set_user_data_path(os.path.abspath("./chrome-profile-mws"))
    # 浏览器一律直连：多次实测直连打开站点正常；走 sing-box 的 http 入口时
    # Chrome 报 ERR_CONNECTION_RESET，DrissionPage 本身也不支持 socks 代理。
    # 接口(HTTP)侧的代理逻辑不受影响，照旧走 socks5h。
    print("🌐 Chrome 直连（不使用代理）")
    # GitHub Actions + Xvfb 下运行有头 Chrome（与 XServer 脚本同一套参数）
    co.set_argument("--window-size=1920,1080")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-gpu")
    co.set_argument("--lang=ja-JP,ja,zh-CN,zh,en-US,en")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--disable-infobars")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--disable-sync")
    return co

def _page_text(page):
    try:
        return page.run_js("return document.body ? document.body.innerText : '';") or ""
    except Exception:
        return ""

def _parse_remaining_hours(text):
    """从详情页文本解析剩余小时数：'剩余 6天 23h' / 'SLEEP IN 167h' / '剩余 167h'"""
    if not text:
        return None
    m = re.search(r"(\d+)\s*天\s*(\d+)\s*h", text)
    if m:
        return int(m.group(1)) * 24 + int(m.group(2))
    m = re.search(r"SLEEP IN\s*(\d+)\s*h", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*d\s*(\d+)\s*h", text)
    if m:
        return int(m.group(1)) * 24 + int(m.group(2))
    m = re.search(r"(\d+)\s*h\s*left", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"剩余\s*(\d+)\s*h", text)
    if m:
        return int(m.group(1))
    return None

def _log_page_diag(page, tag):
    """失败时把浏览器实际看到的页面打进日志：地址、标题、正文前 200 字"""
    try:
        title = page.title or ""
    except Exception:
        title = ""
    text = _page_text(page).replace("\n", " ")[:200]
    try:
        url = page.url
    except Exception:
        url = ""
    print(f"🔎 [{tag}] url={url} title={title} text={text}")

def _renew_button_exists(page):
    """详情页是否已渲染出续期按钮（不点击）"""
    js = r"""
    var btns = document.querySelectorAll('button, a');
    for (var i = 0; i < btns.length; i++) {
        var t = (btns[i].innerText || '').trim();
        if (/重置计时器|续期|延長|リセット|Reset timer|Extend|Renew/i.test(t) && btns[i].offsetParent !== null) return true;
    }
    return false;
    """
    try:
        return bool(page.run_js(js))
    except Exception:
        return False

def _wait_detail_ready(page, timeout=15):
    """等详情页渲染：读到剩余时间或出现续期按钮为止。返回 (剩余小时数, 是否就绪)"""
    deadline = time.time() + timeout
    baseline = None
    while time.time() < deadline:
        baseline = _parse_remaining_hours(_page_text(page))
        if baseline is not None or _renew_button_exists(page):
            return baseline, True
        time.sleep(1)
    return baseline, _renew_button_exists(page)
def _find_turnstile_rect(page):
    """找到 Turnstile 可点击区域，返回视口坐标 [x, y, w, h]；找不到返回 None。
    优先 iframe（checkbox 实际所在），其次 .cf-turnstile 容器，最后带 data-sitekey 的元素。
    （与 XServer 脚本同一套定位链）"""
    js = """
    var el = document.querySelector('.cf-turnstile iframe[src*="challenges.cloudflare.com"]');
    if (!el || el.getClientRects().length === 0) el = document.querySelector('.cf-turnstile iframe');
    if (!el || el.getClientRects().length === 0) el = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (!el || el.getClientRects().length === 0) el = document.querySelector('.cf-turnstile');
    if (!el || el.getClientRects().length === 0) el = document.querySelector('.turnstile-wrapper');
    if (!el || el.getClientRects().length === 0) el = document.querySelector('[data-sitekey]');
    if (!el) return null;
    var r = el.getBoundingClientRect();
    if (!r || r.width === 0 || r.height === 0) return null;
    return [r.left, r.top, r.width, r.height];
    """
    try:
        rect = page.run_js(js)
        if rect and len(rect) == 4:
            return [float(v) for v in rect]
    except Exception:
        pass
    return None

def _get_turnstile_token(page):
    """读取 cf-turnstile-response 隐藏域 token（存在且足够长才算通过）"""
    try:
        token = page.run_js(
            "var i=document.querySelector('input[name=\"cf-turnstile-response\"]');"
            "return (i&&i.value)||'';"
        )
        return token if isinstance(token, str) and len(token) > 20 else ""
    except Exception:
        return ""

def _click_turnstile(page, rect):
    """点击 Turnstile checkbox：取 iframe/容器中心点，而非固定偏移"""
    x, y, w, h = rect
    cx = x + max(20, w / 2)
    cy = y + max(20, h / 2)
    print(f"🎯 点击 Turnstile ({int(cx)},{int(cy)})...")
    page.actions.move_to((cx, cy)).click()
    time.sleep(1)

def _handle_turnstile(page, baseline_hours=None):
    """验证面板已打开后处理 Turnstile：先等自动通过；不行再点复选框并轮询 token，共 3 轮。
    本站面板渲染慢且可能是自动验证：等面板/等 token 期间若剩余时间已涨，说明已自动续上"""
    print("🤖 处理 Turnstile 验证...")
    if _get_turnstile_token(page):
        print("✅ Turnstile 已通过（有 token）")
        return True

    def _renewed():
        if baseline_hours is None:
            return False
        cur = _parse_remaining_hours(_page_text(page))
        return cur is not None and cur > baseline_hours

    # 先等小组件出现/自动通过，最多 25 秒（面板出现偏慢）
    for _ in range(25):
        if _get_turnstile_token(page):
            print("✅ Turnstile 自动通过")
            return True
        if _renewed():
            print("✅ 验证面板自动完成，续期已生效")
            return True
        if _find_turnstile_rect(page):
            break
        time.sleep(1)
    if not _get_turnstile_token(page) and not _find_turnstile_rect(page):
        _turnstile_diag(page)
    for attempt in range(3):
        if _renewed():
            print("✅ 验证面板自动完成，续期已生效")
            return True
        if attempt > 0 and _get_turnstile_token(page):
            # 上一轮点击可能已生效只是 token 来得慢：先确认，避免重复点击已验证的 widget
            print("✅ Turnstile 验证通过")
            return True
        rect = None
        for _ in range(15):
            rect = _find_turnstile_rect(page)
            if rect:
                break
            time.sleep(1)
        if not rect:
            print(f"⚠️ 第 {attempt+1} 次没找到 Turnstile 容器")
            continue
        time.sleep(1)  # 容器出现后再稳 1 秒，等 widget 可交互
        rect = _find_turnstile_rect(page) or rect
        _click_turnstile(page, rect)
        # 轮询等 token：点后启动的可能是慢挑战，统一等满一轮，不中途打断
        for _ in range(TURNSTILE_TOKEN_POLL_SECONDS):
            if _get_turnstile_token(page):
                print("✅ Turnstile 验证通过")
                return True
            if _renewed():
                print("✅ 验证完成，续期已生效")
                return True
            time.sleep(1)
    return bool(_get_turnstile_token(page))

RENEW_TEXT_RE = re.compile(r"重置计时器|续期|延長|リセット|Reset timer|Extend|Renew", re.I)

def _renew_candidates(page):
    """详情页里所有可见的续期按钮元素（页面可能有隐藏的响应式副本，点隐藏的等于没点）"""
    out = []
    try:
        for tag in ("button", "a"):
            for ele in page.eles(f"tag:{tag}"):
                try:
                    t = (ele.text or "").strip()
                    if t and RENEW_TEXT_RE.search(t) and ele.states.is_displayed:
                        out.append((ele, t))
                except Exception:
                    continue
    except Exception:
        pass
    return out

def _dialog_appeared(page):
    """点击后验证面板是否出现：token/容器/弹窗元素，或正文出现验证面板文案。
    本站的验证面板是无 role 的内嵌面板（文案「確認しています…」），且渲染偏慢，
    既不在 iframe 里、也不是 role=dialog，只认文案才不会误判没打开"""
    if _get_turnstile_token(page) or _find_turnstile_rect(page):
        return True
    try:
        if page.run_js("return document.querySelectorAll('[role=\"dialog\"], dialog, iframe').length > 0;"):
            return True
    except Exception:
        pass
    text = _page_text(page)
    return any(k in text for k in ("確認しています", "確認中", "验证中", "驗證中", "Verify you are human", "正在验证"))

def _open_renew_dialog(page):
    """逐个点击可见的续期按钮候选，每点一个就确认弹窗有没有真的打开；
    都不行再用 JS 派发 pointer/mouse/click 全套事件兜底"""
    cands = _renew_candidates(page)
    print(f"🖱 可见续期按钮候选: {len(cands)} 个")
    for ele, t in cands:
        print(f"🖱 点击续期按钮: {t[:50]}")
        try:
            ele.click()
        except Exception:
            continue
        for _ in range(8):
            if _dialog_appeared(page):
                return True
            time.sleep(0.5)
    js = r"""
    var btns = document.querySelectorAll('button, a');
    for (var i = 0; i < btns.length; i++) {
        var t = (btns[i].innerText || '').trim();
        if (/重置计时器|续期|延長|リセット|Reset timer|Extend|Renew/i.test(t) && btns[i].offsetParent !== null) {
            var r = btns[i].getBoundingClientRect();
            var opts = {bubbles: true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, view: window};
            ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function(type) {
                var Ev = (type.indexOf('pointer') === 0 && window.PointerEvent) ? PointerEvent : MouseEvent;
                btns[i].dispatchEvent(new Ev(type, opts));
            });
            return t;
        }
    }
    return '';
    """
    try:
        hit = page.run_js(js)
        if hit:
            print(f"🖱 JS 全事件序列点击: {str(hit)[:50]}")
    except Exception:
        pass
    for _ in range(8):
        if _dialog_appeared(page):
            return True
        time.sleep(0.5)
    return False

def _turnstile_diag(page):
    """Turnstile 容器找不到时，把现场打进日志：页面里所有 iframe、弹窗、turnstile API 是否加载、正文尾部"""
    js = """
    var iframes = Array.prototype.map.call(document.querySelectorAll('iframe'), function(f){ return (f.src || '').slice(0, 90); });
    var dialogs = document.querySelectorAll('[role="dialog"], dialog');
    var dtext = dialogs.length ? (dialogs[0].innerText || '').replace(/\\n/g, ' ').slice(0, 120) : '';
    var tail = (document.body.innerText || '').replace(/\\n/g, ' ').slice(-160);
    return JSON.stringify({iframes: iframes, dialogs: dialogs.length, dialog_text: dtext, turnstile_api: typeof window.turnstile !== 'undefined', body_tail: tail});
    """
    try:
        print(f"🔎 Turnstile 现场: {page.run_js(js)}")
    except Exception as e:
        print(f"🔎 Turnstile 现场读取失败: {e}")

def _wait_renew_outcome(page, baseline_hours, timeout=30):
    """等前端发出续期请求后的结果：剩余时间变多（或满 168h）判成功；失败提示判失败。
    返回 (是否成功, 最新读到的剩余小时数)"""
    deadline = time.time() + timeout
    last = baseline_hours
    while time.time() < deadline:
        text = _page_text(page)
        if any(k in text for k in ("確認に失敗しました", "验证失败", "驗證失敗", "Verification failed")):
            return False, last
        cur = _parse_remaining_hours(text)
        if cur is not None:
            last = cur
            if baseline_hours is not None and cur > baseline_hours:
                return True, cur
            if cur >= 168 and not _find_turnstile_rect(page):
                return True, cur
        time.sleep(2)
    return False, last

def renew_server_browser(server_id: str):
    """浏览器续期单个服务器，返回 (是否成功, 剩余小时数, 失败原因)；
    原因 'auth' 表示 COOKIE 在浏览器里未登录，'' 为一般失败，'captcha' 为验证未过"""
    try:
        from DrissionPage import ChromiumPage
    except Exception:
        print("❌ 未安装 DrissionPage，无法走浏览器续期")
        return False, None, ""
    page = None
    try:
        page = ChromiumPage(_build_browser_options())
        page.get(f"{SITE_ORIGIN}/")
        time.sleep(2)
        page.set.cookies({
            "name": COOKIE_NAME, "value": COOKIE,
            "domain": "cloud.m-ws.cc", "path": "/", "secure": True,
        })
        page.get(f"{SITE_ORIGIN}/bots/{server_id}")
        baseline, _ = _wait_detail_ready(page, timeout=15)
        text = _page_text(page)
        if "/login" in page.url or (
            baseline is None
            and any(k in text for k in ("ログイン", "登录", "Log in"))
        ):
            _log_page_diag(page, server_id)
            print(f"❌ [{server_id}] 浏览器里未登录（COOKIE 可能已失效）")
            return False, None, "auth"
        if baseline is None:
            _log_page_diag(page, server_id)
            print(f"⚠️ [{server_id}] 未读到剩余时间，仍尝试续期流程")
        else:
            print(f"🖥 [{server_id}] 浏览器续期：当前剩余 {baseline} 小时")
        # 预刷新一次再验证（XServer 现行策略：先刷新一遍，坏抽签概率低）
        page.refresh()
        baseline2, _ = _wait_detail_ready(page, timeout=15)
        if baseline2 is not None:
            baseline = baseline2
        for round_no in range(2):
            if round_no == 1:
                print("🔄 第一轮未成功，刷新页面再试一轮...")
                page.refresh()
                _wait_detail_ready(page, timeout=15)
            if not _open_renew_dialog(page):
                _log_page_diag(page, server_id)
                _turnstile_diag(page)
                print(f"❌ [{server_id}] 点了续期但验证弹窗没打开")
                return False, None, ""
            if not _handle_turnstile(page, baseline):
                print(f"❌ [{server_id}] Turnstile 未通过")
                continue
            ok, remaining = _wait_renew_outcome(page, baseline)
            if ok:
                print(f"✅ 服务器 [{server_id}] 续期成功，剩余 {remaining} 小时")
                return True, remaining, ""
            print(f"⚠️ [{server_id}] 验证通过但续期未生效")
        return False, None, "captcha"
    except Exception as e:
        print(f"❌ [{server_id}] 浏览器续期异常: {e}")
        try:
            _log_page_diag(page, server_id)
        except Exception:
            pass
        return False, None, ""
    finally:
        if page is not None:
            try:
                page.quit()
            except Exception:
                pass

# ================= Discord OAuth 登录（API 请求失败时的兜底，纯 HTTP） =================
#   1) GET /auth/login 拿授权 URL（含 state），Session 自动种下 oauth_state cookie
#   2) DISCORD_TOKEN 调 Discord OAuth2 API 授权，拿回带 code 的回调 location
#   3) GET 回调 -> 302 到 /auth/success?code=<一次性code>
#   4) POST /api/auth/exchange 用一次性 code 换取新 __Host-mrtcloud_token

STATE_RE = re.compile(r"[?&]state=([^&]+)")

def discord_authorize(state: str) -> str:
    """用 DISCORD_TOKEN 请求 Discord OAuth2 API 授权，返回带 code 的回调 location"""
    query = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope":         OAUTH_SCOPE,
        "state":         state,
    })
    authorize_url = f"{DISCORD_AUTHORIZE_API}?{query}"
    referer = f"https://discord.com/oauth2/authorize?{query}"

    headers = {
        "accept":           "*/*",
        "authorization":    DISCORD_TOKEN,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "ja",
        "x-super-properties": base64.b64encode(json.dumps({
            "os": "Windows",
            "browser": "Chrome",
            "device": "",
            "system_locale": "ja-JP",
            "browser_version": "140.0.0.0",
            "os_version": "10",
        }).encode()).decode(),
    }

    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })

    data = None

    # 途径1：curl_cffi 直连（CI/有代理环境通常可用）
    try:
        resp = requests.post(
            authorize_url,
            headers=headers,
            data=body,
            timeout=12,
            impersonate="chrome",
            proxies=build_proxies(),
        )
        if resp.status_code == 200:
            data = resp.json()
        else:
            print(f"⚠️ Discord OAuth2 直连返回 HTTP {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"⚠️ Discord OAuth2 直连失败: {e}")

    if not data:
        print("❌ Discord OAuth2 授权失败，请检查 DISCORD_TOKEN 或网络")
        return ""

    location = data.get("location", "")
    if not location:
        print(f"❌ 授权响应没有 location 字段: {data}")
        return ""

    print(f"✅ 拿到回调 URL: {re.sub(r'code=[^&]+', 'code=***', location)}")
    return location

def fetch_new_cookie_via_http() -> str:
    """纯 HTTP 完成 Discord OAuth 授权拿cookie：
    1) GET /auth/login 拿 302 -> discord 授权页 URL（含 state），Session 自动种下 oauth_state cookie
    2) DISCORD_TOKEN 调 Discord OAuth2 API 授权 -> 带 code 的回调 location
    3) GET 回调 -> 302 到 /auth/success?code=<一次性code>（Session 自动带 state cookie）
    4) POST /api/auth/exchange 用一次性 code 换取新 __Host-mrtcloud_token
    curl_cffi 的 chrome TLS 指纹真实，不会被 Cloudflare 掐断。"""
    if not DISCORD_TOKEN:
        print("❌ 未配置 DISCORD_TOKEN，无法自动重新登录")
        return ""

    print("\n🔑 COOKIE 续期失败，切换到Discord OAuth登录获取新的Cookie...")

    try:
        sess = requests.Session(impersonate="chrome")
        if IS_PROXY:
            sess.proxies = build_proxies()

        # 1) 触发 /auth/login，取授权 URL（含后端种下的 state）
        resp = sess.get(f"{API_ORIGIN}/auth/login", timeout=20, allow_redirects=False)
        authorize_page = resp.headers.get("location", "")
        if resp.status_code != 302 or not authorize_page:
            print(f"❌ /auth/login 未返回授权跳转: HTTP {resp.status_code} {resp.text[:150]}")
            return ""
        print(f"✅ 拿到 Discord 授权入口: {re.sub(r'state=[^&]+', 'state=***', authorize_page)}")

        m = STATE_RE.search(authorize_page) or STATE_RE.search(urllib.parse.unquote(authorize_page))
        if not m:
            print("❌ 授权 URL 中未找到 state")
            return ""
        state = urllib.parse.unquote(m.group(1))

        # 2) Discord 侧授权（带 DISCORD_TOKEN 的 API 调用）
        location = discord_authorize(state)
        if not location:
            return ""

        # 3) 打开回调，Session 会自动带上 oauth_state cookie。
        #    成功时后端 302 到 cloud.m-ws.cc/auth/success?code=<一次性code>（不直接发 token）
        print("↩️ 携带授权码请求回调...")
        cb = sess.get(location, timeout=20, allow_redirects=False)
        # print(f"↩️ 回调响应: HTTP {cb.status_code}")

        success_code = ""
        if cb.status_code == 302:
            loc = cb.headers.get("location", "")
            if "/auth/success" in loc:
                mm = re.search(r"[?&]code=([^&]+)", loc)
                if mm:
                    success_code = urllib.parse.unquote(mm.group(1))
            else:
                print(f"   回调跳转到: {loc[:120]}")
        else:
            print(f"   回调返回异常状态: {cb.text[:150]}")

        if not success_code:
            print("❌ 回调未返回 auth/success 换取码（可能 state 校验失败或 code 无效）")
            return ""

        # 4) 模拟前端 auth/success 页面：用一次性 code 调 exchange 换真正的 token
        print("🔄 用一次性 code 调 /api/auth/exchange 换取 token ...")
        ex = sess.post(
            f"{SITE_ORIGIN}/api/auth/exchange",
            json={"code": success_code},
            headers={"Content-Type": "application/json", "Referer": f"{SITE_ORIGIN}/auth/success"},
            timeout=20,
        )
        if ex.status_code != 200:
            print(f"❌ exchange 失败: HTTP {ex.status_code} - {ex.text[:200]}")
            return ""

        token = ""
        for ck in sess.cookies.jar:
            if ck.name == COOKIE_NAME:
                token = ck.value
                break
        if not token:
            set_cookie = ex.headers.get("set-cookie", "")
            mm = re.search(rf"{re.escape(COOKIE_NAME)}=([^;]+)", set_cookie)
            if mm:
                token = urllib.parse.unquote(mm.group(1))

        if not token:
            print(f"❌ exchange 响应未包含 {COOKIE_NAME}（HTTP {ex.status_code}，body: {ex.text[:150]}）")
            return ""

        expiry = get_expiry_from_cookie(token)
        if expiry:
            print(f"📅 新 COOKIE 到期时间: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"✅ HTTP 登录成功，拿到新的 {COOKIE_NAME}: {mask_secret(token)}")
        return token
    except Exception as e:
        print(f"❌ HTTP OAuth 登录异常: {e}")
        return ""

# ================= 主流程 =================
def main():
    global COOKIE

    print("#" * 25)
    print("   MWS 自动续期")
    print("#" * 25)

    server_list = [s_id.strip() for s_id in SERVER_IDS.split(",") if s_id.strip()]
    if not server_list:
        print("❌ 未配置有效的 SERVER_IDS")
        return

    if not COOKIE and not DISCORD_TOKEN:
        print("❌ COOKIE 和 DISCORD_TOKEN 均为空，请添加相关变量后再运行！")
        return

    print(f"🚀 开始续期任务，共 {len(server_list)} 个服务器: {server_list}")
    print(f"🔗 使用代理: {PROXY_SERVER}" if IS_PROXY else "🍭 未使用代理，直连访问")

    if COOKIE:
        expiry = get_expiry_from_cookie(COOKIE)
        if expiry:
            print(f"📅 当前 COOKIE 到期时间: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("ℹ️ 未配置 COOKIE，将直接走 Discord 登录流程")

    # ---------- 第一轮：使用现有 COOKIE 调续期 API ----------
    results = {}      
    all_ok = True
    captcha_pending = []   # 被 Turnstile 拦下的服务器，转第三轮浏览器续期
    auth_failed = False    # 出现过认证失败（COOKIE 可能已失效）
    for s_id in server_list:
        ok, status_code, remaining, err_code = renew_server(s_id)
        results[s_id] = remaining if ok else None
        if not ok:
            if err_code == "captcha_required":
                print(f"🔐 [{s_id}] 续期被 Turnstile 拦截，转浏览器续期")
                captcha_pending.append(s_id)
            elif status_code in (401, 403):
                auth_failed = True
                print(f"⚠️ [{s_id}] 认证失败（HTTP {status_code}），COOKIE 可能已失效")
        time.sleep(2)

    all_ok = all(v is not None for v in results.values())

    new_cookie = ""

    if all_ok:
        print("🏁 全部服务器续期成功，续期任务完成")
    else:
        # ---------- 第二轮：只有认证失败才换 COOKIE（Discord OAuth，纯 HTTP，与原来一致） ----------
        # 被 Turnstile 拦下的不属于 COOKIE 失效，不在这里处理，走后面的浏览器续期
        need_http_retry = auth_failed and any(
            results[s] is None and s not in captcha_pending for s in server_list
        )
        if need_http_retry:
            if not DISCORD_TOKEN:
                print("❌ 存在续期失败的服务器，但未配置 DISCORD_TOKEN，无法自动获取新的Cookie")
                send_tg_notification(
                    "🇯🇵 MWS 续期通知\n\n"
                    f"❌ 续期失败\n👤 账户: {EMAIL}\n"
                    "⚠️ COOKIE 可能已失效，且未配置 DISCORD_TOKEN，无法自动刷新。"
                )
                return

            # 纯 HTTP OAuth（curl_cffi chrome TLS 指纹，无需浏览器）
            new_cookie = fetch_new_cookie_via_http()
            if not new_cookie:
                send_tg_notification(
                    "🇯🇵 MWS 续期通知\n\n"
                    f"❌ 续期失败\n👤 账户: {EMAIL}\n"
                    "⚠️ COOKIE 失效且 Discord OAuth 登录失败，请检查 DISCORD_TOKEN 或代理节点。"
                )
                return

            COOKIE = new_cookie

            # ---------- 用新 COOKIE 重新请求续期 API ----------
            print("\n🔁 使用新 COOKIE 重新请求续期 API ...")
            for s_id in server_list:
                if results[s_id] is not None or s_id in captcha_pending:
                    continue
                print(f"🔁 续期服务器 [{s_id}] ...")
                ok2, _, remaining2, err_code2 = renew_server(s_id)
                if ok2:
                    results[s_id] = remaining2
                elif err_code2 == "captcha_required":
                    captcha_pending.append(s_id)
                time.sleep(2)

        # ---------- 第三轮：浏览器续期（过 Turnstile，登录方式不变） ----------
        if captcha_pending:
            print(f"\n🌐 以下服务器走浏览器续期: {list(captcha_pending)}")
            browser_auth_failed = []
            for s_id in list(captcha_pending):
                ok_b, remaining_b, reason_b = renew_server_browser(s_id)
                if ok_b:
                    results[s_id] = remaining_b
                    captcha_pending.remove(s_id)
                elif reason_b == "auth":
                    browser_auth_failed.append(s_id)
                time.sleep(2)

            # 浏览器里未登录、且本轮还没换过 COOKIE：换一次 COOKIE 再试浏览器续期
            if browser_auth_failed and not new_cookie and DISCORD_TOKEN:
                new_cookie = fetch_new_cookie_via_http()
                if new_cookie:
                    COOKIE = new_cookie
                    for s_id in browser_auth_failed:
                        ok_b, remaining_b, _ = renew_server_browser(s_id)
                        if ok_b:
                            results[s_id] = remaining_b
                            if s_id in captcha_pending:
                                captcha_pending.remove(s_id)
                        time.sleep(2)

        all_ok = all(v is not None for v in results.values())

    # ---------- 最后统一发送一次 TG 通知 ----------
    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    lines = ["🇯🇵 MWS 续期通知\n"]
    if all_ok:
        lines.append("✅ 全部续期成功")
    else:
        lines.append("⚠️ 部分服务器续期失败，请手动检查")
    for s_id, remaining in results.items():
        if remaining is None:
            lines.append(f"🖥 服务器: [{s_id}] 续期失败")
        else:
            lines.append(f"🖥 服务器: [{s_id}] 剩余 {remaining} H")
    lines.append(f"👤 续期账户: {EMAIL}")
    lines.append(f"⏱️ 运行时间: {current_time}")
    send_tg_notification("\n".join(lines))

    # ---------- 续期完成后：把新 COOKIE 写回 GitHub Secret ----------
    if new_cookie:
        if GH_TOKEN:
            update_github_secret("COOKIE", new_cookie)
        else:
            print("ℹ️ 未配置 GH_TOKEN，跳过写入 COOKIE Secret")
            # print(f"📋 如需手动更新，请将 COOKIE 设置为: {new_cookie}")

    print("🏁 脚本执行完毕")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_tg_notification(
            f"🇯🇵 MWS 续期通知\n\n❌ 脚本异常退出\n👤 账户: {EMAIL}\n⚠️ {e}"
        )
