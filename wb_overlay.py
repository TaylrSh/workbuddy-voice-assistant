"""wb_overlay.py —— 悬浮窗式语音助手界面。

设计目标：游戏全屏时也能和 WorkBuddy 说话。

实现要点
--------
1. **分层窗口**（WS_EX_LAYERED + UpdateLayeredWindow）：逐像素透明，
   圆角与阴影都没有锯齿，不会有一圈白边。
2. **不抢焦点**（WS_EX_NOACTIVATE）：点悬浮窗不会把焦点从游戏抢走，
   鼠标照常在游戏里操作。
3. **不进任务栏**（WS_EX_TOOLWINDOW）：不占 Alt-Tab 位置。
4. **始终置顶**（WS_EX_TOPMOST）：压在游戏画面之上。
5. **透明区点击穿透**：透明像素上的鼠标事件直接落到下面的游戏窗口。

两种形态：
  - 收起态：右上角一颗圆形悬浮球，显示状态（空闲/录音中/思考中）。
  - 展开态：一张卡片，显示波形、转写文本、WorkBuddy 回复，并可输入文字。

渲染全部用 Pillow 画成位图，再交给 GDI 合成，因此不依赖 tkinter/PyQt。
"""

from __future__ import annotations

import ctypes
import json
import math
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path
from functools import lru_cache

import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

BUBBLE_SIZE = 72          # 收起态直径（逻辑像素）
PANEL_W = 404             # 展开态宽度
PANEL_H = 572             # 展开态高度
MARGIN = 18               # 距屏幕边缘
WM_OVERLAY_UPDATE = 0x8001
WM_APP_TIMER = 0x8002

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"
STATE_ERROR = "error"

STATE_TEXT = {
    STATE_IDLE: "空闲",
    STATE_RECORDING: "录音中",
    STATE_THINKING: "思考中",
    STATE_SPEAKING: "朗读中",
    STATE_ERROR: "出错",
}


def panel_insets(scale: float) -> tuple[int, int, int]:
    """面板不再预留外扩阴影区，避免透明区域显示成黑边。"""
    return 0, 0, 0


# --------------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------------

class Theme:
    """一套配色。深色更适合夜里打游戏，浅色贴近产品默认观感。"""

    def __init__(self, dark: bool = False) -> None:
        self.dark = dark
        if dark:
            self.card = (26, 29, 37, 244)
            self.card_solid = (26, 29, 37)
            self.border = (58, 64, 80, 255)
            self.text = (233, 237, 245)
            self.text_sub = (146, 155, 174)
            self.text_faint = (108, 116, 134)
            self.accent = (98, 154, 255)
            self.accent_2 = (61, 116, 245)
            self.accent_soft = (40, 52, 82)
            self.field = (36, 40, 51, 255)
            self.field_border = (68, 75, 93, 255)
            self.shadow = (0, 0, 0, 120)
            self.danger = (255, 106, 105)
            self.bubble_top = (104, 158, 255)
            self.bubble_bottom = (56, 112, 240)
        else:
            self.card = (255, 255, 255, 247)
            self.card_solid = (255, 255, 255)
            self.border = (223, 228, 238, 255)
            self.text = (24, 30, 43)
            self.text_sub = (129, 139, 158)
            self.text_faint = (160, 169, 186)
            self.accent = (47, 107, 255)
            self.accent_2 = (30, 84, 226)
            self.accent_soft = (234, 240, 255)
            self.field = (246, 248, 252, 255)
            self.field_border = (228, 233, 243, 255)
            self.shadow = (16, 24, 40, 56)
            self.danger = (234, 74, 73)
            self.bubble_top = (110, 162, 255)
            self.bubble_bottom = (47, 107, 255)


# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------

_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("C:/Windows/Fonts/msyhl.ttc", 0),
    ("C:/Windows/Fonts/simhei.ttf", None),
    ("C:/Windows/Fonts/simsun.ttc", 0),
]

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


@lru_cache(maxsize=16)
def load_app_icon(size: int) -> Image.Image:
    """加载本项目自绘图标，裁掉透明边距后缩放到目标尺寸。"""
    path = Path(__file__).resolve().parent / "assets" / "app-icon.png"
    img = Image.open(path).convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """取一个能显示中文的字体，取不到就退回 Pillow 默认字体。"""
    key = (size, bold)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached

    candidates = list(_FONT_CANDIDATES)
    if bold:
        candidates.insert(0, ("C:/Windows/Fonts/msyhbd.ttc", 0))
        candidates.append(("C:/Windows/Fonts/simhei.ttf", None))

    for path, index in candidates:
        if not Path(path).exists():
            continue
        try:
            if index is None:
                font = ImageFont.truetype(path, size)
            else:
                font = ImageFont.truetype(path, size, index=index)
            _font_cache[key] = font
            return font
        except OSError:
            continue

    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# --------------------------------------------------------------------------
# 文本排版
# --------------------------------------------------------------------------

def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """按像素宽度折行。

    中文逐字断行；英文单词尽量不拆开（在空格处断）。
    """
    if not text:
        return []

    lines: list[str] = []
    for raw_line in text.split("\n"):
        if not raw_line:
            lines.append("")
            continue

        current = ""
        # 把文本切成「中文单字」与「英文单词」两类单元
        tokens: list[str] = []
        buffer = ""
        for ch in raw_line:
            if ord(ch) < 128 and not ch.isspace():
                buffer += ch
                continue
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(ch)
        if buffer:
            tokens.append(buffer)

        for token in tokens:
            probe = current + token
            if draw.textlength(probe, font=font) <= max_width or not current:
                current = probe
                continue
            lines.append(current.rstrip())
            current = "" if token.isspace() else token
        lines.append(current.rstrip())

    return lines


