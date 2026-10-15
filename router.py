#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Router
==========
本地优先 + 多云端接力兜底 + 手动路由（/route 端点、聊天内 /route 指令）。

方向由请求里的 model 决定，/route 只影响 model=auto 时：
    model=local / 本地真实模型名     -> 只走本地
    model=<某条云端的 model 名>      -> 只走那条云端
    model=<某条云端的 name 或 alias> -> 同上（例：deepseek）
    model=auto                      -> 按 /route 模式（默认 auto/fallback）
    其它没配置过的 model 名          -> 按 router.unknown_model（默认 error，直接 400）

/route 模式：
    auto | fallback | 自动   本地优先，本地失败按顺序试整条云端链
    local | 本地             只走本地，不降级
    cloud | 云端             只走整条云端链（按顺序接力）
    <云名/别名>（如 deepseek）只走匹配的云端链

内置能力：
    * 本地健康探测（后台探活，挂了冷却 N 秒内直接走云端）
    * 熔断（单条链路连挂 N 次后冷却一段时间）
    * 智能路由（估算 token，超过本地窗口直接走云端）
    * 云端响应加链路前缀（prefix_mode: every | smart | off）
    * 空正文兜底：content 为空但 reasoning_content 有内容时自动补上
      （本地 Huihui 这类思考模型 max_tokens 偏小时正文会是空串）
    * 多模态请求（带图/视频）在 auto 下强制走本地，不丢给纯文本云端
    * 流式（stream=true）真透传（chunked），前缀只加在首个内容分片上
    * 响应头 X-Router-Mode / X-Router-Source / X-Router-Why 便于排查
    * 模式持久化（persist_mode），重启不回退到默认模式
    * 日志按大小轮转（log.max_bytes / log.backup_count）

已知限制：
    * 流式响应一旦开始（200 + 首个分片），上游中途断开无法降级到云端，
      客户端只会拿到截断流。需要完整降级就用 stream=false。

配置：config.json（可用环境变量 LLM_ROUTER_CONFIG 指定别的配置文件）
接口：POST /v1/chat/completions、GET /v1/models、GET /health、GET|POST /route
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("LLM_ROUTER_CONFIG") or os.path.join(BASE_DIR, "config.json")
MODE_STATE_PATH = os.environ.get("LLM_ROUTER_MODE_STATE") or os.path.join(BASE_DIR, "mode.state")

LOCAL_KEY = "local"

# 这些 model 名会被当成“让 router 自己决定”（走 /route 模式）
AUTO_TOKENS = {"auto", "自动", "default", "router"}

MODE_FALLBACK = "fallback"
MODE_FORCE_LOCAL = "force_local"
MODE_FORCE_CLOUD = "force_cloud"
MODE_PIN_PREFIX = "force:"          # force:<云端名> —— 只走这条云端


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for c in cfg.get("clouds", []):
        if not c.get("api_key"):
            env_name = c.get("api_key_env", "")
            if env_name:
                c["api_key"] = os.environ.get(env_name, "")
        alias = c.get("alias")
        if isinstance(alias, str):
            c["alias"] = [alias]
        elif not isinstance(alias, list):
            c["alias"] = []
    return cfg


CFG = load_config()


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
LOG_ROTATED = [False]

# 日志级别 -> 数值，越大越严重；_write 时低于 self.level 的直接丢弃
LOG_LEVELS = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "warning": 30,
    "error": 40,
    "fatal": 50,
    "critical": 50,
}


class Logger:
    def __init__(self, cfg):
        log_cfg = cfg.get("log", {}) or {}
        self.level = LOG_LEVELS.get(str(log_cfg.get("level", "info")).lower(), 20)
        self.path = os.path.join(BASE_DIR, log_cfg.get("file", "logs/router.log"))
        self.max_bytes = int(log_cfg.get("max_bytes", 0) or 0)
        self.backup_count = max(0, int(log_cfg.get("backup_count", 3) or 0))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()
        self._size = self._disk_size()

    def _disk_size(self):
        try:
            return os.path.getsize(self.path)
        except Exception:
            return 0

    def _rotate_locked(self):
        """超过 max_bytes 就 router.log -> .1 -> .2 …（调用方已持锁）。"""
        if not self.max_bytes or self._size < self.max_bytes:
            return
        try:
            if self.backup_count <= 0:
                os.remove(self.path)
            else:
                for i in range(self.backup_count - 1, 0, -1):
                    src = "%s.%d" % (self.path, i)
                    dst = "%s.%d" % (self.path, i + 1)
                    if os.path.exists(src):
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.replace(src, dst)
                os.replace(self.path, self.path + ".1")
            LOG_ROTATED[0] = True
        except Exception:
            pass
        self._size = 0

    def _write(self, tag, msg):
        line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag, msg)
        blob = (line + "\n").encode("utf-8")
        with self._lock:
            try:
                with open(self.path, "ab") as f:
                    f.write(blob)
                self._size += len(blob)
                self._rotate_locked()
            except Exception:
                pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    def info(self, msg):
        if self.level <= 20:
            self._write("INFO", msg)

    def warn(self, msg):
        if self.level <= 30:
            self._write("WARN", msg)

    def error(self, msg):
        if self.level <= 40:
            self._write("ERROR", msg)


LOG = Logger(CFG)


def router_cfg():
    return CFG.get("router", {}) or {}


def enabled_clouds():
    return [c for c in CFG.get("clouds", []) if c.get("enabled", True)]


def label_of(c):
    return "cloud[%s]" % c.get("name", "?")


def cloud_tokens(c):
    """这条云端的可寻址名字：name + alias 列表。"""
    out = [c.get("name") or ""] + [a for a in (c.get("alias") or []) if a]
    return [t for t in out if t]


# --------------------------------------------------------------------------
# 上游请求
# --------------------------------------------------------------------------
class UpstreamError(Exception):
    def __init__(self, reason, detail=""):
        self.reason = reason          # timeout / connection / http_5xx / http_429 /
                                      # http_401 / http_402 / http_403 / context_overflow /
                                      # http_4xx / other
        self.detail = detail
        super().__init__("%s: %s" % (reason, detail))


