"""真正创建悬浮窗并截图，验证 Win32 分层窗口合成是否正常。

用法：
    python tools/shot_window.py [--dark]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import wb_capture
import wb_overlay as ov


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()

    out_dir = BASE / "recordings"
    out_dir.mkdir(parents=True, exist_ok=True)

    overlay = ov.Overlay(dark=args.dark, scale=args.scale)
    overlay.create()
    time.sleep(0.4)
    overlay.render(force=True)
    time.sleep(0.2)

    suffix = "dark" if args.dark else "light"
    bubble_png = out_dir / f"_win_bubble_{suffix}.png"
    ok = wb_capture.capture_window(overlay.hwnd, bubble_png)
    print("气泡窗口 ->", bubble_png if ok else "截图失败")

    # 展开面板
    overlay.expand(True)
    overlay.set_transcript("帮我看看这个项目现在跑到哪一步了")
    overlay.set_reply("已经跑完单元测试，7 个用例里 6 个通过，剩下 1 个是网络超时，不影响主流程。")
    overlay.set_state(ov.STATE_THINKING, "WorkBuddy 处理中… 3s")
    overlay.render(force=True)
    time.sleep(0.3)
    panel_png = out_dir / f"_win_panel_{suffix}.png"
    ok = wb_capture.capture_window(overlay.hwnd, panel_png)
    print("面板窗口 ->", panel_png if ok else "截图失败")

    print("窗口句柄:", overlay.hwnd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
