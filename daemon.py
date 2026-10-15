#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台守护启动器：把 router.py 脱离当前进程独立运行。

启动前先检查端口，避免出现两个路由器抢同一个端口（互相看不见对方的 /route 模式）。
"""

import json
import os
import socket
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ROUTER = os.path.join(BASE, "router.py")
CONFIG = os.environ.get("LLM_ROUTER_CONFIG") or os.path.join(BASE, "config.json")
LOG = os.path.join(BASE, "logs", "stdout.log")
PID_FILE = os.path.join(BASE, "router.pid")
os.makedirs(os.path.dirname(LOG), exist_ok=True)


def port_in_use(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        port = cfg.get("server", {}).get("port", 8100)
        host = str(cfg.get("server", {}).get("host", "127.0.0.1") or "127.0.0.1")
    except Exception as e:
        print("读不到配置 %s: %s" % (CONFIG, e))
        return 1

    # 0.0.0.0 / :: 监听时 127.0.0.1 一定能连上；绑到具体网卡就用那个地址探
    probe_host = host if host not in ("0.0.0.0", "::", "", "localhost") else "127.0.0.1"

    if port_in_use(port, probe_host):
        print("端口 %s (%s) 已有服务在监听 —— 可能路由器已经在跑了。" % (port, probe_host))
        print("要重启请先执行 stop.bat，再来一次。")
        return 1

    # Windows: 用 DETACHED_PROCESS 彻底脱离父进程
    DETACHED = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    f = open(LOG, "a", encoding="utf-8")
    p = subprocess.Popen(
        [PY, ROUTER],
        cwd=BASE,
        stdout=f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=DETACHED | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    print("llm-router started, pid=%s, port=%s" % (p.pid, port))
    # 记下 pid，stop.bat 靠它精确收进程（不再靠 netstat 猜端口）
    try:
        with open(PID_FILE, "w", encoding="utf-8") as pf:
            pf.write(str(p.pid))
    except Exception as e:
        print("warn: cannot write %s: %s" % (PID_FILE, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
