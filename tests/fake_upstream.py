#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试用假上游：多端口、多种失败模式，用来验证 router 的降级/前缀/流式行为。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(behavior):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/models"):
                self._json(200, {"object": "list", "data": []})
            else:
                self._json(404, {"error": "not found"})

        def _sse(self, chunks, model):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for ch in chunks:
                ch["id"], ch["object"] = "c1", "chat.completion.chunk"
                ch["model"], ch["created"] = model, 1
                payload = ("data: " + json.dumps(ch, ensure_ascii=False) + "\n\n").encode("utf-8")
                self.wfile.write(("%X\r\n" % len(payload)).encode() + payload + b"\r\n")
                self.wfile.flush()
                time.sleep(0.1)
            done = b"data: [DONE]\n\n"
            self.wfile.write(("%X\r\n" % len(done)).encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            self.close_connection = True

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}
            if behavior == "401":
                return self._json(401, {"error": {"message": "invalid api key"}})
            if behavior == "429":
                return self._json(429, {"error": {"message": "rate limited"}})
            if behavior == "500":
                return self._json(500, {"error": {"message": "boom"}})

            model = body.get("model")
            last = ""
            for m in reversed(body.get("messages") or []):
                if isinstance(m, dict) and m.get("role") == "user":
                    c = m.get("content")
                    last = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    break

            if behavior == "reasoning":
                # 思考模型：content 为空串，正文全在 reasoning_content 里
                think = "THINK-ONLY-ANSWER"
                if body.get("stream"):
                    self._sse([
                        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                      "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": {"reasoning_content": think},
                                      "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    ], model)
                    return
                return self._json(200, {
                    "id": "c4", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": "",
                                             "reasoning_content": think},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

            if body.get("stream"):
                self._sse([
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {"content": "HELLO-"}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {"content": "WORLD"}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ], model)
                return

            if "TOOL" in last:
                return self._json(200, {
                    "id": "c2", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": None,
                                    "tool_calls": [{"id": "call_1", "type": "function",
                                                    "function": {"name": "ping", "arguments": "{}"}}]},
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

            return self._json(200, {
                "id": "c3", "object": "chat.completion", "created": 1, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant",
                                                     "content": "served-by:%s" % model},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

    return Handler


def serve(port, behavior):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(behavior))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve(8097, "401")
    serve(8098, "ok")
    serve(8099, "429")
    while True:
        time.sleep(1)
