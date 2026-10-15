#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm-router 自测：起假上游 + 起 router，全链路验证路由/降级/前缀/流式/熔断。

用法：python tests/selftest_router.py
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
sys.path.insert(0, HERE)

import fake_upstream  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   :: " + str(extra)) if extra else ""))


def http(url, payload=None, method="GET", timeout=30):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8", "ignore")


def chat(port, payload, **kw):
    return http("http://127.0.0.1:%d/v1/chat/completions" % port, payload, method="POST", **kw)


def route(port, word=None, method="POST"):
    if method == "GET":
        return http("http://127.0.0.1:%d/route" % port)
    return http("http://127.0.0.1:%d/route" % port, {"mode": word}, method="POST")


def start_router(cfg_name, port, log_name):
    log_path = os.path.join(BASE, "logs", log_name)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    open(log_path, "w", encoding="utf-8").close()
    env = dict(os.environ)
    env["LLM_ROUTER_CONFIG"] = os.path.join(HERE, cfg_name)
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "router.py")], cwd=BASE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(80):
        time.sleep(0.25)
        if proc.poll() is not None:
            raise RuntimeError("router exited early (rc=%s)" % proc.returncode)
        try:
            st, _ct, _b = http("http://127.0.0.1:%d/health" % port, timeout=2)
            if st == 200:
                return proc, log_path
        except Exception:
            pass
    raise RuntimeError("router on port %d did not come up" % port)


