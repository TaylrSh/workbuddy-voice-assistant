"""截取指定窗口的图像（纯 ctypes + GDI，无第三方依赖）。

用于验证注入是否真的落到了输入框里。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.PrintWindow.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT]
user32.PrintWindow.restype = wt.BOOL

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def capture_window(hwnd: int, out_path: str | Path) -> Path | None:
    """抓取窗口位图存成 PNG。失败返回 None。"""
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)

    ok = user32.PrintWindow(hwnd, mem_dc, 2)  # PW_RENDERFULLCONTENT

    header = BITMAPINFO()
    header.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.bmiHeader.biWidth = width
    header.bmiHeader.biHeight = -height  # 负值 = 自上而下
    header.bmiHeader.biPlanes = 1
    header.bmiHeader.biBitCount = 32
    header.bmiHeader.biCompression = 0

    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), DIB_RGB_COLORS)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, window_dc)

    if not ok:
        return None

    _write_png(buffer, width, height, out_path)
    return Path(out_path)


def _write_png(rgb_buffer, width: int, height: int, out_path: str | Path) -> None:
    """把 32 位 BGRA 数据写成 PNG（手写 PNG 编码，零依赖）。"""
    import struct
    import zlib

    # GDI 给的是 BGRA，PNG 要 RGBA —— 交换 R/B 两个通道
    bgra = bytes(rgb_buffer)
    rgba = bytearray(len(bgra))
    rgba[0::4] = bgra[2::4]   # R
    rgba[1::4] = bgra[1::4]   # G
    rgba[2::4] = bgra[0::4]   # B
    rgba[3::4] = bgra[3::4]   # A

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter type 0
        start = y * stride
        raw += rgba[start : start + stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")

    Path(out_path).write_bytes(png)


if __name__ == "__main__":
    import sys

    import wb_inject

    handle = int(sys.argv[1]) if len(sys.argv) > 1 else wb_inject.find_workbuddy_window()
    target = sys.argv[2] if len(sys.argv) > 2 else "window_test.png"
    result = capture_window(handle, target)
    print("已保存:", result if result else "失败")
