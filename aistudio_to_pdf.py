#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Studio 对话导出 PDF 工具
============================
把 Google AI Studio (https://aistudio.google.com) 的对话导出为排版好的 PDF。

用法:
    python aistudio_to_pdf.py <对话链接> [更多链接...]
    python aistudio_to_pdf.py https://aistudio.google.com/prompts/xxxxxxxx

可选参数:
    -o, --out FILE      指定输出 PDF 路径 (仅传一个链接时有效)
    --profile DIR       浏览器配置目录 (保存登录状态, 默认 ~/.aistudio_profile)
    --timeout SECONDS   等待登录/加载的超时秒数 (默认 300)
    --channel BROWSER   浏览器通道: msedge / chrome / chromium (默认 msedge)
    --keep-raw          同时保存抓到的原始 JSON 数据
    --no-thinking       PDF 中不包含 AI 思考过程

说明:
    * 首次运行会弹出浏览器窗口, 请手动登录一次 Google 账号;
      登录状态会保存在 --profile 目录, 之后运行无需再登录。
    * 支持多账号: 若对话属于另一个账号, 按提示在浏览器里切换即可。
    * 支持一次传多个链接, 批量导出多个 PDF。
    * 同时会生成同名 .html 文件 (排版预览用, 不需要可删)。
"""

import argparse
import html as html_mod
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import markdown as md_lib
    from playwright.sync_api import sync_playwright
except ImportError as e:
    print(f"缺少依赖库: {e.name}", file=sys.stderr)
    print("请先运行以下命令安装依赖, 然后重新运行本脚本:", file=sys.stderr)
    print("    python -m pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

DEFAULT_PROFILE = Path.home() / ".aistudio_profile"


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- 数据抓取

def _collect_response(resp, captured):
    try:
        if resp.status != 200:
            return
        if resp.request.resource_type not in ("xhr", "fetch"):
            return
        ct = (resp.headers or {}).get("content-type", "")
        if "json" not in ct and "text/plain" not in ct and "event-stream" not in ct:
            return
        cl = (resp.headers or {}).get("content-length")
        if cl and cl.isdigit() and int(cl) > 50_000_000:
            return
        captured.append(resp)
    except Exception:
        pass


def _response_payload(resp):
    """读取响应体并解析为 JSON, 失败返回 None。"""
    try:
        body = resp.text()
    except Exception:
        return None
    if not body or len(body) < 500:
        return None
    t = body.lstrip()
    if t.startswith(")]}'"):
        t = t[4:].lstrip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # SSE 流: 逐行解析 "data: {json}", 拼接多个事件对象 (Claude 消息流式返回)
    objs = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            d = line[5:].strip()
            if d and d != "[DONE]":
                try:
                    objs.append(json.loads(d))
                except Exception:
                    pass
    if objs:
        return objs if len(objs) > 1 else objs[0]
    return None


def extract_turns(data):
    """优先按 Claude 的结构解析 (human/assistant + content 块), 失败再按 AI Studio 结构解析。"""
    turns = _extract_claude_turns(data)
    if not turns:
        turns = _extract_ai_studio_turns(data)
    return _dedup_consecutive(turns)


def _dedup_consecutive(turns):
    """只去掉连续重复, 不误删真实重复提问。"""
    out = []
    for t in turns:
        if out and out[-1]["role"] == t["role"] and out[-1]["text"] == t["text"]:
            continue
        out.append(t)
    return out


def _extract_claude_turns(data):
    """解析 claude.ai 的消息结构:
    {"sender"/"role": "human"|"assistant", "content": [{"type": "text"|"thinking", ...}]}
    thinking 块单独成轮, 便于按 --no-thinking 过滤。"""
    turns = []

    def walk(o):
        if isinstance(o, dict):
            sender = o.get("sender") or o.get("role")
            content = o.get("content")
            if sender in ("human", "assistant") and isinstance(content, list):
                if sender == "human":
                    texts = [b["text"].strip() for b in content
                             if isinstance(b, dict) and b.get("type") == "text"
                             and isinstance(b.get("text"), str) and b["text"].strip()]
                    if texts:
                        turns.append({"role": "user", "text": "\n\n".join(texts)})
                else:
                    thinks = [b["thinking"].strip() for b in content
                              if isinstance(b, dict) and b.get("type") == "thinking"
                              and isinstance(b.get("thinking"), str)
                              and b["thinking"].strip()]
                    if thinks:
                        turns.append({"role": "model", "kind": "thinking",
                                      "text": "\n\n".join(thinks)})
                    texts = [b["text"].strip() for b in content
                             if isinstance(b, dict) and b.get("type") == "text"
                             and isinstance(b.get("text"), str) and b["text"].strip()]
                    if texts:
                        turns.append({"role": "model", "kind": "reply",
                                      "text": "\n\n".join(texts)})
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    return turns


def _extract_ai_studio_turns(data):
    """保留原 AI Studio 解析逻辑 (user/model 列表结构)。"""
    turns = []

    def walk(o):
        if isinstance(o, list):
            role = next((x for x in o if x in ("user", "model")), None)
            if role is not None:
                text = next(
                    (x for x in o
                     if isinstance(x, str) and len(x) > 15
                     and x not in ("user", "model")
                     and not x.startswith("models/")
                     and not x.startswith("prompts/")),
                    None)
                if text:
                    turns.append({"role": role, "text": text})
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(data)

    if not turns:
        def walk_dict(o):
            if isinstance(o, dict):
                role = o.get("role")
                if role in ("user", "model"):
                    parts = o.get("parts") if isinstance(o.get("parts"), list) else [o.get("content")]
                    texts = [p.get("text") for p in parts
                             if isinstance(p, dict) and isinstance(p.get("text"), str) and p.get("text")]
                    if texts:
                        turns.append({"role": role, "text": "\n".join(texts)})
                for v in o.values():
                    walk_dict(v)
            elif isinstance(o, list):
                for x in o:
                    walk_dict(x)
        walk_dict(data)

    return turns


def _walk_str_key(o, key, min_len=1, max_len=1000, prefix=None):
    """深度遍历, 收集指定 key 下符合条件的字符串值 (支持 {"model": {"name": ...}} 嵌套)。"""
    found = []

    def walk(x):
        if isinstance(x, dict):
            v = x.get(key)
            if isinstance(v, str) and min_len <= len(v.strip()) <= max_len:
                if prefix is None or v.startswith(prefix):
                    found.append(v.strip())
            elif isinstance(v, dict) and isinstance(v.get("name"), str):
                n = v["name"].strip()
                if min_len <= len(n) <= max_len and (prefix is None or n.startswith(prefix)):
                    found.append(n)
            for c in x.values():
                walk(c)
        elif isinstance(x, list):
            for c in x:
                walk(c)

    walk(o)
    return found


def extract_title(data):
    """从响应中提取对话标题 (Claude 的 "title" 字段或 AI Studio 的 prompts/ 结构)。"""
    found = []

    def walk(o):
        if isinstance(o, list) and o and isinstance(o[0], str) and o[0].startswith("prompts/"):
            for c in o:
                if (isinstance(c, list) and c and isinstance(c[0], str)
                        and 0 < len(c[0]) < 200
                        and any(isinstance(x, list) and x and isinstance(x[0], str)
                                for x in c[1:])):
                    found.append(c[0])
            return
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(data)
    if not found:
        found = _walk_str_key(data, "title", 3, 200)
    return found[0] if found else None


def extract_model_name(data):
    result = []

    def walk(o):
        if isinstance(o, str) and o.startswith("models/") and len(o) < 100:
            result.append(o.split("/", 1)[1])
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(data)
    if not result:
        for v in _walk_str_key(data, "model", 1, 100, prefix="claude"):
            result.append(v.split("/", 1)[1] if "/" in v else v)
    return result[0] if result else None


def capture_conversation(ctx, url, timeout_s):
    """打开链接并等待对话数据接口响应, 返回 (payload, dom_title, closed)。

    支持多账号: 若当前登录账号不是对话主人, 提示用户在浏览器窗口里
    切换/添加账号, 切换后页面会自动加载对话并被捕获。
    """
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    captured = []
    ctx.on("response", lambda r: _collect_response(r, captured))

    log(f"打开链接: {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except Exception:
        log("页面加载较慢, 继续等待…")

    deadline = time.time() + timeout_s
    start = time.time()
    best_payload, best_turns = None, []
    parsed_idx, found_time = 0, None
    login_hinted = switch_hinted = False
    next_status = start + 15

    while time.time() < deadline:
        while parsed_idx < len(captured):
            resp = captured[parsed_idx]
            parsed_idx += 1
            payload = _response_payload(resp)
            if payload is None:
                continue
            turns = extract_turns(payload)
            if len(turns) > len(best_turns):
                best_payload, best_turns = payload, turns
        if best_turns and len(best_turns) >= 2 and any(t["role"] == "user" for t in best_turns):
            if found_time is None:
                found_time = time.time()
                log(f"已捕获对话数据: {len(best_turns)} 轮")
            if time.time() - found_time > 4:  # 多等几秒, 取轮数最多的响应
                break
        else:
            try:
                cur_url = page.url
            except Exception:
                cur_url = ""
            if any(h in cur_url for h in ("accounts.google.com", "claude.ai/login",
                                          "claude.ai/oath", "aistudio.google.com/login")) \
                    and not login_hinted:
                log("需要登录: 请在浏览器窗口中登录对话所属的账号 (Claude / Google), 完成后自动继续。")
                login_hinted = True
            elif (time.time() - start > 15
                  and any(h in cur_url for h in ("claude.ai", "aistudio.google.com"))
                  and not switch_hinted):
                log("页面已打开但未捕获到对话数据: 可能未登录、当前账号无权访问该对话, 或数据仍在加载。")
                log("  → 请在弹出的浏览器里登录对话所属的账号 (或切换账号), 完成后脚本自动继续。")
                switch_hinted = True
            if time.time() >= next_status:
                remain = int(deadline - time.time())
                log(f"  等待中… 剩余 {remain // 60} 分 {remain % 60:02d} 秒")
                next_status = time.time() + 20
        try:
            page.wait_for_timeout(400)
        except Exception:
            live = list(ctx.pages)
            if live:
                page = live[0]  # 切换账号可能关闭了原标签页, 换到存活的标签页继续
            else:
                log("浏览器窗口被关闭。")
                return None, None, True

    dom_title = None
    try:
        dom_title = page.evaluate(
            "() => (document.querySelector('h1') && document.querySelector('h1').innerText.trim())"
            " || document.title || ''")
    except Exception:
        pass
    return best_payload, dom_title, False


# ---------------------------------------------------------------- 解析与分类

def classify_turns(turns):
    """连续的 model 段落中, 除最后一条外都是思考过程。
    若轮次已带 kind (Claude 解析), 保留原样并补全缺失。"""
    if any("kind" in t for t in turns):
        for t in turns:
            if "kind" not in t:
                t["kind"] = "reply" if t["role"] == "model" else "user"
        return turns
    i = 0
    while i < len(turns):
        if turns[i]["role"] == "model":
            j = i
            while j < len(turns) and turns[j]["role"] == "model":
                j += 1
            for k in range(i, j):
                turns[k]["kind"] = "thinking" if k < j - 1 else "reply"
            i = j
        else:
            turns[i]["kind"] = "user"
            i += 1
    return turns


# ---------------------------------------------------------------- HTML / PDF

CSS = """
@page { size: A4; }
body { font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", "Noto Sans CJK SC", sans-serif;
       font-size: 11pt; line-height: 1.8; color: #1a1a1a; }
.cover { text-align: center; padding: 50px 0 30px; border-bottom: 3px solid #4285f4; margin-bottom: 28px; }
.cover h1 { font-size: 24pt; color: #1a73e8; margin: 0 0 10px; }
.cover .subtitle { font-size: 13pt; color: #666; margin-bottom: 8px; }
.cover .meta { font-size: 9.5pt; color: #999; }
.cover .meta a { color: #1a73e8; text-decoration: none; word-break: break-all; }
.turn { margin-bottom: 22px; page-break-inside: avoid; }
.turn-header { font-weight: bold; font-size: 10pt; padding: 4px 10px; border-radius: 4px;
               display: inline-block; margin-bottom: 8px; }
.turn.user .turn-header { background: #e8f0fe; color: #1a73e8; border-left: 4px solid #1a73e8; }
.turn.model .turn-header { background: #e6f4ea; color: #137333; border-left: 4px solid #137333; }
.turn.thinking .turn-header { background: #fef7e0; color: #b06000; border-left: 4px solid #f9ab00; }
.turn.thinking .turn-content { color: #5f6368; font-style: italic; background: #fffbeb;
                               padding: 10px 14px; border-radius: 4px; font-size: 10pt; }
.turn-content { padding: 0 4px; }
.turn-content h1 { font-size: 15pt; color: #1a73e8; margin: 12px 0 6px; }
.turn-content h2 { font-size: 13pt; color: #333; margin: 10px 0 5px; }
.turn-content h3, .turn-content h4 { font-size: 12pt; color: #333; margin: 8px 0 4px; }
.turn-content p { margin: 6px 0; }
.turn-content ul, .turn-content ol { margin: 6px 0 6px 22px; }
.turn-content li { margin: 3px 0; }
.turn-content code { background: #f1f3f4; padding: 1px 4px; border-radius: 3px;
                     font-family: Consolas, "Courier New", monospace; font-size: 10pt; }
.turn-content pre { background: #f8f9fa; padding: 10px; border-radius: 4px; border: 1px solid #e0e0e0;
                    white-space: pre-wrap; overflow-wrap: anywhere; page-break-inside: auto; }
.turn-content pre code { background: none; padding: 0; font-size: 9.5pt; }
.turn-content table { border-collapse: collapse; margin: 8px 0; width: 100%; font-size: 10pt; }
.turn-content th, .turn-content td { border: 1px solid #cfcfcf; padding: 4px 8px;
                                     text-align: left; vertical-align: top; }
.turn-content th { background: #f1f3f4; }
.turn-content blockquote { border-left: 4px solid #d0d0d0; margin: 6px 0; padding: 2px 12px;
                           color: #555; background: #fafafa; }
.turn-content hr { border: none; border-top: 1px solid #e0e0e0; margin: 14px 0; }
"""


def md_to_html(text):
    escaped = html_mod.escape(text, quote=False)
    return md_lib.markdown(escaped, extensions=["tables", "fenced_code", "sane_lists"])


def build_html(title, turns, model_name, url, include_thinking, source="Google AI Studio"):
    shown = [t for t in turns if include_thinking or t["kind"] != "thinking"]
    n_user = sum(1 for t in shown if t["kind"] == "user")
    n_think = sum(1 for t in shown if t["kind"] == "thinking")
    meta_bits = [f"来源: {source}"]
    if model_name:
        meta_bits.append(f"模型: {html_mod.escape(model_name)}")
    meta_bits.append(f"用户提问 {n_user} 轮 / AI 回复 {len(shown) - n_user - n_think} 条")
    if n_think:
        meta_bits.append(f"思考过程 {n_think} 条")
    meta_bits.append(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">',
        f"<title>{html_mod.escape(title)}</title>",
        f"<style>{CSS}</style></head><body>",
        '<div class="cover">',
        f"<h1>{html_mod.escape(title)}</h1>",
        '<div class="subtitle">AI 对话记录</div>',
        f'<div class="meta">{" | ".join(meta_bits)}<br>'
        f'<a href="{html_mod.escape(url, quote=True)}">{html_mod.escape(url)}</a></div>',
        "</div>",
    ]

    user_no = 0
    for t in shown:
        if t["kind"] == "user":
            user_no += 1
            cls, label = "user", f"用户 #{user_no}"
        elif t["kind"] == "thinking":
            cls, label = "thinking", "AI 思考过程"
        else:
            cls, label = "model", "AI 回复"
        parts.append(f'<div class="turn {cls}"><div class="turn-header">{label}</div>')
        parts.append(f'<div class="turn-content">{md_to_html(t["text"])}</div></div>')

    parts.append("</body></html>")
    return "\n".join(parts)


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" .")
    return name[:80] or "AI_对话"


# ---------------------------------------------------------------- 浏览器启动

def _channel_chain(preferred):
    return [c for c in dict.fromkeys([preferred, "msedge", "chrome", "chromium"]) if c]


def launch_capture_context(p, channel, profile_dir):
    """启动带登录状态的浏览器窗口 (有界面, 用于登录/切换账号)。"""
    last_err = None
    for ch in _channel_chain(channel):
        kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1380, "height": 900},
        )
        if ch != "chromium":
            kwargs["channel"] = ch
        try:
            ctx = p.chromium.launch_persistent_context(**kwargs)
            try:
                ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            except Exception:
                pass
            return ctx
        except Exception as e:
            last_err = e
    raise RuntimeError(f"无法启动浏览器: {last_err}\n"
                       f"Linux 下可先运行: python -m playwright install chromium")


def launch_headless(p, channel):
    """启动无头浏览器, 用于把 HTML 渲染成 PDF。"""
    last_err = None
    for ch in _channel_chain(channel):
        kwargs = dict(headless=True)
        if ch != "chromium":
            kwargs["channel"] = ch
        try:
            return p.chromium.launch(**kwargs)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"无法启动浏览器: {last_err}\n"
                       f"Linux 下可先运行: python -m playwright install chromium")


# ---------------------------------------------------------------- 主流程

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="导出 Google AI Studio 对话为 PDF")
    ap.add_argument("urls", nargs="+", help="AI Studio 对话链接 (可多个)")
    ap.add_argument("-o", "--out", help="输出 PDF 路径 (仅单个链接时有效)")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE), help="浏览器配置目录")
    ap.add_argument("--timeout", type=int, default=300, help="等待登录/加载超时秒数")
    ap.add_argument("--channel", default="chrome", choices=["msedge", "chrome", "chromium"])
    ap.add_argument("--keep-raw", action="store_true", help="保存原始 JSON 数据")
    ap.add_argument("--no-thinking", action="store_true", help="不包含 AI 思考过程")
    args = ap.parse_args()

    if args.out and len(args.urls) > 1:
        ap.error("--out 只能在导出单个链接时使用")

    out_dir = Path.cwd()
    profile_dir = Path(args.profile)

    results = []
    with sync_playwright() as p:
        log("启动浏览器 (首次运行需要登录一次, 登录状态会被保存)…")
        ctx = launch_capture_context(p, args.channel, profile_dir)
        try:
            for idx, url in enumerate(args.urls, 1):
                log(f"\n===== [{idx}/{len(args.urls)}] {url} =====")
                payload, dom_title, closed = capture_conversation(ctx, url, args.timeout)
                if closed:
                    log("浏览器窗口被意外关闭, 3 秒后重启浏览器重试 (请不要关闭弹出的窗口)…")
                    time.sleep(3)
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    ctx = launch_capture_context(p, args.channel, profile_dir)
                    payload, dom_title, closed = capture_conversation(ctx, url, args.timeout)
                if payload is None:
                    log("未能捕获对话数据: 可能超时未登录, 或链接无权限/不是对话页面。")
                    continue

                turns = classify_turns(extract_turns(payload))
                if not turns:
                    log("响应中未找到对话内容, 跳过。")
                    continue

                title = extract_title(payload) or dom_title or "Claude 对话"
                model_name = extract_model_name(payload)
                log(f"标题: {title}")
                log(f"共 {len(turns)} 条消息 (用户 {sum(1 for t in turns if t['kind']=='user')} 轮)")
                results.append((url, title, turns, model_name, payload))
        finally:
            try:
                ctx.close()
            except Exception:
                pass

        if not results:
            log("\n没有成功导出任何对话。")
            sys.exit(1)

        for url, title, turns, model_name, payload in results:
            pdf_path = (Path(args.out) if args.out
                        else out_dir / f"{sanitize_filename(title)}.pdf")
            html_path = pdf_path.with_suffix(".html")

            html_doc = build_html(title, turns, model_name, url, not args.no_thinking,
                                  "Claude" if "claude.ai" in url else "Google AI Studio")
            html_path.write_text(html_doc, encoding="utf-8")
            log(f"已生成 HTML: {html_path}")

            if args.keep_raw:
                raw_path = pdf_path.with_suffix(".raw.json")
                raw_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                log(f"已保存原始数据: {raw_path}")

            log("正在渲染 PDF…")
            browser = launch_headless(p, args.channel)
            try:
                page = browser.new_page()
                page.goto(html_path.resolve().as_uri(), wait_until="load")
                page.wait_for_timeout(300)
                page.pdf(path=str(pdf_path), format="A4", print_background=True,
                         margin={"top": "1.2cm", "bottom": "1.2cm",
                                 "left": "1.5cm", "right": "1.5cm"})
            finally:
                browser.close()
            size_kb = pdf_path.stat().st_size // 1024
            log(f"完成! PDF 已保存: {pdf_path} ({size_kb} KB)")


if __name__ == "__main__":
    main()