def _classify_http(code, detail):
    if code == 429:
        return "http_429"
    if 500 <= code < 600:
        return "http_5xx"
    if code in (401, 402, 403):
        return "http_%d" % code
    lowered = (detail or "").lower()
    if code == 400 and any(k in lowered for k in ("context", "exceed", "too long",
                                                 "too large", "maximum")):
        return "context_overflow"
    return "http_4xx"


def _open(req, timeout):
    """打开上游连接，失败一律抛 UpstreamError。"""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body_bytes = b""
        try:
            body_bytes = e.read()
        except Exception:
            pass
        detail = body_bytes[:400].decode("utf-8", "ignore")
        raise UpstreamError(_classify_http(e.code, detail), "HTTP %d: %s" % (e.code, detail))
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            raise UpstreamError("timeout", reason)
        raise UpstreamError("connection", reason)
    except TimeoutError:
        raise UpstreamError("timeout", "socket timeout")
    except UpstreamError:
        raise
    except Exception as e:
        raise UpstreamError("other", repr(e))


def _build_request(endpoint_cfg, body, accept="application/json"):
    base = endpoint_cfg["base_url"].rstrip("/")
    url = base + "/chat/completions"
    api_key = endpoint_cfg.get("api_key", "")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", accept)
    if api_key:
        req.add_header("Authorization", "Bearer %s" % api_key)
    return req, float(endpoint_cfg.get("timeout", 180))


def call_upstream(endpoint_cfg, body):
    """非流式：返回 (status, resp_bytes)。"""
    req, timeout = _build_request(endpoint_cfg, body)
    resp = _open(req, timeout)
    try:
        return resp.status, resp.read()
    finally:
        try:
            resp.close()
        except Exception:
            pass


def open_upstream_stream(endpoint_cfg, body):
    """流式：返回未读取的 response 对象（调用方负责 close）。"""
    req, timeout = _build_request(endpoint_cfg, body, accept="text/event-stream")
    return _open(req, timeout)


def should_fallback(err):
    """本地失败后是否允许降级云端（由 router.fallback_on 决定）。"""
    fo = router_cfg().get("fallback_on", {}) or {}
    key = {
        "timeout": "timeout",
        "connection": "connection_error",
        "http_5xx": "http_5xx",
        "http_429": "http_429",
        "http_401": "http_401",
        "http_402": "http_402",
        "http_403": "http_403",
        "context_overflow": "context_overflow",
        "http_4xx": "http_4xx",
    }.get(err.reason)
    if key is None:
        return False
    return bool(fo.get(key, False))


# --------------------------------------------------------------------------
# 健康状态：熔断 + 本地探活
# --------------------------------------------------------------------------
class Health:
    def __init__(self):
        self._lock = threading.Lock()
        self._fails = {}
        self._open_until = {}
        self._unhealthy_until = {}
        self._last_error = {}

    def block_remaining(self, key):
        with self._lock:
            until = max(self._open_until.get(key, 0.0), self._unhealthy_until.get(key, 0.0))
        return max(0.0, until - time.time())

    def is_blocked(self, key):
        return self.block_remaining(key) > 0

    def last_error(self, key):
        with self._lock:
            return self._last_error.get(key, "")

    def note_success(self, key):
        with self._lock:
            self._fails[key] = 0
            self._open_until.pop(key, None)
            self._unhealthy_until.pop(key, None)
            self._last_error.pop(key, None)

    def note_failure(self, key, reason, detail=""):
        cb = router_cfg().get("circuit_breaker", {}) or {}
        threshold = int(cb.get("threshold", 3) or 0)
        cool = float(cb.get("cool_down", 120) or 0)
        with self._lock:
            n = self._fails.get(key, 0) + 1
            self._fails[key] = n
            self._last_error[key] = "%s: %s" % (reason, detail)
            opened = False
            if cb.get("enabled", True) and threshold > 0 and n >= threshold:
                self._open_until[key] = time.time() + cool
                opened = True
        if opened:
            LOG.warn("circuit OPEN for %s after %d consecutive failures (cool %.0fs)"
                     % (key, n, cool))
        return n

    def mark_unhealthy(self, key, seconds, why):
        with self._lock:
            self._unhealthy_until[key] = time.time() + float(seconds)
            self._last_error[key] = why

    def snapshot(self):
        with self._lock:
            return {
                "fails": dict(self._fails),
                "open_until": dict(self._open_until),
                "unhealthy_until": dict(self._unhealthy_until),
                "last_error": dict(self._last_error),
            }


HEALTH = Health()


def local_probe_loop():
    """后台探活本地模型：挂了就冷却一段时间，期间直接走云端。"""
    lh = router_cfg().get("local_health", {}) or {}
    interval = float(lh.get("interval", 30) or 30)
    cooldown = float(lh.get("cooldown", 60) or 60)
    local = CFG.get("local", {}) or {}
    url = str(local.get("base_url", "")).rstrip("/") + "/models"
    if not url.startswith("http"):
        return
    was_down = False
    while True:
        try:
            req = urllib.request.Request(url, method="GET")
            key = local.get("api_key")
            if key:
                req.add_header("Authorization", "Bearer %s" % key)
            with urllib.request.urlopen(req, timeout=min(15.0, max(3.0, interval))) as r:
                r.read(1024)
            if was_down:
                LOG.info("local health: RECOVERED (%s)" % url)
                was_down = False
            HEALTH.note_success(LOCAL_KEY)
        except Exception as e:
            why = "probe failed: %s" % (e,)
            if not was_down:
                LOG.warn("local health: DOWN (%s) -> cooldown %.0fs" % (url, cooldown))
                was_down = True
            HEALTH.mark_unhealthy(LOCAL_KEY, cooldown, why)
        time.sleep(max(3.0, interval))


# --------------------------------------------------------------------------
# 智能路由：估算 token
# --------------------------------------------------------------------------
def _content_size(content):
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    total = 0
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                total += len(part["text"].encode("utf-8"))
            elif part.get("type") in ("image_url", "input_image", "image"):
                total += 4000
    return total


