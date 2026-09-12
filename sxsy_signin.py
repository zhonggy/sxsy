#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尚香书苑 (https://sxsy45.com) 青龙面板每日自动签到 (Cookie 版)
网站登录需要验证码, 故改用浏览器 Cookie 方案: 登录一次复制 Cookie,
脚本带 Cookie 直接打开 dsu_paulsign 签到页, 自动计算"两数相加"验证答案并提交签到。

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
SIGN_URL = BASE + "/plugin.php?id=dsu_paulsign:sign"


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


def check_login(s):
    """验证 Cookie 是否有效, 返回 formhash"""
    r = s.get(BASE + "/index.php", timeout=30)
    if is_cf_challenge(r):
        raise RuntimeError(
            f"访问网站被 Cloudflare 人机验证拦截(HTTP {r.status_code}), "
            "请给青龙配置环境变量 SXTB_PROXY 指向可用代理(如 http://127.0.0.1:7897)")
    if r.status_code != 200:
        raise RuntimeError(f"打开首页异常: HTTP {r.status_code}")

    names = [c.name for c in s.cookies]
    missing = [k for k in ("saltkey", "auth") if not any(k in n for n in names)]
    if missing:
        raise RuntimeError(
            "Cookie 不完整, 缺少关键字段(" + ", ".join(missing) + "): "
            "请复制完整 Cookie 字符串, 必须包含 u52q_2132_auth 和 u52q_2132_saltkey")

    # HTML 属性里 & 会被转义成 &amp;, 统一还原后再匹配
    text = r.text.replace("&amp;", "&")
    if "logging&action=logout" not in text:
        uid = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
        if not uid or uid.group(1) == "0":
            raise RuntimeError(
                "Cookie 无效(未登录状态): Cookie 可能已过期, "
                "请重新从浏览器登录后复制完整 Cookie 更新 SXTB_COOKIE")
        raise RuntimeError("登录态异常, 网站返回异常页面, 请把青龙日志反馈给脚本作者")

    m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', r.text)
    if not m:
        raise RuntimeError("已登录但未获取到 formhash, 网站可能改版")
    return m.group(1)


def solve_math(page):
    """从页面中找出 'x + y' 验证题并返回答案, 找不到返回 None"""
    candidates = re.findall(r"(\d+)\s*\+\s*(\d+)", page)
    if candidates:
        a, b = candidates[0]
        return int(a) + int(b)
    return None


def qiandao(s, say):
    """执行签到, 返回结果描述"""
    r = s.get(SIGN_URL, timeout=30)
    page = r.text.replace("&amp;", "&")

    if "logging&action=logout" not in page:
        raise RuntimeError("Cookie 已失效, 请重新复制 Cookie")

    if re.search(r"已经签到|今日已签|已签到过|明日再来", page):
        return "今日已签到(无需重复)"

    # 定位签到表单
    m = re.search(r"<form[^>]*id=\"signform\"[^>]*>", page)
    if not m:
        msg = strip_tags(page)[:150]
        raise RuntimeError("未找到签到表单: " + msg)

    form_tag = m.group(0)
    end = page.find("</form>", m.end())
    seg = page[m.end():end if end != -1 else m.end() + 3000]

    am = re.search(r"action=\"([^\"]+)\"", form_tag)
    action = am.group(1).replace("&amp;", "&") if am else \
        "plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1&inajax=1"
    if action.startswith("/"):
        action_url = BASE + action
    elif action.startswith("http"):
        action_url = action
    else:
        action_url = BASE + "/" + action

    # 收集表单里已有的 hidden 字段(两种属性顺序都兼容)
    fields = {}
    for tag in re.findall(r"<input[^>]*>", seg):
        nm = re.search(r"name=\"([^\"]+)\"", tag)
        vm = re.search(r"value=\"([^\"]*)\"", tag)
        if nm:
            fields[nm.group(1)] = vm.group(1) if vm else ""

    formhash = fields.get("formhash", "")
    if not formhash:
        hm = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', page)
        formhash = hm.group(1) if hm else ""

    # 心情 qdxq: 下拉框取第一个选项, 否则用 hidden 值
    if "qdxq" not in fields:
        qm = re.search(r"<select[^>]*name=\"qdxq\".*?<option value=\"([^\"]+)\"", seg, re.S)
        if qm:
            fields["qdxq"] = qm.group(1)
        else:
            om = re.search(r"<option value=\"([^\"]+)\"", seg)
            fields["qdxq"] = om.group(1) if om else "1"

    data = {
        "formhash": formhash,
        "qdxq": fields.get("qdxq", "1"),
        "qdmode": fields.get("qdmode", "3"),
        "todaysay": say,
        "fastreply": fields.get("fastreply", "0"),
    }

    # 算术验证: 优先在表单附近找题目, 找不到再全页找
    answer = solve_math(seg)
    if answer is None:
        answer = solve_math(page)
    need_answer = "qdanswer" in page  # 页面带验证输入框说明需要答案
    if need_answer:
        if answer is None:
            raise RuntimeError("需要输入两数之和的验证答案, 但未在页面中找到算式, 请反馈给脚本作者")
        data["qdanswer"] = str(answer)

    r = s.post(action_url, data=data, headers={
        "Referer": SIGN_URL,
        "X-Requested-With": "XMLHttpRequest",
    }, timeout=30)

    body = r.text
    cm = re.search(r"<!\[CDATA\[(.*?)\]\]>", body, re.S)
    msg = strip_tags(cm.group(1) if cm else body)

    if re.search(r"签到成功|恭喜|奖励", msg):
        return "签到成功: " + msg[:150]
    if re.search(r"已经签到|今日已签|明日再来", msg):
        return "今日已签到(重复提交): " + msg[:100]
    if re.search(r"验证|答案", msg) and need_answer:
        raise RuntimeError("验证答案校验未通过, 请反馈页面题目格式: " + msg[:150])
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


def run_account(cookie_str, say):
    s = new_session(cookie_str)
    check_login(s)
    return qiandao(s, say)


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
