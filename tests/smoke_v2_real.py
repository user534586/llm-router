#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机冒烟（第二轮新特性）：用真实 config.json 起 router，验证前缀模式 / 未知 model /
多模态强制本地 / 响应头 / 空正文兜底。会真的调用本地模型和云端（token 很少）。

用法：python tests/smoke_v2_real.py [--with-cloud]
      不加 --with-cloud 时不会碰云端（只走本地链路）。
"""

import base64
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
PORT = 8100


def tiny_png_data_url():
    """造一张真正合法的 1x1 红色 PNG（写死 base64 容易踩坑，就自己生成）。"""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)      # 1x1 truecolor
    idat = zlib.compress(b"\x00" + b"\xff\x00\x00")           # 一个红点
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", idat) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()

PASS = []
FAIL = []
SKIP = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   :: " + str(extra)) if extra else ""))


def skip(name, why):
    SKIP.append(name)
    print("  SKIP  " + name + "   :: " + why)


def http(url, payload=None, method="GET", timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "ignore")


def chat(payload):
    return http("http://127.0.0.1:%d/v1/chat/completions" % PORT, payload, method="POST")


def content_of(raw):
    try:
        return json.loads(raw)["choices"][0]["message"].get("content") or ""
    except Exception:
        return ""


def main():
    with_cloud = "--with-cloud" in sys.argv
    env = dict(os.environ)
    env.pop("LLM_ROUTER_CONFIG", None)
    env.pop("LLM_ROUTER_MODE_STATE", None)
    log = os.path.join(BASE, "logs", "smoke_v2_real.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    fh = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "router.py")], cwd=BASE, env=env,
                            stdout=fh, stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            time.sleep(0.25)
            if proc.poll() is not None:
                raise RuntimeError("router exited early (rc=%s)，看 %s" % (proc.returncode, log))
            st, _h, _b = http("http://127.0.0.1:%d/health" % PORT, timeout=3)
            if st == 200:
                break
        print("[router] 真配置已起在 8100，日志 %s" % log)

        st, _h, raw = http("http://127.0.0.1:%d/health" % PORT)
        js = json.loads(raw)
        check("R1 /health 新增字段齐全（prefix_mode / unknown_model / models）",
              st == 200 and js.get("prefix_mode") == "smart" and js.get("unknown_model") == "error"
              and "deepseek" in (js.get("models") or []), raw[:200])

        st, _h, raw = http("http://127.0.0.1:%d/route" % PORT)
        check("R2 初始模式 = fallback（无 mode.state）",
              st == 200 and json.loads(raw).get("mode") == "fallback", raw[:120])

        st, _h, raw = chat({"model": "gpt-4o-nonsense",
                            "messages": [{"role": "user", "content": "hi"}]})
        try:
            err = json.loads(raw)["error"]
        except Exception:
            err = {}
        check("R3 未知 model 直接 400，不再静默走云端",
              st == 400 and err.get("type") == "invalid_model"
              and "deepseek-v4.1-flash" in err.get("message", ""), (st, err.get("message", "")[:150]))

        st, h, raw = chat({"model": "auto", "max_tokens": 32,
                           "messages": [{"role": "user", "content": "只回两个字：收到"}]})
        src = h.get("X-Router-Source")
        c = content_of(raw)
        check("R4 短请求走本地 + 响应头 X-Router-Source",
              st == 200 and src == "local" and c and not c.startswith("[via:"), (st, src, c[:60]))

        st, h, raw = chat({"model": "auto", "max_tokens": 32, "messages": [
            {"role": "user", "content": "只回两个字：收到"},
            {"role": "assistant", "content": c},
            {"role": "user", "content": "再来一次"}]})
        check("R5 本地链路：带历史也不加前缀",
              st == 200 and h.get("X-Router-Source") == "local"
              and not (content_of(raw) or "").startswith("[via:"),
              (st, h.get("X-Router-Source"), content_of(raw)[:60]))

        log_size = os.path.getsize(log)
        st, h, raw = chat({"model": "auto", "max_tokens": 64, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这张图是什么颜色？一个字回答"},
            {"type": "image_url", "image_url": {"url": tiny_png_data_url()}},
        ]}]})
        src = h.get("X-Router-Source") or ""
        try:
            tried = [t["target"] for t in json.loads(raw)["error"]["tried"]]
        except Exception:
            tried = []
        with open(log, encoding="utf-8", errors="ignore") as f:
            f.seek(log_size)
            tail = f.read()
        touched_cloud = src.startswith("cloud[") or any(t.startswith("cloud[") for t in tried)
        check("R6 带图请求：只撞本地，本地失败也不甩给纯文本云端",
              "多模态请求强制本地" in tail and "plan=['local']" in tail and not touched_cloud,
              {"status": st, "source": src, "tried": tried})

        if not with_cloud:
            skip("R7/R8 云端链路（前缀 + smart 去重）", "没加 --with-cloud")
        else:
            st, h, raw = chat({"model": "deepseek", "max_tokens": 64,
                               "messages": [{"role": "user", "content": "只回三个字：我在这"}]})
            c = content_of(raw)
            check("R7 点名 deepseek -> 真的走 deepseek，前缀是它（不再是 agnes）",
                  st == 200 and h.get("X-Router-Source") == "cloud[dmxapi-deepseek]"
                  and c.startswith("[via:dmxapi-deepseek(deepseek-v4.1-flash)] "),
                  (st, h.get("X-Router-Source"), c[:90]))
            check("R7b content 非空（空的话就是兜底生效了）",
                  bool(c.strip()), c[:80])

            st, h, raw = chat({"model": "deepseek", "max_tokens": 64, "messages": [
                {"role": "user", "content": "只回三个字：我在这"},
                {"role": "assistant", "content": c},
                {"role": "user", "content": "再来一次"}]})
            c2 = content_of(raw)
            check("R8 smart 前缀：同一条云端接着聊不再重复加前缀",
                  st == 200 and c2 and not c2.startswith("[via:"), (st, c2[:80]))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        fh.close()

    print("\n==== %d passed, %d failed, %d skipped ====" % (len(PASS), len(FAIL), len(SKIP)))
    for f in FAIL:
        print("  FAILED: " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
