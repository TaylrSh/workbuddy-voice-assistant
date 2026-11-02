"""渲染悬浮窗的两种形态并保存成 PNG，用于快速核对视觉。

用法：
    python tools/shot_overlay.py            # 浅色
    python tools/shot_overlay.py --dark     # 深色
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import wb_overlay as ov

OUT = BASE / "recordings"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    renderer = ov.Renderer(ov.Theme(args.dark), args.scale)

    levels = [0.15 + 0.55 * abs(math.sin(i * 0.7)) for i in range(28)]

    suffix = "dark" if args.dark else "light"

    bubble = renderer.bubble(ov.STATE_RECORDING, levels[-1], 1.2)
    bubble_path = OUT / f"_shot_bubble_{suffix}.png"
    bubble.save(bubble_path)
    print("气泡 ->", bubble_path)

    panel = renderer.panel(
        ov.STATE_RECORDING,
        "帮我把项目里的这三个脚本跑一遍，然后总结一下结果。",
        "已跑完：build.py 成功，test.py 有 2 个用例失败，lint.py 通过。"
        "失败原因是依赖版本不匹配，需要升级 requirements.txt 里的 pandas。",
        levels,
        "按住 Ctrl+Alt+V 说话",
        "",
        False,
        1.2,
    )
    panel_path = OUT / f"_shot_panel_{suffix}.png"
    panel.save(panel_path)
    print("面板 ->", panel_path)

    idle = renderer.panel(
        ov.STATE_IDLE, "", "", [], "按住 Ctrl+Alt+V 说话", "", False, 0.4
    )
    idle_path = OUT / f"_shot_panel_idle_{suffix}.png"
    idle.save(idle_path)
    print("空闲面板 ->", idle_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