def stop_router(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def read_log(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def msg_content(raw):
    try:
        obj = json.loads(raw)
        return obj["choices"][0]["message"]
    except Exception:
        return {}


def main():
    fake_upstream.serve(8097, "401")
    fake_upstream.serve(8098, "ok")
    fake_upstream.serve(8099, "429")
    print("[fake upstreams] 8097=401  8098=ok  8099=429")

    # ---------------- router A: 主流程 ----------------
    proc, log = start_router("cfg_main.json", 8101, "test_main.log")
    print("[router A] port 8101 (local=429)")
    try:
        st, _ct, raw = chat(8101, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        msg = msg_content(raw)
        content = msg.get("content") or ""
        check("A1 本地429 -> 自动降级云端(非流式)", st == 200 and "served-by:fakeA-model" in content, (st, content))
        check("A2 云端前缀带链路名", content.startswith("[via:fakeA(fakeA-model)] "), content)

        st, _ct, raw = chat(8101, {"model": "fakeB-model",
                                   "messages": [{"role": "user", "content": "hi"}]})
        content = (msg_content(raw).get("content") or "")
        check("A3 指定 model=第二条云端 -> 命中那条(不是第一条)", "served-by:fakeB-model" in content
              and content.startswith("[via:fakeB(fakeB-model)] "), content)

        st, _ct, raw = chat(8101, {"model": "auto", "tools": [{"type": "function",
                                "function": {"name": "ping", "parameters": {}}}],
                                   "messages": [{"role": "user", "content": "TOOL please"}]})
        msg = msg_content(raw)
        check("A4 工具调用响应不加前缀、tool_calls 完好",
              bool(msg.get("tool_calls")) and not (msg.get("content") or "").startswith("[via:"),
              {"content": msg.get("content"), "tool_calls": bool(msg.get("tool_calls"))})

        st, ct, raw = chat(8101, {"model": "auto", "stream": True,
                                  "messages": [{"role": "user", "content": "hi"}]})
        text = ""
        for line in raw.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if isinstance(d.get("content"), str):
                    text += d["content"]
        check("A5 流式真透传 + 前缀只加一次",
              st == 200 and "text/event-stream" in ct and text == "[via:fakeA(fakeA-model)] HELLO-WORLD",
              {"status": st, "ctype": ct, "text": text})

        log_before = len(read_log(log))
        st, _ct, raw = chat(8101, {"model": "auto", "messages": [{"role": "user", "content": "x" * 20000}]})
        new_log = read_log(log)[log_before:]
        check("A6 智能路由：超本地窗口直接走云端(不撞本地)",
              st == 200 and "local_max_ctx" in new_log and "-> local" not in new_log,
              new_log.strip().splitlines()[:2])

        log_before = len(read_log(log))
        st, _ct, raw = chat(8101, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        new_log = read_log(log)[log_before:]
        check("A7 短请求仍然先撞本地再降级", "-> local" in new_log, new_log.strip().splitlines()[:3])

        st, _ct, raw = route(8101, "deepseek")
        mode = json.loads(raw).get("mode")
        check("A8 /route deepseek -> 指定 deepseek 链", st == 200 and mode == "force:fakeB", raw[:200])

        st, _ct, raw = chat(8101, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        content = (msg_content(raw).get("content") or "")
        check("A9 指定后请求真的走那条云端", "served-by:fakeB-model" in content, content)

        st, _ct, raw = route(8101, "local")
        st2, _ct2, raw2 = chat(8101, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        check("A10 /route local 后只走本地，不降级", st2 == 502 and "all upstreams failed" in raw2,
              (st2, raw2[:160]))

        st, _ct, raw = route(8101, "cloud")
        st2, _ct2, raw2 = chat(8101, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        content = (msg_content(raw2).get("content") or "")
        check("A11 /route cloud 后按整条云端链接力", st2 == 200 and "served-by:fakeA-model" in content, content)

        st, _ct, raw = route(8101, "fallback")
        st2, _ct2, raw2 = route(8101, None, method="GET")
        info = json.loads(raw2).get("info", "")
        check("A12 GET /route 查询状态", st2 == 200 and "自动" in info and "云端链" in info, info[:120])

        st, _ct, raw = http("http://127.0.0.1:8101/health")
        js = json.loads(raw)
        check("A13 /health 带模式与各链路状态",
              st == 200 and js.get("mode") == "fallback" and "local" in js and js.get("clouds"),
              raw[:200])

        st, _ct, raw = http("http://127.0.0.1:8101/v1/models")
        ids = [m.get("id") for m in json.loads(raw).get("data", [])]
        check("A14 /v1/models 暴露 local/auto/云端模型",
              "auto" in ids and "local" in ids and "fakeA-model" in ids, ids)

        st, ct, raw = chat(8101, {"model": "auto", "messages": [{"role": "user", "content": "/route info"}]})
        check("A15 聊天内 /route 指令拦截(不调模型)", st == 200 and "当前路由模式" in raw, raw[:120])
    finally:
        stop_router(proc)

    # ---------------- router B: 401 降级 ----------------
    proc, log = start_router("cfg_401.json", 8102, "test_401.log")
    print("[router B] port 8102 (local=401)")
    try:
        st, _ct, raw = chat(8102, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        content = (msg_content(raw).get("content") or "")
        check("B1 本地401(欠费/密钥) 也降级云端", st == 200 and "served-by:fakeA-model" in content, (st, content))
        check("B2 日志把 401 记成可降级错误", "http_401" in read_log(log), read_log(log)[-200:])
    finally:
        stop_router(proc)

    # ---------------- router C: 本地探活 + 冷却 ----------------
    proc, log = start_router("cfg_probe.json", 8103, "test_probe.log")
    print("[router C] port 8103 (local 不可达, 探活 3s)")
    try:
        time.sleep(5)
        st, _ct, raw = http("http://127.0.0.1:8103/health")
        js = json.loads(raw)
        blocked = (js.get("local") or {}).get("blocked_for", 0)
        check("C1 本地探活失败 -> /health 标记冷却中", st == 200 and blocked > 0, js.get("local"))

        log_before = len(read_log(log))
        st, _ct, raw = chat(8103, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        new_log = read_log(log)[log_before:]
        content = (msg_content(raw).get("content") or "")
        check("C2 冷却期间直接走云端(不撞本地)",
              st == 200 and "served-by:fakeA-model" in content and "-> local" not in new_log,
              new_log.strip().splitlines()[:2])
    finally:
        stop_router(proc)

    print("\n==== %d passed, %d failed ====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAILED: " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
