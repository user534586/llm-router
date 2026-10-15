#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证真实云端返回的正文里能看到链路前缀。"""
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


def main():
    env = dict(os.environ)
    env["LLM_ROUTER_CONFIG"] = os.path.join(BASE, "config.json")
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "router.py")], cwd=BASE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            time.sleep(0.25)
            try:
                st, _ = http("http://127.0.0.1:%d/health" % PORT, timeout=2)
                if st == 200:
                    break
            except Exception:
                pass

        for model in ("deepseek-v4.1-flash", "auto"):
            st, raw = http("http://127.0.0.1:%d/v1/chat/completions" % PORT,
                           {"model": model, "max_tokens": 400, "stream": False,
                            "messages": [{"role": "user", "content": "用一句话说：你是谁？"}]},
                           method="POST")
            try:
                obj = json.loads(raw)
                msg = obj["choices"][0]["message"]
                print("[非流式 model=%s] status=%s content=%r" % (model, st, (msg.get("content") or "")[:120]))
            except Exception:
                print("[非流式 model=%s] status=%s raw=%r" % (model, st, raw[:200]))

        # 流式，看首个分片
        st, raw = http("http://127.0.0.1:%d/v1/chat/completions" % PORT,
                       {"model": "deepseek-v4.1-flash", "max_tokens": 400, "stream": True,
                        "messages": [{"role": "user", "content": "用一句话说：你是谁？"}]},
                       method="POST")
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
        print("[流式 deepseek] status=%s text=%r" % (st, text[:160]))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