def tail_lines(draw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    """折行后只保留末尾 max_lines 行（长回复从头截断）。"""
    lines = wrap_text(draw, text, font, max_width)
    if len(lines) <= max_lines:
        return lines
    return lines[-max_lines:]


def elide(draw, text: str, font, max_width: int) -> str:
    """单行超长时截断并加省略号。

    注意：Pillow 的 textlength 不能接受含换行的文本（会抛
    "can't measure length of multiline text"）。WorkBuddy 的回复经常带换行，
    所以这里先把换行换成空格再量宽度。
    """
    if not text:
        return ""

    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if draw.textlength(flat, font=font) <= max_width:
        return flat

    ellipsis = "…"
    # 按字符删不一定等比缩宽度，用二分找截断点更稳
    lo, hi = 0, len(flat)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = flat[:mid]
        if draw.textlength(candidate + ellipsis, font=font) <= max_width:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return (best + ellipsis) if best else ellipsis


# --------------------------------------------------------------------------
# GDI 分层窗口合成
# --------------------------------------------------------------------------

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND,
    wintypes.HDC,
    ctypes.POINTER(_POINT),
    ctypes.POINTER(_SIZE),
    wintypes.HDC,
    ctypes.POINTER(_POINT),
    wintypes.DWORD,
    ctypes.POINTER(_BLENDFUNCTION),
    wintypes.DWORD,
]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL
_user32.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
_user32.SetTimer.restype = wintypes.UINT
_user32.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.KillTimer.restype = wintypes.BOOL


def _premultiplied_bgra(img: Image.Image) -> bytes:
    """RGBA → 预乘 BGRA（UpdateLayeredWindow 要求预乘 alpha）。"""
    img = img.convert("RGBA")
    r, g, b, a = img.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


