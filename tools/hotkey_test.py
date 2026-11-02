"""回归测试：真实发送按键，验证热键能正确触发与复位。

曾经的 bug：
  1. CallNextHookEx 传了未初始化的钩子句柄 → ctypes 回调抛异常 →
     pywin32 直接终止进程（按快捷键就闪退）
  2. 只比较 VK_CONTROL/VK_MENU，而钩子报的是 0xA2/0xA4 → 热键永不触发
  3. wParam 掩码用错 → WM_KEYUP 被当成 WM_KEYDOWN → 按住不放不松手
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import wb_hotkey as hk

KEYEVENTF_KEYUP = 0x0002


def kb(vk: int, up: bool = False) -> None:
    hk.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def main() -> int:
    listener = hk.PushToTalk()
    if not listener.start():
        print("[FAIL] 钩子安装失败")
        return 1
    print("[OK] 钩子已安装")

    failures: list[str] = []
    try:
        # 1) Ctrl+Alt+V 应触发
        kb(0x11); kb(0x12); kb(0x56)
        time.sleep(0.3)
        if not listener.held.is_set():
            failures.append("Ctrl+Alt+V 按下没有触发")
        else:
            print("[OK] Ctrl+Alt+V 按下触发")

        # 2) 松开应复位
        kb(0x56, True); kb(0x12, True); kb(0x11, True)
        time.sleep(0.3)
        if listener.held.is_set():
            failures.append("松开后没有复位")
        else:
            print("[OK] 松开复位")

        # 3) 单独按 V 不应触发
        kb(0x56)
        time.sleep(0.25)
        if listener.held.is_set():
            failures.append("单独按 V 误触发")
        else:
            print("[OK] 单独 V 不误触发")
        kb(0x56, True)

        # 4) 右 Alt + Ctrl + V 也应触发
        time.sleep(0.15)
        kb(0x11); kb(0xA5); kb(0x56)
        time.sleep(0.3)
        if not listener.held.is_set():
            failures.append("右 Alt + Ctrl + V 没有触发")
        else:
            print("[OK] 右 Alt 也能触发")
        kb(0x56, True); kb(0xA5, True); kb(0x11, True)
        time.sleep(0.2)
    finally:
        listener.stop()

    if failures:
        print("\n[FAIL] 以下问题未解决：")
        for f in failures:
            print("  -", f)
        return 1

    print("\n热键回归测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