def estimate_tokens(body, bytes_per_token=4.0):
    size = 0
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        size += _content_size(m.get("content"))
        for tc in m.get("tool_calls") or []:
            try:
                size += len(json.dumps(tc, ensure_ascii=False).encode("utf-8"))
            except Exception:
                pass
    for t in body.get("tools") or []:
        try:
            size += len(json.dumps(t, ensure_ascii=False).encode("utf-8"))
        except Exception:
            pass
    return int(size / max(1.0, float(bytes_per_token or 4))) + 16


# --------------------------------------------------------------------------
# 手动路由模式
# --------------------------------------------------------------------------
class ManualOverride:
    """手动路由模式：/route 端点或聊天内 /route 指令切换，可持久化到 mode.state。"""
    _mode = MODE_FALLBACK
    _persist = False
    _lock = threading.Lock()

    @classmethod
    def init(cls, mode, persist):
        with cls._lock:
            cls._mode = mode or MODE_FALLBACK
            cls._persist = bool(persist)

    @classmethod
    def get_mode(cls):
        with cls._lock:
            return cls._mode

    @classmethod
    def set_mode(cls, mode, save=True):
        with cls._lock:
            cls._mode = mode
            if not (save and cls._persist):
                return
        try:
            with open(MODE_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"mode": mode, "ts": int(time.time())}, f, ensure_ascii=False)
        except Exception as e:
            LOG.warn("MODE: 写入 %s 失败：%s" % (MODE_STATE_PATH, e))


def init_mode_from_config():
    """启动时决定初始模式：persist_mode=true 优先读 mode.state，否则用 default_mode。"""
    r = router_cfg()
    default_word = r.get("default_mode") or MODE_FALLBACK
    default_mode = mode_alias_map().get(str(default_word)) or MODE_FALLBACK
    persist = bool(r.get("persist_mode", False))
    mode, src = default_mode, "default_mode=%s" % default_word
    if persist:
        try:
            with open(MODE_STATE_PATH, encoding="utf-8") as f:
                saved = json.load(f).get("mode")
            if saved:
                mode, src = saved, "mode.state"
        except Exception:
            pass
    ManualOverride.init(mode, persist)
    return mode, src


def mode_alias_map():
    """模式词 -> 内部模式。云端 name/alias 会被注册成“指定云端”的模式词。"""
    m = {
        "fallback": MODE_FALLBACK, "auto": MODE_FALLBACK, "自动": MODE_FALLBACK,
        "local": MODE_FORCE_LOCAL, "force_local": MODE_FORCE_LOCAL, "本地": MODE_FORCE_LOCAL,
        "cloud": MODE_FORCE_CLOUD, "force_cloud": MODE_FORCE_CLOUD, "云端": MODE_FORCE_CLOUD,
    }
    for c in enabled_clouds():
        for token in cloud_tokens(c):
            m.setdefault(token, MODE_PIN_PREFIX + (c.get("name") or ""))
    return m


def chain_for_pin(pin):
    return [c for c in enabled_clouds() if pin in cloud_tokens(c)]


def _mode_cn(mode):
    if mode == MODE_FALLBACK:
        return "自动（本地优先，失败降级云端链）"
    if mode == MODE_FORCE_LOCAL:
        return "强制本地（不降级）"
    if mode == MODE_FORCE_CLOUD:
        return "强制云端（整条链按序接力）"
    if mode.startswith(MODE_PIN_PREFIX):
        return "指定云端：%s" % mode[len(MODE_PIN_PREFIX):]
    return str(mode)


def _current_status_text():
    mode = ManualOverride.get_mode()
    clouds = enabled_clouds()
    local = CFG.get("local", {}) or {}
    lines = [
        "当前路由模式：%s（%s）" % (_mode_cn(mode), mode),
        "本地：%s  model=%s" % (local.get("base_url", "?"), local.get("model", "?")),
        "云端链：" + (", ".join("%s(%s)" % (c.get("name"), c.get("model")) for c in clouds) or "（无）"),
    ]
    rem = HEALTH.block_remaining(LOCAL_KEY)
    if rem > 0:
        lines.append("本地健康：冷却中（剩 %.0fs）%s" % (rem, HEALTH.last_error(LOCAL_KEY)))
    else:
        lines.append("本地健康：可用")
    for c in clouds:
        r = HEALTH.block_remaining(label_of(c))
        if r > 0:
            lines.append("熔断中：%s 剩 %.0fs" % (c.get("name"), r))
    pins = sorted({t for c in clouds for t in cloud_tokens(c)})
    r = router_cfg()
    lines.append("前缀模式：%s（模板 %r）｜未知 model：%s｜模式持久化：%s"
                 % (r.get("prefix_mode", "smart"), r.get("cloud_prefix", ""),
                    r.get("unknown_model", "error"), bool(r.get("persist_mode", False))))
    lines.append("可用命令：/route info | /route fallback | /route local | /route cloud"
                 + ("".join(" | /route " + p for p in pins) if pins else ""))
    return "\n".join(lines)


MODE_DESC = {
    MODE_FALLBACK: "已切换回【自动模式】：本地优先，本地失败自动降级云端链。",
    MODE_FORCE_LOCAL: "已切换为【强制本地】：只走本地模型，不再降级。",
    MODE_FORCE_CLOUD: "已切换为【强制云端】：按顺序接力整条云端链。",
}


# --------------------------------------------------------------------------
# 聊天内 /route 指令
# --------------------------------------------------------------------------
# 时间戳前缀：QwenPaw 等上层会在用户消息前注入，如 "[现在是26-10-12 15:12 周一]"
_TS_PREFIX_RE = re.compile(r"^\s*\[现在是[^\]]*\]\s*")


def _strip_ts_prefix(text):
    prev = None
    while prev != text:
        prev = text
        text = _TS_PREFIX_RE.sub("", text)
    return text.strip()


