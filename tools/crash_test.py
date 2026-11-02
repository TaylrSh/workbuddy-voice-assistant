"""回归测试：确保各种异常文本不会导致悬浮窗渲染崩溃。

起因：WorkBuddy 的回复经常带换行，而 Pillow 的 textlength 不接受
含换行的文本（ValueError: can't measure length of multiline text）。
这个异常沿着 render() → run() → main() 一路抛出，会让整个守护进程
无声消失（stderr 只进控制台管道，窗口一关就没了）。
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PIL import Image, ImageDraw

import wb_overlay as ov


def main() -> int:
    failures: list[str] = []

    canvas = Image.new("RGBA", (404, 572), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = ov.load_font(13)

    # ---- 1) elide 不能被换行文本炸掉 ----
    texts = {
        "多行回复": "第一行\n第二行\n第三行",
        "CRLF": "a\r\nb\r\nc",
        "纯换行": "\n\n\n",
        "换行加超长": ("行" * 100 + "\n") * 20,
        "制表符": "a\tb\tc",
        "emoji 换行": "🎮\n🎮",
        "空串": "",
        "单个字符": "x",
        "超长无换行": "字" * 5000,
    }
    for name, text in texts.items():
        try:
            out = ov.elide(draw, text, font, 370)
            width = draw.textlength(out, font=font)
            if width > 371:
                failures.append(f"elide({name}) 结果仍超宽：{width:.0f}px")
        except Exception as exc:
            failures.append(f"elide({name}) 抛异常：{type(exc).__name__}: {exc}")

    # ---- 2) tail_lines 同样不能炸 ----
    for name, text in texts.items():
        try:
            ov.tail_lines(draw, text, font, 370, 3)
        except Exception as exc:
            failures.append(f"tail_lines({name}) 抛异常：{type(exc).__name__}: {exc}")

    # ---- 3) 整块面板渲染：含换行、None、空内容都不能炸 ----
    renderer = ov.Renderer(ov.Theme(False), 1.0)
    panels = {
        "多行回复": dict(state="idle", transcript="识别文本\n第二行",
                         reply="回复第一行\n回复第二行\n回复第三行",
                         levels=[], hint="提示", input_text="输入\n换行",
                         input_active=True, t=1.0),
        "None 内容": dict(state="idle", transcript=None, reply=None, levels=None,
                          hint=None, input_text=None, input_active=False, t=1.0),
        "空回复": dict(state="idle", transcript="", reply="", levels=[],
                       hint="", input_text="", input_active=False, t=1.0),
    }
    for name, kwargs in panels.items():
        try:
            img = renderer.panel(**kwargs)
            if img.width < 100 or img.height < 100:
                failures.append(f"panel({name}) 尺寸异常：{img.size}")
        except Exception as exc:
            failures.append(f"panel({name}) 抛异常：{type(exc).__name__}: {exc}")

    # ---- 4) render() 本身必须吞掉异常，不让它冒泡 ----
    overlay = ov.Overlay()

    class Boom:
        def panel(self, *a, **k):
            raise RuntimeError("模拟渲染崩溃")

        def bubble(self, *a, **k):
            raise RuntimeError("模拟渲染崩溃")

    overlay.renderer = Boom()
    overlay.hwnd = 12345  # 假装有窗口，让它走到渲染分支
    try:
        overlay.render(force=True)
    except Exception as exc:
        failures.append(f"render() 把异常漏出来了：{type(exc).__name__}: {exc}")

    if failures:
        print("[FAIL] 以下问题未解决：")
        for f in failures:
            print("  -", f)
        return 1

    print("[OK] elide / tail_lines / panel / render 全部不再因异常文本崩溃")
    print("[OK] render() 已能吞掉渲染异常，不会拖垮守护进程")
    return 0


if __name__ == "__main__":
    sys.exit(main())
