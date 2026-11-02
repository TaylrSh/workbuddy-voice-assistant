"""启动 WorkBuddy 桌面版，并开启 CDP 调试端口。

桌面版内置了 remote-debugging 支持：只要环境变量
WORKBUDDY_REMOTE_DEBUGGING_PORT 是纯数字，启动时就会开启对应的 CDP 端口。
本脚本负责带上这个环境变量启动 WorkBuddy。

用法：
    python start_workbuddy.py            # 启动（若已在运行则提示）
    python start_workbuddy.py --port 9333
    python start_workbuddy.py --check    # 只检查状态
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEBUG_ENV = "WORKBUDDY_REMOTE_DEBUGGING_PORT"
DEFAULT_PORT = 9333
EXE = Path(r"D:\Program Files (x86)\WorkBuddy\WorkBuddy.exe")


def cdp_ready(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def workbuddy_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.lower()
        return "workbuddy.exe" in out
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--check", action="store_true", help="只检查状态")
    parser.add_argument("--restart", action="store_true", help="先关闭已有实例再启动")
    args = parser.parse_args()

    if args.check:
        print(f"WorkBuddy 运行中: {workbuddy_running()}")
        print(f"CDP 端口 {args.port} 可用: {cdp_ready(args.port)}")
        return 0

    if not EXE.exists():
        print(f"找不到 WorkBuddy 可执行文件：{EXE}")
        return 1

    if args.restart and workbuddy_running():
        print("正在关闭 WorkBuddy…")
        subprocess.run(
            ["taskkill", "/IM", "WorkBuddy.exe", "/F"],
            capture_output=True,
            timeout=30,
        )
        time.sleep(4)

    if workbuddy_running():
        if cdp_ready(args.port):
            print(f"WorkBuddy 已在运行，CDP 端口 {args.port} 可用。")
            return 0
        print(
            "WorkBuddy 已在运行，但 CDP 未开启。\n"
            f"需要用 {DEBUG_ENV}={args.port} 重启它才能启用 DOM 注入。\n"
            "重新执行：python start_workbuddy.py --restart"
        )
        return 1

    env = dict(os.environ)
    env[DEBUG_ENV] = str(args.port)

    print(f"启动 WorkBuddy（CDP 端口 {args.port}）…")
    subprocess.Popen(
        [str(EXE)],
        env=env,
        cwd=str(EXE.parent),
        detached=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    for _ in range(40):
        time.sleep(1)
        if cdp_ready(args.port):
            print(f"启动成功，CDP 端口 {args.port} 已就绪。")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{args.port}/json/list", timeout=3
                ) as resp:
                    import json

                    pages = json.loads(resp.read().decode("utf-8"))
                    pages = [p for p in pages if p.get("type") == "page"]
                    print(f"可调试页面 {len(pages)} 个：")
                    for page in pages[:6]:
                        title = (page.get("title") or "")[:48]
                        print(f"  - {title}")
            except Exception as exc:
                print("CDP 已就绪（页面列表读取失败）：", exc)
            return 0

    print("启动超时，CDP 端口未就绪。请手动打开 WorkBuddy 确认。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
