#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm-router 第二轮自测：前缀模式 / 空正文兜底 / 未知 model / 多模态 / 响应头 /
模式持久化 / 日志轮转 / 云端别名 / 别名点名接力。

用法：python tests/selftest_v2.py
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

MODE_STATE = os.path.join(HERE, "mode.state.test")

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   :: " + str(extra)) if extra else ""))


def http_full(url, payload=None, method="GET", timeout=30):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "ignore")


def http(url, payload=None, method="GET", timeout=30):
    st, _h, body = http_full(url, payload, method, timeout)
    return st, body


def chat(port, payload, **kw):
    return http("http://127.0.0.1:%d/v1/chat/completions" % port, payload, method="POST", **kw)


def chat_h(port, payload, **kw):
    return http_full("http://127.0.0.1:%d/v1/chat/completions" % port, payload, method="POST", **kw)


def route(port, word=None, method="POST"):
    if method == "GET" or word is None:
        return http("http://127.0.0.1:%d/route" % port)
    return http("http://127.0.0.1:%d/route" % port, {"mode": word}, method="POST")


def start_router(cfg_name, port, log_name):
    log_path = os.path.join(BASE, "logs", log_name)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    open(log_path, "w", encoding="utf-8").close()
    for suffix in ("", ".1", ".2", ".3"):
        p = log_path + suffix
        if os.path.exists(p):
            os.remove(p)
    env = dict(os.environ)
    env["LLM_ROUTER_CONFIG"] = os.path.join(HERE, cfg_name)
    env["LLM_ROUTER_MODE_STATE"] = MODE_STATE
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "router.py")], cwd=BASE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(80):
        time.sleep(0.25)
        if proc.poll() is not None:
            raise RuntimeError("router exited early (rc=%s)" % proc.returncode)
        st, _b = http("http://127.0.0.1:%d/health" % port, timeout=2)
        if st == 200:
            return proc, log_path
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


def msg_content(raw):
    try:
        obj = json.loads(raw)
        return obj["choices"][0]["message"]
    except Exception:
        return {}


def sse_text(raw):
    """把 SSE 流里所有 delta.content 拼起来。"""
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
    return text


P_AGNES = "[via:reason(reason-model)] "