def _work_area() -> tuple[int, int, int, int]:
    """返回当前主显示器的工作区 (left, top, right, bottom)。

    单位必须与 CreateWindowEx / UpdateLayeredWindow 用的是同一套——
    进程设了 PER_MONITOR_AWARE 后，窗口坐标是**物理像素**。
    （本机逻辑分辨率 2048x1152，物理 2560x1440，缩放 125%。）

    注意 GetMonitorInfo 在 PER_MONITOR_AWARE 下返回的仍是逻辑像素，
    会和窗口坐标系统打架；所以这里用 GetSystemMetrics(SM_CXSCREEN)
    取物理尺寸，再按缩放比例校正到工作区。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromPoint(
            ctypes.wintypes.POINT(0, 0), 2  # MONITOR_DEFAULTTOPRIMARY
        )
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            work = mi.rcWork
            # PER_MONITOR_AWARE 下窗口坐标是物理像素，
            # 而 GetMonitorInfo 给的是逻辑像素——按 SM_CXSCREEN 比例换算，
            # 保证两者同一坐标系，位置既准确又不会跑出屏幕。
            phys_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            log_w = work.right - work.left
            if log_w > 0 and phys_w != log_w:
                k = phys_w / log_w
                return (int(work.left * k), int(work.top * k),
                        int(work.right * k), int(work.bottom * k))
            return work.left, work.top, work.right, work.bottom
    except Exception:
        pass
    return (0, 0, win32api.GetSystemMetrics(win32con.SM_CXSCREEN) or 1920,
            win32api.GetSystemMetrics(win32con.SM_CYSCREEN) or 1080)


def paint_layered(hwnd: int, img: Image.Image, x: int, y: int) -> None:
    """把一张 RGBA 图作为分层窗口内容贴到 (x, y)。

    整段包 try：任何 GDI 层的异常都不能让守护进程崩掉。
    """
    try:
        _paint_layered_inner(hwnd, img, x, y)
    except Exception as exc:
        try:
            sys.stderr.write(f"[overlay] 绘制失败: {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()
        except Exception:
            pass


def _paint_layered_inner(hwnd: int, img: Image.Image, x: int, y: int) -> None:
    width, height = img.size
    hdc_screen = win32gui.GetDC(0)
    hdc_mem = win32gui.CreateCompatibleDC(hdc_screen)

    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height          # 负数 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0           # BI_RGB

    raw_bits = ctypes.c_void_p()
    hbitmap = _gdi32.CreateDIBSection(
        hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(raw_bits), None, 0
    )
    if not hbitmap:
        win32gui.DeleteDC(hdc_mem)
        win32gui.ReleaseDC(0, hdc_screen)
        raise ctypes.WinError(ctypes.get_last_error())

    payload = _premultiplied_bgra(img)
    ctypes.memmove(raw_bits, payload, len(payload))

    old = _gdi32.SelectObject(hdc_mem, hbitmap)
    pt_dst = _POINT(x, y)
    size = _SIZE(width, height)
    pt_src = _POINT(0, 0)
    # AC_SRC_OVER / AC_SRC_ALPHA 模式下 AlphaConstant 必须是 255，
    # 写成 1 会让整张图几乎完全透明——窗口在但看不见。
    blend = _BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER + 常量 Alpha 255 + AC_SRC_ALPHA

    _user32.UpdateLayeredWindow(
        hwnd, hdc_screen, ctypes.byref(pt_dst), ctypes.byref(size),
        hdc_mem, ctypes.byref(pt_src), 0, ctypes.byref(blend), 2,  # ULW_ALPHA
    )

    _gdi32.SelectObject(hdc_mem, old)
    _gdi32.DeleteObject(hbitmap)
    win32gui.DeleteDC(hdc_mem)
    win32gui.ReleaseDC(0, hdc_screen)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def _rounded_shadow(canvas: Image.Image, box, radius: int, blur: int, offset: int,
                    alpha: int) -> None:
    """在画布上叠一层柔和阴影。"""
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sx0, sy0, sx1, sy1 = box
    sd.rounded_rectangle(
        (sx0 + offset, sy0 + offset, sx1 + offset, sy1 + offset),
        radius=radius, fill=(0, 0, 0, alpha),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow)


def _vertical_gradient(size, top, bottom) -> Image.Image:
    """竖直线性渐变。"""
    width, height = size
    grad = Image.new("RGB", (1, height))
    pixels = grad.load()
    for y in range(height):
        t = y / max(1, height - 1)
        pixels[0, y] = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
    return grad.resize((width, height)).convert("RGBA")


def draw_mic(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float, color) -> None:
    """画一个麦克风图标。"""
    w = max(2, int(9 * scale))
    h = max(3, int(15 * scale))
    # 拾音头
    draw.rounded_rectangle(
        (cx - w // 2, cy - h, cx + w // 2, cy - h + int(2.1 * h)),
        radius=w // 2, fill=color,
    )
    # 支架
    arc_w = max(2, int(3 * scale))
    span = int(13 * scale)
    draw.arc(
        (cx - span, cy - h + int(0.6 * h), cx + span, cy + int(0.9 * h)),
        start=0, end=180, fill=color, width=arc_w,
    )
    # 立柱与底座
    draw.line((cx, cy + int(0.9 * h), cx, cy + int(1.5 * h)), fill=color, width=arc_w)
    draw.line(
        (cx - int(5 * scale), cy + int(1.5 * h), cx + int(5 * scale), cy + int(1.5 * h)),
        fill=color, width=arc_w,
    )


class Renderer:
    """把状态画成一张 RGBA 位图。"""

    def __init__(self, theme: Theme, scale: float = 1.0) -> None:
        self.theme = theme
        self.scale = scale
        self._bg_cache: tuple | None = None

    # ---------- 通用小部件 ----------

    def _rounded(self, draw, box, radius, fill, outline=None, width=1):
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

    def _status_pill(self, draw, x, y, text, color) -> int:
        """画一个状态胶囊，返回它占用的宽度。"""
        font = load_font(int(12 * self.scale), bold=True)
        tw = int(draw.textlength(text, font=font))
        w = tw + int(20 * self.scale)
        h = int(24 * self.scale)
        self._rounded(draw, (x, y, x + w, y + h), h // 2, color)
        draw.text((x + w // 2, y + h // 2), text, font=font, fill=(255, 255, 255),
                  anchor="mm")
        return w

    def _status_pill_width(self, draw, text) -> int:
        font = load_font(int(12 * self.scale), bold=True)
        return int(draw.textlength(text, font=font)) + int(20 * self.scale)

    def _mic_button(self, draw, cx, cy, r, active: bool, level: float, t: float) -> None:
        """中间那颗大麦克风按钮。"""
        theme = self.theme
        if active:
            # 录音时：红色 + 随音量呼吸的光晕
            glow = 0.35 + min(1.0, level) * 0.65
            halo = Image.new("RGBA", draw._image.size, (0, 0, 0, 0))
            hd = ImageDraw.Draw(halo)
            hr = int(r + 14 * self.scale + 6 * self.scale * math.sin(t * 6))
            hd.ellipse(
                (cx - hr, cy - hr, cx + hr, cy + hr),
                fill=(theme.danger[0], theme.danger[1], theme.danger[2],
                      int(70 * glow)),
            )
            halo = halo.filter(ImageFilter.GaussianBlur(int(12 * self.scale)))
            draw._image.alpha_composite(halo)

            body = _vertical_gradient(
                (2 * r, 2 * r), (255, 128, 126), theme.danger
            )
            mask = Image.new("L", (2 * r, 2 * r), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 2 * r - 1, 2 * r - 1), fill=255)
            draw._image.alpha_composite(
                Image.composite(body, Image.new("RGBA", body.size, (0, 0, 0, 0)), mask),
                (cx - r, cy - r),
            )
        else:
            body = _vertical_gradient(
                (2 * r, 2 * r), theme.bubble_top, theme.bubble_bottom
            )
            mask = Image.new("L", (2 * r, 2 * r), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 2 * r - 1, 2 * r - 1), fill=255)
            draw._image.alpha_composite(
                Image.composite(body, Image.new("RGBA", body.size, (0, 0, 0, 0)), mask),
                (cx - r, cy - r),
            )

        draw_mic(draw, cx, cy, self.scale * (r / 34.0), (255, 255, 255))

    def _waveform(self, draw, x, y, w, h, levels, color) -> None:
        """一行随音量跳动的波形条。"""
        if not levels:
            return
        n = len(levels)
        slot = w / n
        bar_w = max(2, int(slot * 0.52))
        for i, level in enumerate(levels):
            amp = max(0.06, min(1.0, level))
            bh = max(2, int(h * amp))
            bx = int(x + i * slot + (slot - bar_w) / 2)
            draw.rounded_rectangle(
                (bx, y + (h - bh) / 2, bx + bar_w, y + (h + bh) / 2),
                radius=bar_w // 2, fill=color,
            )

    # ---------- 收起态：悬浮球 ----------

    def bubble(self, state: str, level: float, t: float) -> Image.Image:
        s = self.scale
        size = int(BUBBLE_SIZE * s)
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        theme = self.theme

        r = int(size * 0.36)
        cx = cy = size // 2

        if state == STATE_RECORDING:
            halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            hd = ImageDraw.Draw(halo)
            hr = int(r + 10 * s + 5 * s * math.sin(t * 6))
            hd.ellipse((cx - hr, cy - hr, cx + hr, cy + hr),
                       fill=(theme.danger[0], theme.danger[1], theme.danger[2], 90))
            halo = halo.filter(ImageFilter.GaussianBlur(int(10 * s)))
            img.alpha_composite(halo)
            top, bottom = (255, 132, 130), theme.danger
        elif state == STATE_THINKING:
            top, bottom = (150, 190, 255), theme.accent
        else:
            top, bottom = theme.bubble_top, theme.bubble_bottom

        # 使用官方 WorkBuddy 品牌图标，状态光圈和动画保持不变。
        img.alpha_composite(load_app_icon(2 * r), (cx - r, cy - r))

        if state == STATE_THINKING:
            # 思考中：转圈的小圆点
            dot_r = int(4 * s)
            for i in range(3):
                angle = t * 4 + i * (2 * math.pi / 3)
                dx = cx + int(math.cos(angle) * 13 * s)
                dy = cy + int(math.sin(angle) * 13 * s)
                draw.ellipse((dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r),
                             fill=(255, 255, 255, 235))

        if state == STATE_IDLE:
            # 右上角小绿点，表示助手在线
            dot = int(5 * s)
            draw.ellipse(
                (size - dot * 3, dot, size - dot, dot * 3),
                fill=(64, 202, 126, 255),
            )
        return img

    # ---------- 展开态：面板 ----------

    def panel(self, state: str, transcript: str, reply: str, levels, hint: str,
              input_text: str, input_active: bool, t: float,
              hotkey_text: str = "Ctrl+Alt+V", hotkey_editing: bool = False) -> Image.Image:
        # 兜底：任何 None 都不该让整块面板渲染失败
        transcript = transcript or ""
        reply = reply or ""
        hint = hint or ""
        input_text = input_text or ""
        levels = levels or []
        s = self.scale
        theme = self.theme

        # 画布比卡片大一圈，专门留给阴影，否则阴影会被裁掉
        w = int(PANEL_W * s)
        h = int(PANEL_H * s)
        ix, it, ib = panel_insets(s)
        cw = w - 2 * ix
        ch = h - it - ib

        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)

        radius = int(20 * s)
        self._rounded(draw, (0, 0, cw - 1, ch - 1), radius, theme.card, theme.border,
                      max(1, int(s)))

        pad = int(18 * s)
        y = pad

        # ---- 标题栏 ----
        icon = int(32 * s)
        card.alpha_composite(load_app_icon(icon), (pad, y))

        title_font = load_font(int(15 * s), bold=True)
        draw.text((pad + icon + int(10 * s), y), "WorkBuddy 语音助手",
                  font=title_font, fill=theme.text)
        sub_font = load_font(int(11 * s))
        draw.text((pad + icon + int(10 * s), y + int(19 * s)),
                  hint or "按住 Ctrl+Alt+V 说话", font=sub_font, fill=theme.text_faint)

        # 状态胶囊 + 关闭按钮
        state_color = {
            STATE_RECORDING: theme.danger,
            STATE_THINKING: theme.accent,
            STATE_SPEAKING: theme.accent,
            STATE_ERROR: theme.danger,
        }.get(state, (64, 178, 116))
        state_label = STATE_TEXT.get(state, "")
        pill_w = self._status_pill_width(draw, state_label)
        pill_x = cw - pad - int(26 * s) - pill_w
        self._status_pill(draw, pill_x, y + int(4 * s), state_label, state_color)

        # 关闭按钮
        cb = int(24 * s)
        cb_box = (cw - pad - cb, y + int(4 * s), cw - pad, y + int(4 * s) + cb)
        cx0, cy0, cx1, cy1 = cb_box
        draw.line((cx0 + cb * 0.32, cy0 + cb * 0.32, cx1 - cb * 0.32, cy1 - cb * 0.32),
                  fill=theme.text_faint, width=max(1, int(1.6 * s)))
        draw.line((cx0 + cb * 0.32, cy1 - cb * 0.32, cx1 - cb * 0.32, cy0 + cb * 0.32),
                  fill=theme.text_faint, width=max(1, int(1.6 * s)))

        y += icon + int(14 * s)

        # 分割线
        draw.line((pad, y, cw - pad, y), fill=theme.border, width=1)
        y += int(16 * s)

        # ---- 麦克风按钮 + 波形 ----
        mic_r = int(40 * s)
        mic_cx = cw // 2
        mic_cy = y + mic_r
        self._mic_button(draw, mic_cx, mic_cy, mic_r,
                         state == STATE_RECORDING,
                         levels[-1] if levels else 0.0, t)
        y += 2 * mic_r + int(10 * s)

        wave_h = int(22 * s)
        wave_w = int(180 * s)
        self._waveform(draw, mic_cx - wave_w // 2, y, wave_w, wave_h,
                       list(levels), theme.accent if state != STATE_RECORDING else theme.danger)
        y += wave_h + int(14 * s)

        # ---- 识别文本 ----
        label_font = load_font(int(12 * s), bold=True)
        body_font = load_font(int(13 * s))
        draw.text((pad, y), "识别", font=label_font, fill=theme.text_sub)
        y += int(19 * s)

        box_h = int(64 * s)
        self._rounded(draw, (pad, y, cw - pad, y + box_h), int(10 * s), theme.field,
                      theme.field_border, max(1, int(s)))
        lines = tail_lines(draw, transcript or "（等待语音输入…）", body_font,
                           cw - pad * 2 - int(16 * s), 3)
        ty = y + int(9 * s)
        for line in lines:
            color = theme.text if transcript else theme.text_faint
            draw.text((pad + int(9 * s), ty), line, font=body_font, fill=color)
            ty += int(19 * s)
        y += box_h + int(14 * s)

        # ---- WorkBuddy 回复 ----
        draw.text((pad, y), "WorkBuddy", font=label_font, fill=theme.text_sub)
        y += int(19 * s)

        reply_h = ch - y - pad - int(90 * s)
        self._rounded(draw, (pad, y, cw - pad, y + reply_h), int(10 * s), theme.field,
                      theme.field_border, max(1, int(s)))
        if reply:
            lines = tail_lines(draw, reply, body_font, cw - pad * 2 - int(16 * s),
                               max(1, (reply_h - int(14 * s)) // int(19 * s)))
            ty = y + int(9 * s)
            for line in lines:
                draw.text((pad + int(9 * s), ty), line, font=body_font, fill=theme.text)
                ty += int(19 * s)
        else:
            draw.text((pad + int(9 * s), y + int(9 * s)), "（还没有回复）",
                      font=body_font, fill=theme.text_faint)
        y += reply_h + int(8 * s)

        # ---- 快捷键设置 ----
        hotkey_h = int(32 * s)
        hotkey_box = (pad, y, cw - pad, y + hotkey_h)
        self._rounded(draw, hotkey_box, int(9 * s), theme.field,
                      theme.accent if hotkey_editing else theme.field_border, max(1, int(s)))
        draw.text((pad + int(10 * s), y + hotkey_h // 2), "快捷键",
                  font=label_font, fill=theme.text_sub, anchor="lm")
        hotkey_value = "输入组合键，回车保存" if hotkey_editing else f"{hotkey_text}  ›"
        draw.text((cw - pad - int(10 * s), y + hotkey_h // 2), hotkey_value,
                  font=body_font, fill=theme.accent if hotkey_editing else theme.text_faint,
                  anchor="rm")
        y += hotkey_h + int(12 * s)

        # ---- 输入框 ----
        field_h = int(38 * s)
        field_box = (pad, y, cw - pad - int(78 * s), y + field_h)
        self._rounded(draw, field_box, int(10 * s), theme.field, theme.field_border,
                      max(1, int(s)))
        caret_on = input_active and int(t * 2) % 2 == 0
        shown = elide(draw, input_text, body_font,
                      field_box[2] - field_box[0] - int(18 * s))
        draw.text((field_box[0] + int(9 * s), y + field_h // 2), shown, font=body_font,
                  fill=theme.text if input_text else theme.text_faint, anchor="lm")
        if caret_on:
            cw_ = draw.textlength(shown, font=body_font)
            cxp = field_box[0] + int(9 * s) + cw_ + 1
            draw.line((cxp, y + int(10 * s), cxp, y + field_h - int(10 * s)),
                      fill=theme.accent, width=max(1, int(1.5 * s)))

        send_box = (cw - pad - int(70 * s), y, cw - pad, y + field_h)
        enabled = bool(input_text.strip())
        send_bg = theme.accent if enabled else theme.field
        self._rounded(draw, send_box, int(10 * s), send_bg)
        draw.text(((send_box[0] + send_box[2]) // 2, y + field_h // 2), "发送",
                  font=load_font(int(13 * s), bold=True),
                  fill=(255, 255, 255) if enabled else theme.text_faint, anchor="mm")

        # ---- 合成：不绘制外扩阴影，直接贴卡片，避免黑边 ----
        canvas.alpha_composite(card, (ix, it))
        return canvas


# --------------------------------------------------------------------------
# 悬浮窗
# --------------------------------------------------------------------------

class Overlay:
    """悬浮窗。

    线程模型：窗口必须在创建它的线程里跑消息循环，所以
    主线程跑消息循环，业务逻辑（热键、录音、请求 WorkBuddy）放 worker 线程。
    """

    def __init__(
        self,
        on_mic_down=None,
        on_mic_up=None,
        on_submit=None,
        on_toggle=None,
        on_quit=None,
        on_hotkey_change=None,
        dark: bool = False,
        scale: float = 1.0,
        position_file: str | Path | None = None,
        hotkey_text: str = "Ctrl+Alt+V",
    ) -> None:
        self.on_mic_down = on_mic_down
        self.on_mic_up = on_mic_up
        self.on_submit = on_submit
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.on_hotkey_change = on_hotkey_change
        self.hotkey_text = hotkey_text
        self.hotkey_editing = False

        self.theme = Theme(dark)
        self.scale = scale
        self.renderer = Renderer(self.theme, scale)

        self.state = STATE_IDLE
        self.transcript = ""
        self.reply = ""
        self.hint = f"按住 {hotkey_text} 说话"
        self.input_text = ""
        self.input_active = False
        self.expanded = False
        self.levels: deque[float] = deque(maxlen=28)
        self._render_errors = 0
        self._last_beat = 0.0

        self.hwnd = 0
        self._bubble_pos = (0, 0)
        self._drag_from: tuple[int, int] | None = None
        self._drag_origin: tuple[int, int] | None = None
        self._drag_down_at = 0.0
        self._dragging = False
        self._pressed = ""            # 当前按下的控件
        self._dirty = True
        self._closing = False
        self._prev_foreground = 0
        self._last_render = 0.0
        self._lock = threading.RLock()

        self.position_file = Path(position_file) if position_file else None
        self._hit: dict[str, tuple] = {}   # 控件名 → 区域

    # ---------------- 状态更新（任意线程可调用）----------------

    def _touch(self) -> None:
        self._dirty = True
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, WM_OVERLAY_UPDATE, 0, 0)
            except Exception:
                pass

    def set_state(self, state: str, hint: str | None = None) -> None:
        with self._lock:
            self.state = state
            if hint is not None:
                self.hint = hint
        self._touch()

    def set_transcript(self, text: str) -> None:
        with self._lock:
            self.transcript = text
        self._touch()

    def set_reply(self, text: str) -> None:
        with self._lock:
            self.reply = text
        self._touch()

    def set_hint(self, text: str) -> None:
        with self._lock:
            self.hint = text
        self._touch()

    def push_level(self, level: float) -> None:
        with self._lock:
            self.levels.append(max(0.0, min(1.0, level)))
        self._touch()

    def clear_levels(self) -> None:
        with self._lock:
            self.levels.clear()
        self._touch()

    def expand(self, expanded: bool = True) -> None:
        with self._lock:
            if self.expanded == expanded:
                return
            self.expanded = expanded
        self._touch()

    def toggle(self) -> None:
        self.expand(not self.expanded)

    def set_input_text(self, text: str) -> None:
        with self._lock:
            self.input_text = text
        self._touch()

    # ---------------- 窗口创建 ----------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_OVERLAY_UPDATE:
            self._dirty = True
            return 0
        if msg == win32con.WM_TIMER:
            self._dirty = True
            return 0
        if msg == win32con.WM_SETCURSOR:
            cursor_id = win32con.IDC_SIZEALL if self._dragging else win32con.IDC_ARROW
            win32gui.SetCursor(win32gui.LoadCursor(0, cursor_id))
            return 1
        if msg == win32con.WM_NCHITTEST:
            cursor_id = win32con.IDC_SIZEALL if self._dragging else win32con.IDC_ARROW
            win32gui.SetCursor(win32gui.LoadCursor(0, cursor_id))
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            sx, sy = win32gui.ScreenToClient(hwnd, (x, y))
            if self._alpha_at(sx, sy) < 24:
                return win32con.HTTRANSPARENT
            return win32con.HTCLIENT
        if msg == win32con.WM_LBUTTONDOWN:
            return self._on_mouse_down(hwnd, lparam)
        if msg == win32con.WM_LBUTTONUP:
            return self._on_mouse_up(hwnd, lparam)
        if msg == win32con.WM_MOUSEMOVE:
            return self._on_mouse_move(hwnd, lparam)
        if msg == win32con.WM_CAPTURECHANGED:
            self._dragging = False
            return 0
        if msg == win32con.WM_CHAR:
            return self._on_char(hwnd, wparam)
        if msg == win32con.WM_KEYDOWN:
            return self._on_keydown(hwnd, wparam)
        if msg == win32con.WM_CLOSE:
            self._quit()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _alpha_at(self, x: int, y: int) -> int:
        """取当前画面某点的 alpha，用来让透明区域点击穿透。"""
        img = getattr(self, "_last_image", None)
        if img is None:
            return 255
        if x < 0 or y < 0 or x >= img.width or y >= img.height:
            return 0
        return img.getpixel((x, y))[3]

    def create(self) -> int:
        """创建窗口（必须在主线程调用）。"""
        # DPI 策略：**保持系统 DPI 感知**（DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -1），
        # 窗口坐标就一直是逻辑像素，不会算到屏幕外。
        # 若设成 PER_MONITOR_AWARE(-4)，本机 125% 缩放下 GetSystemMetrics /
        # GetMonitorInfo 会返回物理像素（2048 -> 2560），
        # 算出来的悬浮球位置会超出屏幕几百像素——窗口存在但用户看不见。
        # 按显示器 DPI 感知（高分屏不糊）。
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

        hinst = win32api.GetModuleHandle(None)
        wndclass = win32gui.WNDCLASS()
        wndclass.hInstance = hinst
        wndclass.lpfnWndProc = self._wndproc
        wndclass.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wndclass.lpszClassName = "WorkBuddyVoiceOverlay"
        wndclass.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        try:
            win32gui.RegisterClass(wndclass)
        except Exception:
            pass  # 重复注册

        wl, wt, wr, wb = _work_area()
        screen_w = wr - wl
        screen_h = wb - wt

        size = int(BUBBLE_SIZE * self.scale)
        pos = self._load_position()
        if pos is None:
            pos = (wl + screen_w - size - MARGIN, wt + MARGIN + 60)
        else:
            # 位置文件里的坐标可能是旧分辨率留下的，夹回屏幕内
            pos = (min(max(pos[0], wl + MARGIN), wl + screen_w - size - MARGIN),
                   min(max(pos[1], wt + MARGIN), wt + screen_h - size - MARGIN))
        self._bubble_pos = pos

        exstyle = (
            win32con.WS_EX_LAYERED
            | win32con.WS_EX_TOOLWINDOW
            | win32con.WS_EX_NOACTIVATE
            | win32con.WS_EX_TOPMOST
        )
        self.hwnd = win32gui.CreateWindowEx(
            exstyle,
            "WorkBuddyVoiceOverlay",
            "WorkBuddy 语音助手",
            win32con.WS_POPUP,
            pos[0], pos[1], size, size,
            None, None, hinst, None,
        )
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)
        self.render(force=True)
        return self.hwnd

    # ---------------- 渲染 ----------------

    def render(self, force: bool = False) -> None:
        try:
            self._render(force)
        except Exception as exc:
            # 渲染绝不能拖垮整个进程——记一次日志，下一帧继续
            self._render_errors += 1
            if self._render_errors <= 5 or self._render_errors % 50 == 0:
                sys.stderr.write(
                    f"[overlay] 渲染失败({self._render_errors} 次): "
                    f"{type(exc).__name__}: {exc}\n"
                )
                sys.stderr.flush()

    def _render(self, force: bool = False) -> None:
        if not self.hwnd:
            return
        # 窗口可能在子线程操作期间被销毁——先确认句柄仍有效
        try:
            if not win32gui.IsWindow(self.hwnd):
                return
        except Exception:
            return
        now = time.monotonic()
        if not force and not self._dirty:
            return
        if not force and now - self._last_render < 0.045:
            return
        self._dirty = False
        self._last_render = now

        with self._lock:
            state = self.state
            transcript = self.transcript
            reply = self.reply
            hint = self.hint
            input_text = self.input_text
            input_active = self.input_active
            hotkey_text = self.hotkey_text
            hotkey_editing = self.hotkey_editing
            levels = list(self.levels)
            expanded = self.expanded
            bubble_pos = self._bubble_pos

        t = now
        if expanded:
            img = self.renderer.panel(state, transcript, reply, levels, hint,
                                      input_text, input_active, t, hotkey_text, hotkey_editing)
            x, y = self._panel_origin(bubble_pos, img.size)
        else:
            img = self.renderer.bubble(state, levels[-1] if levels else 0.0, t)
            x, y = bubble_pos

        self._last_image = img
        self._layout_hits(img.size, expanded)
        try:
            paint_layered(self.hwnd, img, x, y)
        except Exception:
            pass

    def _panel_origin(self, bubble_pos, size) -> tuple[int, int]:
        """面板挂在悬浮球左下方，并保证不出屏。"""
        bw = int(BUBBLE_SIZE * self.scale)
        wl, wt, wr, wb = _work_area()
        screen_w = wr - wl
        screen_h = wb - wt
        x = bubble_pos[0] + bw - size[0]
        y = bubble_pos[1] - int(12 * self.scale)
        if x < wl + MARGIN:
            x = wl + MARGIN
        if x + size[0] > wr - MARGIN:
            x = max(wl + MARGIN, wr - MARGIN - size[0])
        if y < wt + MARGIN:
            y = wt + MARGIN
        if y + size[1] > wb - MARGIN:
            y = max(wt + MARGIN, wb - MARGIN - size[1])
        return int(x), int(y)

    def _layout_hits(self, size, expanded: bool) -> None:
        """记录可点击控件的位置，供命中测试。"""
        self._hit.clear()
        if not expanded:
            self._hit["bubble"] = (0, 0, size[0], size[1])
            return
        s = self.scale
        w, h = size
        pad = int(18 * s)
        ix, it, ib = panel_insets(s)
        ox, oy = ix, it          # 卡片在画布里的原点
        cw = w - 2 * ix          # 命中区域必须基于卡片尺寸，而非画布尺寸
        ch = h - it - ib

        self._hit["close"] = (ox + cw - pad - int(24 * s), oy + int(18 * s) + int(4 * s),
                              ox + cw - pad, oy + int(18 * s) + int(4 * s) + int(24 * s))

        mic_r = int(40 * s)
        mic_top = oy + int(18 * s) + int(32 * s) + int(30 * s)
        self._hit["mic"] = (ox + cw // 2 - mic_r, mic_top,
                            ox + cw // 2 + mic_r, mic_top + 2 * mic_r)

        field_h = int(38 * s)
        field_y = oy + ch - pad - field_h
        hotkey_h = int(32 * s)
        hotkey_y = field_y - int(12 * s) - hotkey_h
        self._hit["hotkey"] = (ox + pad, hotkey_y, ox + cw - pad, hotkey_y + hotkey_h)
        self._hit["input"] = (ox + pad, field_y,
                              ox + cw - pad - int(78 * s), field_y + field_h)
        self._hit["send"] = (ox + cw - pad - int(70 * s), field_y,
                             ox + cw - pad, field_y + field_h)

    # ---------------- 交互 ----------------

    def _clamp_bubble_pos(self, pos) -> tuple[int, int]:
        """把悬浮球限制在主屏工作区内，避免拖动后移出可见区域。"""
        size = int(BUBBLE_SIZE * self.scale)
        wl, wt, wr, wb = _work_area()
        min_x, min_y = wl + MARGIN, wt + MARGIN
        max_x = max(min_x, wr - MARGIN - size)
        max_y = max(min_y, wb - MARGIN - size)
        return (int(min(max(pos[0], min_x), max_x)),
                int(min(max(pos[1], min_y), max_y)))

    def _on_mouse_down(self, hwnd, lparam):
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        # 拖动必须记录屏幕坐标；窗口移动后，客户端坐标会随窗口原点改变。
        self._drag_from = win32gui.ClientToScreen(hwnd, (x, y))
        self._drag_origin = self._bubble_pos
        self._dragging = False
        self._pressed = self._hit_test(x, y)
        self._write_log(f"鼠标按下 x={x} y={y} target={self._pressed!r} origin={self._bubble_pos}")
        if self._pressed == "mic":
            self._set_mic_pressed(True)
        try:
            win32gui.SetCapture(hwnd)
        except Exception:
            pass
        return 0

    def _on_mouse_move(self, hwnd, lparam):
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        screen_pos = win32gui.ClientToScreen(hwnd, (x, y))
        if self._drag_from and not self._dragging:
            if (abs(screen_pos[0] - self._drag_from[0]) > 4
                    or abs(screen_pos[1] - self._drag_from[1]) > 4):
                self._dragging = True
                self._write_log(f"开始拖动 from={self._drag_from} origin={self._drag_origin}")
        if self._dragging and self._drag_origin:
            raw_pos = (
                self._drag_origin[0] + (screen_pos[0] - self._drag_from[0]),
                self._drag_origin[1] + (screen_pos[1] - self._drag_from[1]),
            )
            self._bubble_pos = self._clamp_bubble_pos(raw_pos)
            self._write_log(
                f"拖动计算 x={x} y={y} raw={raw_pos} clamped={self._bubble_pos} area={_work_area()}"
            )
            self._dirty = True
        return 0

    def _on_mouse_up(self, hwnd, lparam):
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        # 先保存拖动状态，再释放鼠标捕获；ReleaseCapture 会同步触发
        # WM_CAPTURECHANGED，提前读取可避免松手后被误判成单击。
        pressed = self._pressed
        self._pressed = ""
        was_drag = self._dragging
        self._write_log(f"鼠标松开 was_drag={was_drag} pos={self._bubble_pos}")
        self._dragging = False
        self._drag_from = None
        try:
            win32gui.ReleaseCapture()
        except Exception:
            pass

        if pressed == "mic":
            self._set_mic_pressed(False)
        if was_drag:
            self._save_position()
            return 0

        target = self._hit_test(x, y)
        if self.hotkey_editing and target not in ("hotkey", "input"):
            self.hotkey_editing = False
            self.input_text = ""

        # 点到输入框以外的地方就退出输入态，并把焦点还给游戏
        if target != "input" and self.input_active:
            self._set_input_active(False)

        if target == "bubble":
            self.toggle()
            if self.on_toggle:
                try:
                    self.on_toggle(self.expanded)
                except Exception:
                    pass
        elif target == "close":
            # × 只收起当前面板，助手继续以悬浮球形式运行。
            self.expand(False)
        elif target == "hotkey":
            self.hotkey_editing = True
            self.input_text = self.hotkey_text
            self._set_input_active(True)
            self._dirty = True
        elif target == "input":
            self._set_input_active(True)
        elif target == "send":
            self._submit()
        return 0

    def _hit_test(self, x: int, y: int) -> str:
        for name, (x0, y0, x1, y1) in self._hit.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return ""

    def _set_mic_pressed(self, pressed: bool) -> None:
        if pressed:
            if self.on_mic_down:
                try:
                    self.on_mic_down()
                except Exception:
                    pass
        else:
            if self.on_mic_up:
                try:
                    self.on_mic_up()
                except Exception:
                    pass

    def _set_input_active(self, active: bool) -> None:
        if active == self.input_active:
            return
        self.input_active = active
        ex = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        if active:
            self._prev_foreground = win32gui.GetForegroundWindow()
            ex &= ~win32con.WS_EX_NOACTIVATE
            win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, ex)
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:
                pass
        else:
            ex |= win32con.WS_EX_NOACTIVATE
            win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, ex)
            if self._prev_foreground:
                try:
                    win32gui.SetForegroundWindow(self._prev_foreground)
                except Exception:
                    pass
        win32gui.SetWindowPos(
            self.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
            | win32con.SWP_FRAMECHANGED,
        )
        self._dirty = True

    def _on_char(self, hwnd, wparam):
        if not self.input_active:
            return 0
        ch = chr(wparam)
        if ch == "\r" or ch == "\n":
            self._submit()
            return 0
        if ch == "\b":
            self.input_text = self.input_text[:-1]
            self._dirty = True
            return 0
        if wparam < 32:
            return 0
        self.input_text += ch
        self._dirty = True
        return 0

    def _on_keydown(self, hwnd, wparam):
        if self.hotkey_editing and wparam == win32con.VK_ESCAPE:
            self.hotkey_editing = False
            self.input_text = ""
            self._set_input_active(False)
            self._dirty = True
            return 0
        if self.input_active and wparam == win32con.VK_ESCAPE:
            self._set_input_active(False)
            return 0
        if self.input_active and wparam == win32con.VK_RETURN:
            self._submit()
            return 0
        return win32gui.DefWindowProc(hwnd, win32con.WM_KEYDOWN, wparam, 0)

    def _submit(self) -> None:
        text = self.input_text.strip()
        self.input_text = ""
        self._dirty = True
        if self.hotkey_editing:
            self.hotkey_editing = False
            if text:
                try:
                    canonical = self.on_hotkey_change(text) if self.on_hotkey_change else text
                    self.hotkey_text = canonical
                    self.hint = f"快捷键已保存：{canonical}"
                except Exception as exc:
                    self.hotkey_editing = True
                    self.input_text = text
                    self.hint = f"快捷键无效：{exc}"
                    self._dirty = True
                    return
            self._set_input_active(False)
            return
        if text and self.on_submit:
            try:
                self.on_submit(text)
            except Exception:
                pass
        self._set_input_active(False)

    # ---------------- 位置持久化 ----------------

    def _load_position(self) -> tuple[int, int] | None:
        if not self.position_file or not self.position_file.exists():
            return None
        try:
            data = json.loads(self.position_file.read_text(encoding="utf-8"))
            return int(data["x"]), int(data["y"])
        except Exception:
            return None

    def _save_position(self) -> None:
        if not self.position_file:
            return
        self._bubble_pos = self._clamp_bubble_pos(self._bubble_pos)
        try:
            self.position_file.parent.mkdir(parents=True, exist_ok=True)
            self.position_file.write_text(
                json.dumps({"x": self._bubble_pos[0], "y": self._bubble_pos[1]}),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ---------------- 生命周期 ----------------

    def _quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.on_quit:
            try:
                self.on_quit()
            except Exception:
                pass
        # WM_QUIT 必须投递到当前 UI 线程的消息队列，而不是窗口句柄。
        win32gui.PostQuitMessage(0)

    def stop(self) -> None:
        self._closing = True
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_QUIT, 0, 0)
            except Exception:
                pass

    def _write_log(self, message: str) -> None:
        """往日志文件写一行（悬浮窗模块不依赖 voice_assistant，避免循环导入）。"""
        try:
            with open(Path(__file__).resolve().parent / "voice-assistant.log",
                      "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
        except OSError:
            pass

    def _recreate_window(self) -> None:
        """窗口句柄失效时重建（带退避，避免疯狂重试）。"""
        now = time.monotonic()
        if now - getattr(self, "_last_recreate", 0.0) < 2.0:
            return
        self._last_recreate = now
        try:
            sys.stderr.write("[overlay] 窗口失效，尝试重建…\n")
            sys.stderr.flush()
            self.hwnd = 0
            self.create()
            sys.stderr.write(f"[overlay] 窗口已重建 hwnd={self.hwnd}\n")
            sys.stderr.flush()
        except Exception as exc:
            sys.stderr.write(f"[overlay] 窗口重建失败: {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()

    def run(self) -> None:
        """主线程消息循环。"""
        if not self.hwnd:
            self.create()
        _user32.SetTimer(self.hwnd, 1, 40, None)
        while not self._closing:
            # PeekMessage 返回 [flags, (hwnd, msg, wp, lp, time, pt)]：
            #   flags 为 0 表示没有消息，但列表本身非空——不能只用 if picked 判断；
            #   六元组必须原样传给 TranslateMessage/DispatchMessage，
            #   传四元组会抛 "MSG param() takes exactly 6 arguments"。
            picked = win32gui.PeekMessage(0, 0, 0, win32con.PM_REMOVE)
            if picked and picked[0]:
                detail = picked[1]
                message = detail[1]
                if message not in (win32con.WM_TIMER, 0x8001):
                    self._write_log(f"主线程收到消息 msg={message} hwnd={detail[0]}")
                if message == win32con.WM_QUIT:
                    break
                if message == win32con.WM_DESTROY:
                    self.hwnd = 0
                    break
                if detail[0] == self.hwnd:
                    try:
                        win32gui.TranslateMessage(detail)
                        win32gui.DispatchMessage(detail)
                    except Exception as exc:
                        self._write_log(f"派发异常 {type(exc).__name__}: {exc}")
            else:
                time.sleep(0.008)

            # 自愈：窗口句柄失效（被系统销毁/重建失败）时自动重建，
            # 否则用户会看到悬浮球凭空消失，而进程还在跑。
            if self.hwnd and not win32gui.IsWindow(self.hwnd):
                self._recreate_window()

            self.render()

            # 心跳：每 15 秒确认消息循环还活着（写到日志文件）
            now = time.monotonic()
            if now - self._last_beat >= 15:
                self._last_beat = now
                alive = bool(self.hwnd) and win32gui.IsWindow(self.hwnd)
                self._write_log(
                    f"消息循环存活（窗口有效={alive}，渲染错误 {self._render_errors} 次）"
                )
                if not alive:
                    self._recreate_window()

        try:
            if self.hwnd:
                _user32.KillTimer(self.hwnd, 1)
                win32gui.DestroyWindow(self.hwnd)
        except Exception:
            pass
