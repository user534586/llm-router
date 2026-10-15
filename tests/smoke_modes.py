#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证四条需求：/route 四种模式、model 指定优先于 /route、云端前缀。

用法：python tests/smoke_modes.py
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
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def chat(model, max_tokens=400, timeout=240):
    st, raw = http("http://127.0.0.1:%d/v1/chat/completions" % PORT,
                   {"model": model, "max_tokens": max_tokens, "stream": False,
                    "messages": [{"role": "user", "content": "只用一句话回答：你是谁？"}]},
                   method="POST", timeout=timeout)
    content = None
    try:
        content = json.loads(raw)["choices"][0]["message"].get("content")
    except Exception:
        pass
    return st, content, raw


def set_mode(mode):
    st, raw = http("http://127.0.0.1:%d/route" % PORT, {"mode": mode}, method="POST")
    cur = None
    try:
        cur = json.loads(raw).get("mode")
    except Exception:
        pass
    return cur


def show(tag, st, content, raw):
    if content is None:
        print("  %-42s status=%s  <no content> raw=%r" % (tag, st, raw[:120]))
    else:
        print("  %-42s status=%s  content=%r" % (tag, st, content[:90]))


def main():
    env = dict(os.environ)
    env["LLM_ROUTER_CONFIG"] = os.path.join(BASE, "config.json")
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "router.py")], cwd=BASE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            time.sleep(0.25)
            try:
                if http("http://127.0.0.1:%d/health" % PORT, timeout=2)[0] == 200:
                    break
            except Exception:
                pass

        print("== 需求1：/route 四种模式 + model=auto 的真实走向 ==")
        print("[M1] /route auto  -> mode=%s" % set_mode("auto"))
        show("model=auto（应走本地、无前缀）", *chat("auto"))
        print("[M2] /route cloud -> mode=%s" % set_mode("cloud"))
        show("model=auto（应走 agnes3.0、带前缀）", *chat("auto"))
        print("[M3] /route local -> mode=%s" % set_mode("local"))
        show("model=auto（应只走本地、无前缀）", *chat("auto"))
        print("[M4] /route deepseek -> mode=%s" % set_mode("deepseek"))
        show("model=auto（应走 dmxapi-deepseek）", *chat("auto"))

        print("== 需求3：model 指定时忽略 /route（当前 /route local）==")
        show("model=agnes-3.0-flash", *chat("agnes-3.0-flash"))
        show("model=deepseek-v4.1-flash", *chat("deepseek-v4.1-flash"))
        show("model=local", *chat("local"))

        print("== 复位 ==")
        print("  /route fallback -> mode=%s" % set_mode("fallback"))

        log_path = os.path.join(BASE, "logs", "router.log")
        try:
            with open(log_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            print("\n==== router.log tail（plan/命中） ====")
            for line in lines[-30:]:
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