def main():
    fake_upstream.serve(8099, "429")      # local：挂（429 可降级）
    fake_upstream.serve(8094, "reasoning")  # 思考模型：content 空、正文在 reasoning_content
    fake_upstream.serve(8095, "ok")       # 正常云端
    print("[fake upstreams] 8099=429(local)  8094=reasoning  8095=ok")

    if os.path.exists(MODE_STATE):
        os.remove(MODE_STATE)

    # ================= router V2：主流程 =================
    proc, log = start_router("cfg_v2.json", 8105, "test_v2.log")
    print("[router V2] port 8105 (local=429)")
    try:
        # --- 空正文兜底 + 前缀 ---
        st, h, raw = chat_h(8105, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        content = msg_content(raw).get("content") or ""
        check("V1 空正文兜底：content 为空时用 reasoning_content 补上",
              st == 200 and content == P_AGNES + "THINK-ONLY-ANSWER", (st, content))
        check("V2 响应头带实际链路",
              h.get("X-Router-Source") == "cloud[reason]" and h.get("X-Router-Mode") == "fallback",
              {k: v for k, v in h.items() if k.startswith("X-Router")})
        check("V3 兜底行为写进 X-Router-Why", "reasoning_fallback" in (h.get("X-Router-Why") or ""),
              h.get("X-Router-Why"))

        # --- prefix_mode=smart：历史里已有同样前缀就不再加 ---
        hist = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": P_AGNES + "THINK-ONLY-ANSWER"},
            {"role": "user", "content": "再问一句"},
        ]
        st, _h, raw = chat_h(8105, {"model": "auto", "messages": hist})
        content = msg_content(raw).get("content") or ""
        check("V4 smart 前缀：同一条链路重复走时不再加前缀（防污染）",
              st == 200 and content == "THINK-ONLY-ANSWER", content)

        # --- prefix_mode=smart：换链路时前缀会出现 ---
        st, _h, raw = chat_h(8105, {"model": "fakeA-model",
                                    "messages": [{"role": "assistant", "content": P_AGNES + "旧回答"},
                                                 {"role": "user", "content": "hi"}]})
        content = msg_content(raw).get("content") or ""
        check("V5 smart 前缀：换成另一条云端时前缀照常出现",
              st == 200 and content == "[via:fakeA(fakeA-model)] served-by:fakeA-model", content)

        st, _h, raw = chat_h(8105, {"model": "fa", "messages": [{"role": "user", "content": "hi"}]})
        content = msg_content(raw).get("content") or ""
        check("V6 别名当 model 名（fa）也能点名云端",
              st == 200 and content.endswith("served-by:fakeA-model"), (st, content))

        # --- 未知 model：明确报错 ---
        st, _h, raw = chat_h(8105, {"model": "gpt-9-turbo",
                                    "messages": [{"role": "user", "content": "hi"}]})
        try:
            err = json.loads(raw)["error"]
        except Exception:
            err = {}
        check("V7 没配置过的 model -> 400 且列出可用名字",
              st == 400 and err.get("type") == "invalid_model" and "fakeA-model" in err.get("message", ""),
              (st, err.get("message", "")[:180]))
        st, _b = chat(8105, {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        check("V8 auto 不受未知 model 检查影响", st == 200, st)

        # --- 多模态：不降级到纯文本云端 ---
        mm = {"model": "auto", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这图里是什么"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}]}
        st, _h, raw = chat_h(8105, mm)
        try:
            tried = [t["target"] for t in json.loads(raw)["error"]["tried"]]
        except Exception:
            tried = []
        check("V9 带图请求只撞本地，不丢给纯文本云端",
              st == 502 and tried == ["local"], (st, tried))
        mm2 = json.loads(json.dumps(mm))
        mm2["model"] = "fakeA-model"
        st, _h, raw = chat_h(8105, mm2)
        check("V10 显式点名云端时，带图请求仍按点名走（用户说了算）",
              st == 200 and "served-by:fakeA-model" in (msg_content(raw).get("content") or ""),
              msg_content(raw).get("content"))
        st, _h, raw = chat_h(8105, {"model": "auto", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "纯文本但用数组形式"}]}]})
        check("V11 纯文本数组内容不算多模态", st == 200, st)

        # --- 工具调用不加前缀、不兜底 ---
        st, _h, raw = chat_h(8105, {"model": "fakeA-model",
                                    "tools": [{"type": "function",
                                               "function": {"name": "ping", "parameters": {}}}],
                                    "messages": [{"role": "user", "content": "TOOL please"}]})
        m = msg_content(raw)
        check("V12 工具调用响应不加前缀、不做兜底",
              bool(m.get("tool_calls")) and not (m.get("content") or ""), 
              {"content": m.get("content"), "tool_calls": bool(m.get("tool_calls"))})

        # --- 流式：前缀只一次 / 空正文兜底 ---
        st, _h, raw = chat_h(8105, {"model": "fakeA-model", "stream": True,
                                    "messages": [{"role": "assistant", "content": P_AGNES + "旧"},
                                                 {"role": "user", "content": "hi"}]})
        text = sse_text(raw)
        check("V13 流式：前缀只加在首个内容分片",
              st == 200 and text == "[via:fakeA(fakeA-model)] HELLO-WORLD", text)

        st, _h, raw = chat_h(8105, {"model": "reason-model", "stream": True,
                                    "messages": [{"role": "user", "content": "hi"}]})
        text = sse_text(raw)
        check("V14 流式空正文兜底：收尾前补一个内容分片",
              st == 200 and text == P_AGNES + "THINK-ONLY-ANSWER", text)

        # --- /v1/models 暴露别名 ---
        st, raw = http("http://127.0.0.1:8105/v1/models")
        ids = [m.get("id") for m in json.loads(raw).get("data", [])]
        check("V15 /v1/models 列出云端别名（fa / rsn / reasoner）与模型名",
              all(x in ids for x in ("auto", "local", "reason-model", "fakeA-model",
                                     "fa", "rsn", "reasoner")), ids)

        # --- /health 带上新配置 ---
        st, raw = http("http://127.0.0.1:8105/health")
        js = json.loads(raw)
        check("V16 /health 暴露 prefix_mode / unknown_model / 可用 model 名",
              js.get("prefix_mode") == "smart" and js.get("unknown_model") == "error"
              and "fa" in (js.get("models") or []), raw[:220])

        # --- /route info 提示前缀模式 ---
        st, raw = chat(8105, {"model": "auto", "messages": [{"role": "user", "content": "/route info"}]})
        check("V17 聊天内 /route info 带上前缀模式说明", st == 200 and "前缀模式" in raw, raw[:160])
    finally:
        stop_router(proc)

    # ================= router A：别名点名也要能接力 =================
    # 8096 故意没人监听，模拟"别名点名的那条云端挂了"
    proc, log = start_router("cfg_alias.json", 8107, "test_alias.log")
    print("[router A] port 8107 (alias d1 -> dead cloud, pin_fallback=true)")
    try:
        st, h, raw = chat_h(8107, {"model": "d1", "messages": [{"role": "user", "content": "hi"}]})
        content = msg_content(raw).get("content") or ""
        check("A1 别名点名的云端挂了也能接力下一条（pin_fallback 对别名同样生效）",
              st == 200 and "served-by:fakeA-model" in content, (st, content))
        check("A2 接力后 X-Router-Source 报的是真正命中的那条",
              h.get("X-Router-Source") == "cloud[fakeA]", h.get("X-Router-Source"))
    finally:
        stop_router(proc)

    # ================= router P：持久化 + 轮转 =================
    proc, log = start_router("cfg_persist.json", 8106, "test_persist.log")
    print("[router P] port 8106 (persist_mode=true, default_mode=cloud)")
    try:
        st, raw = route(8106)
        check("P1 default_mode=cloud 生效（无 mode.state 时）",
              st == 200 and json.loads(raw).get("mode") == "force_cloud", raw[:160])

        route(8106, "local")
        check("P2 /route local 后写入 mode.state",
              os.path.exists(MODE_STATE) and
              json.load(open(MODE_STATE, encoding="utf-8")).get("mode") == "force_local",
              open(MODE_STATE, encoding="utf-8").read() if os.path.exists(MODE_STATE) else "missing")
    finally:
        stop_router(proc)

    proc, log = start_router("cfg_persist.json", 8106, "test_persist.log")
    try:
        st, raw = route(8106)
        check("P3 重启后模式不回退（读回 mode.state）",
              st == 200 and json.loads(raw).get("mode") == "force_local", raw[:160])

        st, h, raw = chat_h(8106, {"model": "fakeA-model",
                                   "messages": [{"role": "assistant",
                                                 "content": "[via:fakeA(fakeA-model)] 旧"},
                                                {"role": "user", "content": "hi"}]})
        content = msg_content(raw).get("content") or ""
        check("P4 prefix_mode=every：历史里有同样前缀也照样加（可切回'每次都要看'）",
              st == 200 and content.startswith("[via:fakeA(fakeA-model)] "), content)

        for _ in range(20):
            chat(8106, {"model": "fakeA-model", "messages": [{"role": "user", "content": "hi"}]})
        check("P5 日志按大小轮转：router.log 超过 max_bytes 后出现 .1",
              os.path.exists(log + ".1"), os.listdir(os.path.dirname(log)))
    finally:
        stop_router(proc)

    if os.path.exists(MODE_STATE):
        os.remove(MODE_STATE)

    print("\n==== %d passed, %d failed ====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAILED: " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