def _last_user_text(body):
    """取 messages 里最后一条 role==user 的文本内容，非纯文本返回 None。"""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return None
    for m in reversed(msgs):
        if not isinstance(m, dict):
            continue
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            only_text = True
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
                else:
                    only_text = False
                    break
            if only_text and texts:
                return "".join(texts)
            return None
        return None
    return None


def parse_route_command(body):
    """最后一条 user 消息是纯 /route 指令时返回 ("route", word) 或 ("status", None)。"""
    text = _last_user_text(body)
    if not isinstance(text, str):
        return None
    stripped = _strip_ts_prefix(text)
    if stripped in ("/route", "/route info", "/route status", "/route state",
                    "/route 状态", "/route 当前", "/route 当前模式"):
        return ("status", None)
    if stripped.startswith("/route"):
        rest = stripped[len("/route"):].strip()
        if rest and (" " not in rest) and ("\n" not in rest) and (rest in mode_alias_map()):
            return ("route", rest)
    return None


def _now_ts():
    return int(time.time())


def build_fake_completion(model, text):
    obj = {
        "id": "chatcmpl-router-%d" % _now_ts(),
        "object": "chat.completion",
        "created": _now_ts(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def build_fake_stream(model, text):
    cid = "chatcmpl-router-%d" % _now_ts()
    created = _now_ts()

    def _chunk(delta, finish=None):
        return {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    out = ["data: " + json.dumps(_chunk({"role": "assistant", "content": ""}), ensure_ascii=False) + "\n\n"]
    if text:
        out.append("data: " + json.dumps(_chunk({"content": text}), ensure_ascii=False) + "\n\n")
    out.append("data: " + json.dumps(_chunk({}, finish="stop"), ensure_ascii=False) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def _handle_chat_command(cmd, body):
    kind, arg = cmd
    is_stream = bool(body.get("stream"))
    model = CFG.get("local", {}).get("model", "router")
    if kind == "route":
        target = mode_alias_map().get(arg)
        old = ManualOverride.get_mode()
        ManualOverride.set_mode(target)
        LOG.info("CHAT-ROUTE: mode changed %s -> %s (arg=%r)" % (old, target, arg))
        text = MODE_DESC.get(target, "已切换为 %s。" % target)
        if target.startswith(MODE_PIN_PREFIX) and not chain_for_pin(target[len(MODE_PIN_PREFIX):]):
            text += "\n（警告：没有匹配的云端，检查 config.json 里的 name / alias）"
        text += "\n\n" + _current_status_text()
    else:
        LOG.info("CHAT-ROUTE: status query -> %s" % ManualOverride.get_mode())
        text = _current_status_text()
    if is_stream:
        LOG.info("CHAT-ROUTE: reply as SSE (stream=true)")
        return 200, build_fake_stream(model, text), "chat-route", "text/event-stream"
    LOG.info("CHAT-ROUTE: reply as JSON")
    return 200, build_fake_completion(model, text), "chat-route", "application/json"


# --------------------------------------------------------------------------
# 云端前缀
# --------------------------------------------------------------------------
def cloud_prefix_text(c):
    tmpl = router_cfg().get("cloud_prefix", "[cloud] ")
    if not tmpl:
        return ""
    return str(tmpl).replace("{name}", str(c.get("name", ""))).replace("{model}", str(c.get("model", "")))


def _prefix_regex(template):
    """把前缀模板编译成"能匹配历史里旧前缀"的正则。"""
    parts = re.split(r"(\{name\}|\{model\})", str(template))
    pat = ""
    for p in parts:
        if p == "{name}":
            pat += r"[^)\]]+"
        elif p == "{model}":
            pat += r"[^)\]]*"
        else:
            pat += re.escape(p)
    try:
        return re.compile(pat)
    except re.error:
        return None


def _text_of_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text") or "" for p in content
                       if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _last_prefix_in_history(body, template):
    """在对话历史里找最后一次出现的链路前缀（只扫尾部 40 条）。"""
    rx = _prefix_regex(template)
    msgs = body.get("messages")
    if rx is None or not isinstance(msgs, list):
        return None
    for m in reversed(msgs[-40:]):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        text = _text_of_content(m.get("content"))
        if not text:
            continue
        found = rx.findall(text)
        if found:
            return found[-1]
    return None


def effective_prefix(c, body):
    """按 prefix_mode 决定这次要不要加前缀。
       every：每次都加
       smart：历史里最后一条前缀跟这次不一样才加（防污染上下文）
       off  ：不加
    """
    tmpl = router_cfg().get("cloud_prefix", "[cloud] ")
    if not tmpl:
        return ""
    mode = str(router_cfg().get("prefix_mode", "smart")).lower()
    if mode in ("off", "none", "never", "no", "0", "false"):
        return ""
    prefix = cloud_prefix_text(c)
    if not prefix:
        return ""
    if mode in ("smart", "auto", "diff", "switch"):
        if _last_prefix_in_history(body, tmpl) == prefix:
            return ""
    return prefix


def is_multimodal(body):
    """请求里是否含图片/音频/视频等非文本内容。"""
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") not in (None, "text"):
                return True
    return False


def want_reasoning_fallback():
    v = str(router_cfg().get("empty_content_fallback", "reasoning")).lower()
    return v not in ("off", "none", "no", "0", "false", "")


def reasoning_marker():
    return str(router_cfg().get("reasoning_fallback_marker", "") or "")


def local_model_ids():
    local = CFG.get("local", {}) or {}
    ids = [local.get("model"), local.get("model_full_name")]
    alias = local.get("alias")
    if isinstance(alias, list):
        ids += alias
    elif isinstance(alias, str):
        ids.append(alias)
    return [i for i in ids if i]


def known_model_ids():
    """所有能被指定为 model 的名字（用于 400 报错提示和 /v1/models）。"""
    ids = ["auto"] + local_model_ids()
    for c in enabled_clouds():
        ids.append(c.get("model"))
        ids += cloud_tokens(c)
    out = []
    for i in ids:
        if i and i not in out:
            out.append(i)
    return out


def apply_json_transforms(resp_bytes, prefix, reasoning_fallback=False, marker=""):
    """非流式返回体加工：
       * 给首个文本 content 加链路前缀（工具调用响应一律不动）
       * content 为空但 reasoning_content 有内容时兜底补上
       返回 (新字节, flags)
    """
    flags = {}
    if not prefix and not reasoning_fallback:
        return resp_bytes, flags
    try:
        obj = json.loads(resp_bytes.decode("utf-8"))
    except Exception:
        return resp_bytes, flags
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return resp_bytes, flags
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message")
        if isinstance(msg, dict):
            if msg.get("tool_calls") or ch.get("finish_reason") == "tool_calls":
                continue
            content = msg.get("content")
            if reasoning_fallback and not (isinstance(content, str) and content.strip()):
                think = msg.get("reasoning_content")
                if not isinstance(think, str) or not think.strip():
                    think = msg.get("reasoning")
                if isinstance(think, str) and think.strip():
                    msg["content"] = marker + think
                    content = msg["content"]
                    flags["reasoning_fallback"] = True
            if prefix and isinstance(content, str) and content and not content.startswith(prefix):
                msg["content"] = prefix + content
        text = ch.get("text")
        if prefix and isinstance(text, str) and text and not text.startswith(prefix):
            ch["text"] = prefix + text
    return json.dumps(obj, ensure_ascii=False).encode("utf-8"), flags


class StreamTransformer:
    """流式加工：
       * 只在首个非空 content 分片前注入前缀；一旦出现 tool_calls 就彻底不加
       * 全程 content 为空但 reasoning_content 有内容时，在收尾分片前补一个内容分片
    """

    def __init__(self, prefix, reasoning_fallback=False, marker=""):
        self.prefix = prefix or ""
        self.prefix_done = not bool(self.prefix)
        self.reasoning_fallback = bool(reasoning_fallback)
        self.marker = marker or ""
        self.content_seen = ""
        self.reasoning_seen = ""
        self.tool_calls = False
        self.fallback_done = False

    def transform_line(self, line):
        if not line.startswith("data:"):
            return line
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return line
        try:
            obj = json.loads(payload)
        except Exception:
            return line
        choices = obj.get("choices")
        if not isinstance(choices, list):
            return line
        injected = False
        finish = False
        for ch in choices:
            if not isinstance(ch, dict):
                continue
            for holder in ("delta", "message"):
                box = ch.get(holder)
                if not isinstance(box, dict):
                    continue
                if box.get("tool_calls"):
                    self.tool_calls = True        # 工具调用：整个流都不加前缀
                    self.prefix_done = True
                content = box.get("content")
                if isinstance(content, str) and content:
                    self.content_seen += content
                    if not self.prefix_done:
                        box["content"] = self.prefix + content
                        self.prefix_done = True
                        injected = True
                think = box.get("reasoning_content")
                if isinstance(think, str) and think:
                    self.reasoning_seen += think
            if ch.get("finish_reason"):
                finish = True
        if (finish and self.reasoning_fallback and not self.tool_calls and not self.fallback_done
                and not self.content_seen.strip() and self.reasoning_seen.strip()):
            self.fallback_done = True
            head = "" if self.prefix_done else self.prefix
            self.prefix_done = True
            filler = dict(obj)
            filler["choices"] = [{"index": 0, "delta": {"content": self.marker + head + self.reasoning_seen},
                                  "finish_reason": None}]
            tail = "data: " + json.dumps(obj, ensure_ascii=False) if injected else line
            return "data: " + json.dumps(filler, ensure_ascii=False) + "\n\n" + tail
        if injected:
            return "data: " + json.dumps(obj, ensure_ascii=False)
        return line


def _iter_sse(resp, prefix, reasoning_fallback=False, marker=""):
    """把上游 SSE 逐行透传，并按需注入前缀 / 兜底空正文。"""
    injector = StreamTransformer(prefix, reasoning_fallback, marker)
    try:
        while True:
            line = resp.readline()
            if not line:
                break
            try:
                text = line.decode("utf-8")
            except Exception:
                yield line
                continue
            ending = ""
            if text.endswith("\r\n"):
                ending, text = "\r\n", text[:-2]
            elif text.endswith("\n"):
                ending, text = "\n", text[:-1]
            yield (injector.transform_line(text) + ending).encode("utf-8")
    finally:
        try:
            resp.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 路由计划
# --------------------------------------------------------------------------
_CTX = threading.local()


def current_info():
    """当前请求的路由信息（响应头 X-Router-* 用）。"""
    return getattr(_CTX, "info", None) or {}


def build_plan(body):
    plan, info = _build_plan(body)
    _CTX.info = info
    return plan, info


def _build_plan(body):
    """返回 (plan, info)。plan = [(kind, endpoint_cfg, label)]，按顺序尝试。"""
    router = router_cfg()
    mode = ManualOverride.get_mode()
    clouds = enabled_clouds()
    local = dict(CFG.get("local", {}) or {})
    info = {}

    req_model = body.get("model")
    local_names = set(local_model_ids())

    cloud_by_model = {}
    for c in clouds:
        cloud_by_model.setdefault(c.get("model"), c)
    cloud_by_token = {}
    for c in clouds:
        for t in cloud_tokens(c):
            cloud_by_token.setdefault(t, c)

    pin = mode[len(MODE_PIN_PREFIX):] if mode.startswith(MODE_PIN_PREFIX) else None

    if req_model in local_names:
        info["why"] = "model=%s -> 本地" % req_model
        return [("local", local, "local")], info
    if req_model in cloud_by_model:
        c = cloud_by_model[req_model]
        plan = [("cloud", c, label_of(c))]
        if router.get("pin_fallback", False):
            plan += [("cloud", x, label_of(x)) for x in clouds if x is not c]
        info["why"] = "model=%s -> %s" % (req_model, c.get("name"))
        return plan, info
    if req_model in cloud_by_token:
        c = cloud_by_token[req_model]
        info["why"] = "model(别名)=%s -> %s" % (req_model, c.get("name"))
        return [("cloud", c, label_of(c))], info

    # 没配置过的 model 名：默认直接报错，避免"随手写个名字结果静默走了云端"
    if req_model and str(req_model) not in AUTO_TOKENS:
        policy = str(router.get("unknown_model", "error")).lower()
        info["unknown_model"] = req_model
        if policy in ("error", "strict", "reject", "400"):
            info["why"] = "model=%s 未配置（unknown_model=error）" % req_model
            return [], info
        info["note"] = "model=%s 未配置 -> 按 %s 退化为自动模式" % (req_model, policy)

    if pin:
        chain = chain_for_pin(pin)
        info["why"] = "pin=%s -> %s" % (pin, [c.get("name") for c in chain] or "无匹配，退回整条链")
        chain = chain or clouds
        return [("cloud", c, label_of(c)) for c in chain], info

    if mode == MODE_FORCE_LOCAL:
        info["why"] = "强制本地"
        return [("local", local, "local")], info

    if mode == MODE_FORCE_CLOUD:
        info["why"] = "强制云端（整条链接力）"
        return [("cloud", c, label_of(c)) for c in clouds], info

    # fallback / auto
    if is_multimodal(body) and router.get("multimodal_force_local", True):
        info["multimodal"] = "请求含图片/视频等非文本内容"
        info["why"] = "自动模式：多模态请求强制本地"
        if HEALTH.is_blocked(LOCAL_KEY):
            info["reject"] = "带图请求不降级到纯文本云端，而本地模型当前不可用（冷却中）"
            return [], info
        return [("local", local, "local")], info

    use_local = bool(router.get("prefer_local", True))
    if use_local and HEALTH.is_blocked(LOCAL_KEY):
        use_local = False
        info["skip_local"] = "健康探测/熔断冷却中"
    if use_local:
        sr = router.get("smart_route", {}) or {}
        if sr.get("enabled", True):
            est = estimate_tokens(body, sr.get("bytes_per_token", 4))
            info["est_tokens"] = est
            max_ctx = int(sr.get("local_max_ctx", 0) or 0)
            if max_ctx and est > max_ctx:
                use_local = False
                info["skip_local"] = "estimated %d tokens > local_max_ctx %d" % (est, max_ctx)
    info["why"] = "自动模式"
    plan = []
    if use_local:
        plan.append(("local", local, "local"))
    plan += [("cloud", c, label_of(c)) for c in clouds]
    return plan, info


def _error_body(message, tried):
    return json.dumps({
        "error": {
            "message": message,
            "type": "upstream_error",
            "source": "none",
            "tried": [{"target": t[0], "reason": t[1], "detail": t[2]} for t in tried],
        }
    }, ensure_ascii=False).encode("utf-8")


def _unknown_model_body(req_model):
    return json.dumps({
        "error": {
            "message": ("model=%r 没在 config.json 里配置过。可用：%s。"
                        "（想让 router 自己决定就填 auto；不想要这个报错就把 "
                        "router.unknown_model 改成 auto）") % (req_model, ", ".join(known_model_ids())),
            "type": "invalid_model",
            "source": "router",
        }
    }, ensure_ascii=False).encode("utf-8")


def _note_flags(label, flags):
    """记录本次响应的加工标记（响应头 X-Router-Reasoning-Fallback 用）。"""
    if not flags:
        return
    cur = dict(getattr(_CTX, "flags", None) or {})
    cur.update(flags)
    _CTX.flags = cur
    if flags.get("reasoning_fallback"):
        LOG.warn("%s content 为空，已用 reasoning_content 兜底（empty_content_fallback）" % label)


def current_flags():
    return getattr(_CTX, "flags", None) or {}


def header_safe(value):
    """HTTP 头只能是 latin-1：非 ASCII 一律百分号编码，既不丢信息也不让 send_header 抛异常。"""
    raw = str(value).replace("\r", " ").replace("\n", " ").encode("utf-8")
    out = []
    for b in raw:
        out.append(chr(b) if 0x20 <= b < 0x7f else "%%%02X" % b)
    return "".join(out)


def router_headers(source):
    """每个响应都带上：走的是哪条链路 / 当前模式 / 为什么。"""
    info = current_info()
    flags = current_flags()
    hdrs = {
        "X-Router-Source": header_safe(source),
        "X-Router-Mode": header_safe(ManualOverride.get_mode()),
    }
    why = []
    if info.get("why"):
        why.append(str(info["why"]))
    if info.get("skip_local"):
        why.append("skip_local: %s" % info["skip_local"])
    if info.get("multimodal"):
        why.append(str(info["multimodal"]))
    if info.get("est_tokens"):
        why.append("est_tokens=%s" % info["est_tokens"])
    if flags.get("reasoning_fallback"):
        why.append("reasoning_fallback")
    if why:
        hdrs["X-Router-Why"] = header_safe("; ".join(why))
    return hdrs


def _key_of(kind, label):
    return LOCAL_KEY if kind == "local" else label


def _skip_blocked(kind, label, tried):
    key = _key_of(kind, label)
    rem = HEALTH.block_remaining(key)
    if rem <= 0:
        return False
    tried.append((label, "skipped", "health/circuit blocked %.0fs" % rem))
    LOG.warn("skip %s (blocked %.0fs, last: %s)" % (label, rem, HEALTH.last_error(key)))
    return True


def _handle_chat_if_needed(body):
    if not router_cfg().get("chat_route_enabled", True):
        return None
    cmd = parse_route_command(body)
    if router_cfg().get("chat_route_debug", False):
        last = _last_user_text(body)
        shown = repr(last)[:200] if isinstance(last, str) else repr(last)
        LOG.info("CHAT-ROUTE-DBG: last_user=%s -> cmd=%s" % (shown, cmd))
    if cmd is None:
        return None
    return _handle_chat_command(cmd, body)


# --------------------------------------------------------------------------
# 路由核心：非流式
# --------------------------------------------------------------------------
def route_chat(body):
    """返回 (status, resp_bytes, source, content_type)。"""
    routed = _handle_chat_if_needed(body)
    if routed is not None:
        return routed

    plan, info = build_plan(body)
    mode = ManualOverride.get_mode()
    LOG.info("plan mode=%s info=%s plan=%s" % (mode, info, [p[2] for p in plan]))

    rf, mk = want_reasoning_fallback(), reasoning_marker()
    tried = []
    for kind, ep, label in plan:
        if _skip_blocked(kind, label, tried):
            continue
        b = dict(body)
        b["model"] = ep["model"]
        LOG.info("-> %s  model=%s" % (label, ep["model"]))
        try:
            status, resp = call_upstream(ep, b)
        except UpstreamError as e:
            HEALTH.note_failure(_key_of(kind, label), e.reason, e.detail)
            tried.append((label, e.reason, e.detail))
            LOG.warn("!! %s failed: %s :: %s" % (label, e.reason, e.detail))
            if kind == "local" and not should_fallback(e):
                break
            continue
        HEALTH.note_success(_key_of(kind, label))
        if kind == "local":
            resp, flags = apply_json_transforms(resp, "", rf, mk)
            _note_flags(label, flags)
            return status, resp, "local", "application/json"
        prefix = effective_prefix(ep, body)
        resp, flags = apply_json_transforms(resp, prefix, rf, mk)
        _note_flags(label, flags)
        LOG.info("<- %s  status=%s  prefix=%r" % (label, status, prefix))
        return status, resp, label, "application/json"

    if info.get("unknown_model"):
        return 400, _unknown_model_body(info["unknown_model"]), "none", "application/json"
    if info.get("reject"):
        return 400, _error_body(info["reject"], tried), "none", "application/json"
    if tried and all(t[1] == "skipped" for t in tried):
        return 503, _error_body("all upstreams unavailable (health/circuit)", tried), "none", "application/json"
    return 502, _error_body("all upstreams failed", tried), "none", "application/json"


# --------------------------------------------------------------------------
# 路由核心：流式（真透传）
# --------------------------------------------------------------------------
def route_chat_stream(body):
    """返回 (status, payload, source, content_type)。

    payload 是 bytes（错误 / 模拟响应）或迭代器（上游 SSE 字节块）。
    """
    routed = _handle_chat_if_needed(body)
    if routed is not None:
        return routed

    plan, info = build_plan(body)
    mode = ManualOverride.get_mode()
    LOG.info("plan(stream) mode=%s info=%s plan=%s" % (mode, info, [p[2] for p in plan]))

    rf, mk = want_reasoning_fallback(), reasoning_marker()
    tried = []
    for kind, ep, label in plan:
        if _skip_blocked(kind, label, tried):
            continue
        b = dict(body)
        b["model"] = ep["model"]
        LOG.info("-> %s  model=%s (stream)" % (label, ep["model"]))
        try:
            resp = open_upstream_stream(ep, b)
        except UpstreamError as e:
            HEALTH.note_failure(_key_of(kind, label), e.reason, e.detail)
            tried.append((label, e.reason, e.detail))
            LOG.warn("!! %s failed: %s :: %s" % (label, e.reason, e.detail))
            if kind == "local" and not should_fallback(e):
                break
            continue
        HEALTH.note_success(_key_of(kind, label))
        if kind == "local":
            LOG.info("<- local  streaming")
            return 200, _iter_sse(resp, "", rf, mk), "local", "text/event-stream; charset=utf-8"
        prefix = effective_prefix(ep, body)
        LOG.info("<- %s  streaming  prefix=%r" % (label, prefix))
        return 200, _iter_sse(resp, prefix, rf, mk), label, "text/event-stream; charset=utf-8"

    if info.get("unknown_model"):
        return 400, _unknown_model_body(info["unknown_model"]), "none", "application/json"
    if info.get("reject"):
        return 400, _error_body(info["reject"], tried), "none", "application/json"
    if tried and all(t[1] == "skipped" for t in tried):
        return 503, _error_body("all upstreams unavailable (health/circuit)", tried), "none", "application/json"
    return 502, _error_body("all upstreams failed", tried), "none", "application/json"


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- 输出 ----
    def _send(self, status, body, content_type="application/json", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        body = bytes(body)
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            if "text/event-stream" in content_type:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            LOG.warn("failed to send response: %r" % (e,))

    def _stream(self, chunks, status=200, content_type="text/event-stream; charset=utf-8",
                extra_headers=None):
        """真流式透传：chunked 编码，边收边发。"""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
        except Exception as e:
            LOG.warn("failed to send stream headers: %r" % (e,))
            return
        self.close_connection = True
        try:
            for chunk in chunks:
                if not chunk:
                    continue
                self.wfile.write(("%X\r\n" % len(chunk)).encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except Exception as e:
            LOG.error("stream aborted: %r" % (e,))
        finally:
            try:
                if hasattr(chunks, "close"):
                    chunks.close()
            except Exception:
                pass
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    # ---- GET ----
    def do_GET(self):
        if self.path.startswith("/health"):
            snap = HEALTH.snapshot()
            now = time.time()
            clouds = []
            for c in CFG.get("clouds", []):
                clouds.append({
                    "name": c.get("name"),
                    "model": c.get("model"),
                    "enabled": bool(c.get("enabled", True)),
                    "blocked_for": round(max(0.0, snap["open_until"].get(label_of(c), 0.0) - now), 1),
                    "fails": snap["fails"].get(label_of(c), 0),
                })
            self._send(200, json.dumps({
                "status": "ok",
                "service": "llm-router",
                "mode": ManualOverride.get_mode(),
                "mode_text": _mode_cn(ManualOverride.get_mode()),
                "prefix_mode": str(router_cfg().get("prefix_mode", "smart")),
                "unknown_model": str(router_cfg().get("unknown_model", "error")),
                "persist_mode": bool(router_cfg().get("persist_mode", False)),
                "models": known_model_ids(),
                "local": {
                    "model": CFG.get("local", {}).get("model"),
                    "blocked_for": round(HEALTH.block_remaining(LOCAL_KEY), 1),
                    "fails": snap["fails"].get(LOCAL_KEY, 0),
                    "last_error": snap["last_error"].get(LOCAL_KEY, ""),
                },
                "clouds": clouds,
            }, ensure_ascii=False))
            return

        if self.path.startswith("/v1/models"):
            local = CFG.get("local", {}) or {}
            local_mods = local.get("input_modalities", ["text", "image", "video"])
            models = [
                {
                    "id": local.get("model", "local"),
                    "object": "model",
                    "owned_by": "local",
                    "input_modalities": local_mods,
                    "capabilities": local.get("capabilities", ["completion", "multimodal"]),
                    "supports_multimodal": True,
                },
                {
                    "id": "auto",
                    "object": "model",
                    "owned_by": "router",
                    "input_modalities": local_mods,
                    "capabilities": ["completion"],
                },
            ]
            for c in CFG.get("clouds", []):
                entry = {
                    "id": c.get("model"),
                    "object": "model",
                    "owned_by": c.get("name", "cloud"),
                    "input_modalities": c.get("input_modalities", ["text"]),
                    "capabilities": c.get("capabilities", ["completion"]),
                }
                models.append(entry)
                if not c.get("enabled", True):
                    continue
                for t in cloud_tokens(c):
                    if t and t != c.get("model"):
                        alt = dict(entry)
                        alt["id"] = t
                        alt["alias_of"] = c.get("name")
                        models.append(alt)
            loc_alias = local.get("alias")
            if isinstance(loc_alias, str):
                loc_alias = [loc_alias]
            for t in (loc_alias or []):
                if t and t != local.get("model"):
                    models.append({
                        "id": t,
                        "object": "model",
                        "owned_by": "local",
                        "input_modalities": local_mods,
                        "capabilities": local.get("capabilities", ["completion", "multimodal"]),
                        "supports_multimodal": True,
                        "alias_of": "local",
                    })
            self._send(200, json.dumps({"object": "list", "data": models}, ensure_ascii=False))
            return

        if self.path.startswith("/route"):
            self._handle_route()
            return

        self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    # ---- POST ----
    def do_POST(self):
        if self.path.startswith("/route"):
            self._handle_route()
            return
        if not (self.path.startswith("/v1/chat/completions") or self.path.startswith("/chat/completions")):
            self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))
            return
        _CTX.info = {}
        _CTX.flags = {}
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:
            self._send(400, json.dumps({"error": {"message": "bad request: %s" % e}}, ensure_ascii=False))
            return

        is_stream = bool(body.get("stream"))
        try:
            if is_stream:
                status, payload, source, ctype = route_chat_stream(body)
            else:
                status, payload, source, ctype = route_chat(body)
        except Exception as e:
            LOG.error("route crashed: %r" % (e,))
            self._send(500, json.dumps({"error": {"message": "router crash: %r" % (e,)}},
                                       ensure_ascii=False))
            return

        if isinstance(payload, (bytes, bytearray)):
            LOG.info("<- client source=%s status=%s bytes=%d type=%s"
                     % (source, status, len(payload), ctype))
            self._send(status, bytes(payload), content_type=ctype,
                       extra_headers=router_headers(source))
        else:
            LOG.info("<- client source=%s status=%s (stream) type=%s" % (source, status, ctype))
            self._stream(payload, status=status, content_type=ctype,
                         extra_headers=router_headers(source))

    # ---- /route ----
    def _handle_route(self):
        """GET /route?mode=xxx 或 POST /route {"mode":"xxx"}；缺省查询当前模式。"""
        word = None
        if self.command == "GET":
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            word = (params.get("mode") or params.get("route") or [None])[0]
        else:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                if isinstance(payload, dict):
                    word = payload.get("mode") or payload.get("route")
            except Exception:
                word = None

        aliases = mode_alias_map()
        if word and word in aliases:
            target = aliases[word]
            old = ManualOverride.get_mode()
            ManualOverride.set_mode(target)
            LOG.info("ROUTE: mode changed %s -> %s (word=%r)" % (old, target, word))
            resp = {"status": "ok", "mode": target, "previous": old, "info": _current_status_text()}
        elif word:
            resp = {"status": "error", "message": "unknown mode: %s" % word,
                    "available": sorted(aliases.keys())}
        else:
            resp = {"status": "ok", "mode": ManualOverride.get_mode(), "info": _current_status_text()}
        self._send(200, json.dumps(resp, ensure_ascii=False))


def main():
    host = CFG["server"]["host"]
    port = CFG["server"]["port"]
    LOG.info("LLM Router starting on http://%s:%s  (config=%s)" % (host, port, CONFIG_PATH))
    init_mode, init_src = init_mode_from_config()
    LOG.info("  mode  : %s (%s, persist_mode=%s)"
             % (init_mode, init_src, bool(router_cfg().get("persist_mode", False))))
    LOG.info("  prefix: %r  prefix_mode=%s  unknown_model=%s  empty_content_fallback=%s"
             % (router_cfg().get("cloud_prefix"), router_cfg().get("prefix_mode", "smart"),
                router_cfg().get("unknown_model", "error"),
                router_cfg().get("empty_content_fallback", "reasoning")))
    lc = CFG.get("log", {}) or {}
    LOG.info("  log   : %s (max_bytes=%s, backup_count=%s)"
             % (LOG.path, lc.get("max_bytes", 0), lc.get("backup_count", 3)))
    local = CFG.get("local", {}) or {}
    LOG.info("  local : %s  model=%s" % (local.get("base_url"), local.get("model")))
    for c in CFG.get("clouds", []):
        state = "ON " if c.get("enabled", True) else "OFF"
        key_state = "key:set" if c.get("api_key") else "key:MISSING"
        LOG.info("  cloud [%s] %s: %s model=%s (%s)"
                 % (state, c.get("name"), c.get("base_url"), c.get("model"), key_state))
    lh = router_cfg().get("local_health", {}) or {}
    if lh.get("enabled", True):
        t = threading.Thread(target=local_probe_loop, name="local-probe", daemon=True)
        t.start()
        LOG.info("  local health probe: every %ss (cooldown %ss)"
                 % (lh.get("interval", 30), lh.get("cooldown", 60)))
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
