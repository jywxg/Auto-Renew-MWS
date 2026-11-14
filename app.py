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
    """调用续期 API，返回 (是否成功, HTTP 状态码, 剩余小时数)"""
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
            return True, response.status_code, remaining_hours

        print(f"❌ 服务器 [{server_id}] 续期失败，状态码: {response.status_code}, 响应: {response.text[:300]}")
        return False, response.status_code, None

    except Exception as e:
        print(f"❌ 服务器 [{server_id}] 请求发生异常, 请检查服务器是否被删除: {e}")
        return False, None, None

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
    for s_id in server_list:
        ok, status_code, remaining = renew_server(s_id)
        results[s_id] = remaining if ok else None
        if not ok:
            all_ok = False
            if status_code in (401, 403):
                print(f"⚠️ [{s_id}] 认证失败（HTTP {status_code}），COOKIE 可能已失效")
        time.sleep(2)

    new_cookie = ""

    if all_ok:
        print("🏁 全部服务器续期成功，续期任务完成")
    else:
        # ---------- COOKIE失效走第二轮：浏览器登录 Discord 获取新 COOKIE ----------
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
            print(f"🔁 续期服务器 [{s_id}] ...")
            ok2, _, remaining2 = renew_server(s_id)
            results[s_id] = remaining2 if ok2 else None
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
