#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尚香书苑 (https://sxsy45.com) 青龙面板每日自动签到 (Cookie 版)
网站使用 Discuz! X3.5 + k_misign(百变每日签到)插件, 登录需要验证码,
故采用浏览器 Cookie 方案: 登录一次复制 Cookie, 脚本带 Cookie 请求
k_misign 的签到接口, 自动计算"两数相加"验证答案并完成签到。

获取 Cookie 方法:
1. 电脑浏览器登录 https://sxsy45.com
2. 按 F12 打开开发者工具 -> Network(网络) -> 刷新页面
3. 点击第一个请求(index.php), 在 Request Headers(请求标头)里找到 Cookie 一行, 复制完整值
   (或 Application -> Cookies, 拼出 "名字=值; 名字=值" 形式)

青龙面板配置:
1. 脚本管理: 新建 sxsy_signin.py, 粘贴本文件内容
2. 环境变量:
   SXTB_COOKIE   完整 Cookie 字符串(多个账号用 & 或换行分隔)
   SXTB_SAY      可选, 签到感言, 默认 "每日打卡"
   TG_BOT_TOKEN  可选, Telegram 机器人 Token (找 @BotFather 创建机器人获取)
   TG_USER_ID    可选, 接收通知的 TG 用户 ID (找 @userinfobot 查询)
   TG_API_HOST   可选, TG API 反代地址, 直连 api.telegram.org 不通时填写
                 (如 https://tg.xx.com, 不带 /sendMessage 后缀)
   SXTB_PROXY    可选, 代理地址, 青龙服务器直连网站被 Cloudflare 拦截时填写
                 (如 http://127.0.0.1:7897)
3. 定时任务: 命令 task sxsy_signin.py, 定时规则 0 8 * * *
依赖: requests (青龙自带)

注意: Cookie 有效期一般几周到几个月(取决于站点设置), 失效后脚本会明确提示,
重新去浏览器复制一份新 Cookie 更新 SXTB_COOKIE 即可。
"""

import html
import os
import re
import sys
import time
import traceback

import requests

BASE = "https://sxsy45.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SIGN_URL = BASE + "/k_misign-sign.html"
QD_URL = (BASE + "/plugin.php?id=k_misign:sign&operation=qiandao"
          "&format=empty&inajax=1&formhash={formhash}")


def notify(title, content):
    """调用青龙自带通知, 失败不影响脚本"""
    try:
        from notify import send  # 青龙内置
        send(title, content)
    except Exception:
        pass


def tg_send(title, content):
    """推送到 Telegram 机器人, 配置了 TG_BOT_TOKEN + TG_USER_ID 才发送"""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    user_id = os.environ.get("TG_USER_ID", "").strip()
    if not token or not user_id:
        print("未配置 TG_BOT_TOKEN / TG_USER_ID, 跳过 TG 推送")
        return
    host = os.environ.get("TG_API_HOST", "").strip().rstrip("/")
    api = (host + "/bot" + token + "/sendMessage") if host \
        else ("https://api.telegram.org/bot" + token + "/sendMessage")
    text = f"{title}\n{content}"
    try:
        r = requests.post(api, json={
            "chat_id": user_id,
            "text": text,
            "disable_web_page_preview": True,
        }, timeout=30)
        if r.status_code == 200 and r.json().get("ok"):
            print("TG 推送成功")
        else:
            print(f"TG 推送失败: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"TG 推送异常: {e}")


def strip_tags(text):
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def new_session(cookie_str):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    # 可选代理: 青龙服务器直连被 Cloudflare 拦截时使用
    proxy = os.environ.get("SXTB_PROXY", "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    # 容错: 去掉误复制的 "Cookie:" 前缀和引号
    cookie_str = re.sub(r"^\s*cookie\s*:\s*", "", cookie_str.strip(), flags=re.I).strip("'\"")
    # 解析 "a=1; b=2" 形式的 Cookie, 逐条写入 session(自动带上 domain/path)
    for kv in cookie_str.replace("\n", ";").split(";"):
        if "=" in kv:
            name, _, value = kv.strip().partition("=")
            if name.strip():
                s.cookies.set(name.strip(), value.strip())
    return s


def is_cf_challenge(r):
    """判断响应是否是 Cloudflare 拦截页(人机验证)"""
    return r.status_code in (403, 503) or "Just a moment" in r.text \
        or "challenges.cloudflare.com" in r.text or "cf-browser-verification" in r.text


def get_uid(page):
    """从页面 JS 变量里取当前用户 ID, 未登录时为 '0' 或不存在"""
    m = re.search(r"discuz_uid\s*=\s*'(\d+)'", page)
    return m.group(1) if m else None


def debug_dump(tag, r):
    """SXTB_DEBUG=1 时把页面存到文件并打印关键信息, 便于排查"""
    if os.environ.get("SXTB_DEBUG", "").strip() != "1":
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"sxsy_debug_{tag}.html")
    with open(path, "w", encoding="utf-8", errors="ignore") as f:
        f.write(r.text)
    print(f"[调试] HTTP {r.status_code} 最终URL: {r.url}")
    print(f"[调试] discuz_uid: {get_uid(r.text)} | 退出链接: {'action=logout' in r.text.replace('&amp;','&')} | 退出文字: {'退出' in r.text}")
    print(f"[调试] 页面已保存: {path}")


def check_login(s):
    """验证 Cookie 是否有效, 返回 formhash"""
    r = s.get(BASE + "/index.php", timeout=30)
    if is_cf_challenge(r):
        raise RuntimeError(
            f"访问网站被 Cloudflare 人机验证拦截(HTTP {r.status_code}), "
            "请给青龙配置环境变量 SXTB_PROXY 指向可用代理(如 http://127.0.0.1:7897)")
    if r.status_code != 200:
        raise RuntimeError(f"打开首页异常: HTTP {r.status_code}")
    debug_dump("index", r)

    names = [c.name for c in s.cookies]
    missing = [k for k in ("saltkey", "auth") if not any(k in n for n in names)]
    if missing:
        raise RuntimeError(
            "Cookie 不完整, 缺少关键字段(" + ", ".join(missing) + "): "
            "请复制完整 Cookie 字符串, 必须包含 u52q_2132_auth 和 u52q_2132_saltkey")

    uid = get_uid(r.text)
    if uid in (None, "0"):
        raise RuntimeError(
            "Cookie 无效(未登录状态): Cookie 可能已过期, "
            "请重新从浏览器登录后复制完整 Cookie 更新 SXTB_COOKIE")
    print(f"登录成功, 用户ID: {uid}")

    m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', r.text)
    if not m:
        raise RuntimeError("已登录但未获取到 formhash, 网站可能改版")
    return m.group(1)


def solve_math(page):
    """从文本中找出 'x op y' 验证题(支持加减乘除)并返回答案, 找不到返回 None"""
    m = re.search(r"(\d+)\s*([+\-×xX*÷])\s*(\d+)", page)
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op in ("×", "x", "X", "*"):
        return a * b
    if b:
        return a // b if a % b == 0 else round(a / b, 2)
    return None


def qiandao(s, formhash, say):
    """执行签到, 返回结果描述"""
    r = s.get(SIGN_URL, timeout=30)
    page = r.text.replace("&amp;", "&")
    debug_dump("sign", r)

    if get_uid(page) in (None, "0"):
        raise RuntimeError("Cookie 已失效, 请重新复制 Cookie")

    # 先探测是否已签到(签到页顶部按钮消失且出现已签字样)
    if re.search(r"k_misign_topb", page) is None and re.search(r"已签到|已经签到|今日已签", page):
        return "今日已签到(无需重复)"

    # k_misign 快速签到接口(同浏览器顶部按钮的 AJAX 请求)
    url = QD_URL.format(formhash=formhash)
    headers = {"Referer": SIGN_URL, "X-Requested-With": "XMLHttpRequest"}
    r = s.get(url, headers=headers, timeout=30)
    debug_dump("qiandao", r)
    body = r.text
    cm = re.search(r"<!\[CDATA\[(.*?)\]\]>", body, re.S)
    msg = strip_tags(cm.group(1) if cm else body)

    if re.search(r"签到成功|获得随机奖励|恭喜", msg):
        return "签到成功: " + msg[:150]
    if re.search(r"今日已签|已经签到|明日再来|已签到", msg):
        return "今日已签到(重复提交): " + msg[:100]

    # k_misign 验证模式: 返回一段 JS prompt 脚本(var q="签到验证：17 - 6 = ?"),
    # 里面带题目和重新提交的地址, 直接从中提取
    qm = re.search(r'var\s+q\s*=\s*["\']([^"\']+)["\']', body)
    if qm:
        qtext = qm.group(1)
        answer = solve_math(qtext)
        if answer is None:
            raise RuntimeError(f"无法计算验证题: {qtext[:80]}")
        print(f"检测到验证题: {qtext} -> 答案: {answer}")

        # 提取脚本里自带的重新提交 URL(答案参数名以脚本实际写法为准)
        retry_urls = []
        for u in re.findall(r"['\"]([^'\"]*k_misign:sign[^'\"]*formhash[^'\"]*)['\"]", body):
            u = u.replace("&amp;", "&")
            if u.startswith("/"):
                u = BASE + u
            elif not u.startswith("http"):
                u = BASE + "/" + u
            if u not in retry_urls:
                retry_urls.append(u)
        if not retry_urls:
            retry_urls = [url]

        last_msg = msg
        for u in retry_urls:
            target = u + str(answer) if re.search(r"[a-z]+=$", u) else u + "&answer=" + str(answer)
            r2 = s.get(target, headers=headers, timeout=30)
            debug_dump("qiandao2", r2)
            raw2 = r2.text
            qm2 = re.search(r'var\s+q\s*=\s*["\']([^"\']+)["\']', raw2)
            if qm2:
                last_msg = "验证答案未通过, 又返回新题目: " + qm2.group(1)
                continue
            # 关键字直接在原始响应里找(strip_tags 会丢掉 script 里的提示文字)
            if re.search(r"签到成功|获得随机奖励|恭喜", raw2):
                return "签到成功: " + (strip_tags(raw2) or raw2)[:150]
            if re.search(r"今日已签|已经签到|明日再来|已签到", raw2):
                return "今日已签到(重复提交): " + strip_tags(raw2)[:100]
            last_msg = strip_tags(raw2) or re.sub(r"\s+", " ", raw2)[:120]

        # 状态复核: 重新请求签到接口, 有些成功响应只返回空的JS脚本,
        # 以签到状态为准而不是提交响应的内容
        r3 = s.get(url, headers=headers, timeout=30)
        debug_dump("qiandao3", r3)
        raw3 = r3.text
        if re.search(r"今日已签|已经签到|已签到", raw3):
            detail = fetch_reward(s)
            return "签到成功" + (f"（{detail}）" if detail else "") + "(复核确认)"
        if re.search(r"签到成功|获得随机奖励|恭喜", raw3):
            detail = fetch_reward(s) if not re.search(r"获得随机奖励", raw3) else ""
            return "签到成功: " + strip_tags(raw3)[:150] + (f"（{detail}）" if detail else "")
        qm3 = re.search(r'var\s+q\s*=\s*["\']([^"\']+)["\']', raw3)
        if qm3:
            raise RuntimeError("验证答案提交后仍未通过(网站又返回新题目), 请开启 SXTB_DEBUG=1 查看 sxsy_debug_qiandao2.html")
        raise RuntimeError("验证答案提交后状态未知, 网站返回: " + (re.sub(r"\s+", " ", raw3)[:150]))

    raise RuntimeError("签到结果未知: " + (msg[:150] or body[:150]))


def format_result(res):
    """把脚本内部结果转成通知用的简洁样式: ✅/☑️/❌ + 文案"""
    if res.startswith("签到成功"):
        rewards = re.findall(r"(金币|金钱|威望|贡献)\s*(\d+)", res)
        detail = "，".join(f"{name} {num}" for name, num in rewards)
        return "✅ 签到成功" + (f"（{detail}）" if detail else "")
    if res.startswith("今日已签到"):
        return "☑️ 今日已签到"
    if res.startswith("失败"):
        return "❌ " + res
    return res


def fetch_reward(s):
    """签到成功后从签到页抓取本次奖励明细(如 金币 20, 威望 5)"""
    try:
        p = s.get(SIGN_URL, headers={"Referer": BASE + "/index.php"}, timeout=30)
        page = p.text.replace("&amp;", "&")
        seg = page
        mm = re.search(r"奖励", page)
        if mm:  # 只看"奖励"附近的文字, 避免误抓页面上的积分总额
            seg = page[max(0, mm.start() - 50): mm.end() + 300]
        rewards = re.findall(r"(金币|金钱|威望|贡献)\s*[+:：=]?\s*(\d+)", seg)
        if rewards:
            return "，".join(f"{n} {v}" for n, v in rewards)
        sm = re.search(r"获得随机奖励([^<\n]{0,60})", page)
        if sm:
            return re.sub(r"\s+", " ", sm.group(1)).strip()
    except Exception:
        pass
    return ""


def run_account(cookie_str, say):
    s = new_session(cookie_str)
    formhash = check_login(s)
    return qiandao(s, formhash, say)


def main():
    cookies = os.environ.get("SXTB_COOKIE", "").strip()
    say = os.environ.get("SXTB_SAY", "每日打卡").strip()

    if not cookies:
        print("请先在青龙面板配置环境变量 SXTB_COOKIE(浏览器登录后复制的完整 Cookie)")
        sys.exit(1)

    cookie_list = [c.strip() for c in re.split(r"[&\n]", cookies) if c.strip()]

    lines = []
    ok_count = 0
    for i, ck in enumerate(cookie_list, 1):
        multi = len(cookie_list) > 1
        print(f"---- 账号{i} ----")
        try:
            res = run_account(ck, say)
            ok_count += 1
            print(f"[成功] {res}")
            lines.append(f"账号{i}: {format_result(res)}" if multi else format_result(res))
        except Exception as e:
            print(f"[失败] {e}")
            line = f"❌ 账号{i}: {e}" if multi else f"❌ {e}"
            lines.append(line)
        if i < len(cookie_list):
            time.sleep(5)

    title = "尚香书苑"
    content = "\n".join(lines)
    print("\n" + title + "\n" + content)
    tg_send(title, content)
    notify(title, content)
    sys.exit(0 if ok_count == len(cookie_list) else 1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        tg_send("尚香书苑签到异常", "脚本运行异常, 请查看青龙日志")
        notify("尚香书苑签到异常", "脚本运行异常, 请查看青龙日志")
        sys.exit(1)
