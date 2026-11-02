"""验证悬浮窗的交互命中：点击展开、点击关闭、点击输入框。

用法：
    python tools/test_click.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import wb_overlay as ov


def click(overlay, x: int, y: int) -> None:
    lparam = (y << 16) | (x & 0xFFFF)
    overlay._on_mouse_down(overlay.hwnd, lparam)
    overlay._on_mouse_up(overlay.hwnd, lparam)


def main() -> int:
    overlay = ov.Overlay()
    overlay.create()
    time.sleep(0.3)

    failures = []

    # 1) 点击气泡 → 展开
    size = overlay.renderer.bubble(ov.STATE_IDLE, 0.0, 0.0).size
    click(overlay, size[0] // 2, size[1] // 2)
    if not overlay.expanded:
        failures.append("点击气泡没有展开面板")
    else:
        print("[OK] 点击气泡 → 面板展开")

    # 2) 面板各控件命中框必须落在画布内
    overlay.render(force=True)
    for name, rect in overlay._hit.items():
        x0, y0, x1, y1 = rect
        w, h = overlay._last_image.size
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            failures.append(f"控件 {name} 的命中框 {rect} 超出画布 {w}x{h}")
    if not failures:
        print("[OK] 控件命中框都在画布内：", list(overlay._hit))

    # 3) 点击麦克风 → 触发 on_mic_down / on_mic_up
    pressed = []
    overlay.on_mic_down = lambda: pressed.append("down")
    overlay.on_mic_up = lambda: pressed.append("up")
    mx0, my0, mx1, my1 = overlay._hit["mic"]
    click(overlay, (mx0 + mx1) // 2, (my0 + my1) // 2)
    if pressed == ["down", "up"]:
        print("[OK] 点击麦克风 → 触发按下/松开")
    else:
        failures.append(f"麦克风点击事件不对：{pressed}")

    # 4) 点击输入框 → 进入输入态；再点关闭 → 退出
    ix0, iy0, ix1, iy1 = overlay._hit["input"]
    click(overlay, (ix0 + ix1) // 2, (iy0 + iy1) // 2)
    if overlay.input_active:
        print("[OK] 点击输入框 → 进入输入态")
    else:
        failures.append("点击输入框没有进入输入态")

    overlay.set_input_text("帮我查下明天的天气")
    overlay.render(force=True)
    # 点到空白处（不是输入框）应当退出输入态
    click(overlay, 30, 240)
    if not overlay.input_active:
        print("[OK] 点空白处 → 退出输入态")
    else:
        failures.append("输入态没有正确退出")

    # 5) 点击关闭 → 退出
    quit_flag = []
    overlay.on_quit = lambda: quit_flag.append(True)
    cx0, cy0, cx1, cy1 = overlay._hit["close"]
    click(overlay, (cx0 + cx1) // 2, (cy0 + cy1) // 2)
    if quit_flag:
        print("[OK] 点击关闭 → 触发退出")
    else:
        failures.append("点击关闭没有触发退出")

    print()
    if failures:
        for f in failures:
            print("[FAIL]", f)
        return 1
    print("交互测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
