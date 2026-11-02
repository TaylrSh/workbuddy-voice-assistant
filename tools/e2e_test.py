"""端到端链路测试：等 WorkBuddy 空闲 → 注入一条指令 → 等待回复。

因为 WorkBuddy 跑任务时输入框不可用，这个脚本会先轮询到空闲再测。

用法：
    python tools/e2e_test.py [--wait 600] [--timeout 180]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import wb_cdp
import voice_assistant as va

MARKER = "VOICE_E2E_PROBE"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=float, default=600.0, help="等待空闲的最长秒数")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待回复超时")
    parser.add_argument("--out", default=str(BASE / "recordings" / "e2e_result.txt"))
    args = parser.parse_args()

    lines: list[str] = []

    def say(text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        lines.append(f"[{stamp}] {text}")
        print(lines[-1], flush=True)

    say("等待 WorkBuddy 空闲…")
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        status = wb_cdp.composer_status()
        if not status.get("busy"):
            say("WorkBuddy 已空闲")
            break
        say(f"仍忙碌：{status}")
        time.sleep(5)
    else:
        say("等待空闲超时")
        Path(args.out).write_text("\n".join(lines), encoding="utf-8")
        return 2

    assistant = va.VoiceAssistant(timeout=args.timeout, speak_replies=False,
                                 wait_idle=30.0)
    prompt = (
        "这是语音助手的自动化链路自检消息，请严格按下面格式回复，不要做任何别的事：\n"
        f"<transcript>{MARKER}</transcript>"
        f"<final>{MARKER}</final>"
    )
    say("注入测试指令…")
    started = time.monotonic()
    reply = assistant.process(prompt)
    elapsed = time.monotonic() - started

    final = va.wb_acp.extract_tag(reply, "final")
    say(f"耗时 {elapsed:.1f}s")
    say(f"原始回复片段：{reply[:200]!r}")
    say(f"提取到的 final：{final!r}")

    ok = MARKER in reply
    say("结果：" + ("通过" if ok else "失败"))

    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已写入 {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
