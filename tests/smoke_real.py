#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实配置冒烟测试：真本地模型 + 真云端，跑完自动关掉路由器。

用法：python tests/smoke_real.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
PORT = 8100


def http(url, payload=None, method="GET", timeout=240):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8", "ignore")


def content_of(raw):
    try:
        return json.loads(raw)["choices"][0]["message"].get("content")
    except Exception:
        return None


def main():
    env = dict(os.environ)
    env["LLM_ROUTER_CONFIG"] = os.path.join(BASE, "config.json")
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "router.py")], cwd=BASE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            time.sleep(0.25)
            if proc.poll() is not None:
                print("router exited early, rc=%s" % proc.returncode)
                return 1
            try:
                st, _c, _b = http("http://127.0.0.1:%d/health" % PORT, timeout=2)
                if st == 200:
                    break
            except Exception:
                pass
        else:
            print("router did not come up")
            return 1

        st, _c, raw = http("http://127.0.0.1:%d/health" % PORT)
        print("[1] /health ->", raw[:300])

        st, _c, raw = http("http://127.0.0.1:%d/route" % PORT)
        print("[2] GET /route ->")
        print("    " + json.loads(raw).get("info", "").replace("\n", "\n    "))

        # 3) 走本地（免费）
        t0 = time.time()
        st, _c, raw = http("http://127.0.0.1:%d/v1/chat/completions" % PORT,
                           {"model": "local", "max_tokens": 24, "stream": False,
                            "messages": [{"role": "user", "content": "回答一个字：好"}]}, method="POST")
        print("[3] 本地非流式 status=%s %.1fs -> %r" % (st, time.time() - t0, (content_of(raw) or raw[:200]))[:300])

        # 4) 指定 deepseek（就是之前走错到 agnes 的坑）
        st, _c, raw = http("http://127.0.0.1:%d/v1/chat/completions" % PORT,
                           {"model": "deepseek-v4.1-flash", "max_tokens": 16, "stream": False,
                            "messages": [{"role": "user", "content": "回答一个字：好"}]}, method="POST")
        c = content_of(raw)
        print("[4] model=deepseek-v4.1-flash status=%s -> %r" % (st, c if c is not None else raw[:200]))
        print("    命中 dmxapi-deepseek：%s" % bool(c and c.startswith("[via:dmxapi-deepseek(")))

        # 5) 流式 + 前缀
        st, ct, raw = http("http://127.0.0.1:%d/v1/chat/completions" % PORT,
                           {"model": "deepseek-v4.1-flash", "max_tokens": 16, "stream": True,
                            "messages": [{"role": "user", "content": "回答一个字：好"}]}, method="POST")
        text = ""
        for line in raw.split("\n"):
            if not line.startswith("data: "):
                continue
            p = line[6:].strip()
            if not p or p == "[DONE]":
                continue
            try:
                o = json.loads(p)
            except Exception:
                continue
            for ch in o.get("choices") or []:
                d = ch.get("delta") or {}
                if isinstance(d.get("content"), str):
                    text += d["content"]
        print("[5] 流式 status=%s ctype=%s -> %r" % (st, ct, text[:200]))

        # 6) /route deepseek 后 auto 也走 deepseek
        st, _c, raw = http("http://127.0.0.1:%d/route" % PORT, {"mode": "deepseek"}, method="POST")
        print("[6] POST /route deepseek -> mode=%s" % json.loads(raw).get("mode"))
        st, _c, raw = http("http://127.0.0.1:%d/v1/chat/completions" % PORT,
                           {"model": "auto", "max_tokens": 16, "stream": False,
                            "messages": [{"role": "user", "content": "回答一个字：好"}]}, method="POST")
        print("[7] auto(deepseek模式) status=%s -> %r" % (st, content_of(raw)))

        # 复位
        http("http://127.0.0.1:%d/route" % PORT, {"mode": "fallback"}, method="POST")
        print("[8] 已复位 /route fallback")

        log_path = os.path.join(BASE, "logs", "router.log")
        try:
            with open(log_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            print("\n==== router.log tail ====")
            for line in lines[-25:]:
                print("    " + line)
        except Exception as e:
            print("(log read failed: %s)" % e)
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
