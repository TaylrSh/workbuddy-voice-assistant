"""生成语音助手自有图标：青绿渐变语音气泡 + 麦克风 + AI 星点。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "assets" / "app-icon.png"
S = 1024
SS = 4
W = S * SS


def lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def main() -> None:
    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    margin = 54 * SS
    radius = 238 * SS
    box = (margin, margin, W - margin, W - margin)

    # 对角青绿渐变，与官方图标同属清爽绿色系，但形状完全自绘。
    grad = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gp = grad.load()
    for y in range(W):
        for x in range(0, W, 2):
            t = (x / W * 0.38 + y / W * 0.62)
            color = (
                lerp(38, 3, t),
                lerp(214, 133, t),
                lerp(181, 113, t),
                255,
            )
            gp[x, y] = color
            if x + 1 < W:
                gp[x + 1, y] = color

    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    canvas.alpha_composite(Image.composite(grad, Image.new("RGBA", (W, W)), mask))

    # 顶部柔和高光
    glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (170 * SS, 70 * SS, 760 * SS, 580 * SS),
        fill=(255, 255, 255, 46),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(70 * SS))
    canvas.alpha_composite(Image.composite(glow, Image.new("RGBA", (W, W)), mask))

    d = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 245)

    # 语音气泡轮廓
    bubble = (244 * SS, 242 * SS, 780 * SS, 690 * SS)
    d.rounded_rectangle(
        bubble, radius=126 * SS, outline=white, width=25 * SS
    )
    d.polygon(
        [
            (350 * SS, 675 * SS),
            (350 * SS, 790 * SS),
            (485 * SS, 675 * SS),
        ],
        fill=white,
    )

    # 麦克风
    d.rounded_rectangle(
        (462 * SS, 330 * SS, 562 * SS, 528 * SS),
        radius=50 * SS,
        fill=white,
    )
    d.arc(
        (402 * SS, 410 * SS, 622 * SS, 610 * SS),
        start=0,
        end=180,
        fill=white,
        width=24 * SS,
    )
    d.rounded_rectangle(
        (500 * SS, 592 * SS, 524 * SS, 650 * SS),
        radius=12 * SS,
        fill=white,
    )
    d.rounded_rectangle(
        (455 * SS, 642 * SS, 569 * SS, 666 * SS),
        radius=12 * SS,
        fill=white,
    )

    # 右上角 AI 星点，与普通麦克风图标形成识别差异
    cx, cy = 728 * SS, 302 * SS
    d.polygon(
        [
            (cx, cy - 62 * SS),
            (cx + 15 * SS, cy - 15 * SS),
            (cx + 62 * SS, cy),
            (cx + 15 * SS, cy + 15 * SS),
            (cx, cy + 62 * SS),
            (cx - 15 * SS, cy + 15 * SS),
            (cx - 62 * SS, cy),
            (cx - 15 * SS, cy - 15 * SS),
        ],
        fill=white,
    )

    icon = canvas.resize((S, S), Image.Resampling.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    icon.save(OUT)
    print(f"图标已生成：{OUT}")


if __name__ == "__main__":
    main()
