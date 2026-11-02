"""向 WorkBuddy 桌面版窗口注入文本。

WorkBuddy 的对话输入框是 Electron 的 contenteditable，没有对外的 HTTP 接口
可以写入。这里用操作系统级的方式投递：

    1. 把文本放进系统剪贴板
    2. 定位并激活 WorkBuddy 主窗口
    3. 模拟 Ctrl+V 粘贴
    4. 模拟 Enter 发送

这样注入的消息会真实出现在桌面版对话里，复用完整上下文与工具能力，
与用户手动输入完全一致。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 显式声明签名 —— 否则 64 位句柄会被当成 int 而溢出
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wt.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_long]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.BringWindowToTop.restype = wt.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wt.DWORD, ctypes.c_ulong]
user32.keybd_event.restype = None
user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wt.UINT

kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wt.DWORD

user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_ulong,
]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_ulong,
    ctypes.c_void_p,
]
user32.CallNextHookEx.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = ctypes.c_bool

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wt.DWORD),
        ("u", _U),
    ]


# ---------------------------------------------------------------- 剪贴板


def set_clipboard(text: str) -> bool:
    """把文本写入系统剪贴板（Unicode）。"""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        data = text + "\0"
        size = len(data) * ctypes.sizeof(ctypes.c_wchar)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return False
        ctypes.memmove(locked, ctypes.create_unicode_buffer(data), size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        return True
    finally:
        user32.CloseClipboard()


def get_clipboard() -> str:
    """读取剪贴板文本（用于自检）。"""
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return ""
        try:
            return ctypes.c_wchar_p(locked).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- 窗口查找


def find_workbuddy_window() -> int:
    """找出 WorkBuddy 主窗口句柄（可见且有标题）。"""
    found: list[int] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if "WorkBuddy" in title or "workbuddy" in title:
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found[0] if found else 0


def focus_window(hwnd: int) -> bool:
    """把窗口激活到前台。"""
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    # AttachThreadInput 能可靠地把窗口提到前台
    fg = user32.GetForegroundWindow()
    cur = kernel32.GetCurrentThreadId()
    target = user32.GetWindowThreadProcessId(hwnd, None)
    attached = False
    if fg:
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        if fg_thread and fg_thread != cur:
            attached = bool(user32.AttachThreadInput(cur, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur, user32.GetWindowThreadProcessId(fg, None), False)
    time.sleep(0.25)
    return user32.GetForegroundWindow() == hwnd


# ---------------------------------------------------------------- 模拟按键


def _send_input(inputs) -> int:
    """用 SendInput 投递一组键盘事件。"""
    count = len(inputs)
    array = (INPUT * count)()
    for i, item in enumerate(inputs):
        array[i] = item
    sent = user32.SendInput(count, ctypes.byref(array), ctypes.sizeof(INPUT))
    return int(sent)


def _mk_input(vk: int, flags: int = 0, scan: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = scan
    inp.ki.dwFlags = flags
    inp.ki.time = 0
    inp.ki.dwExtraInfo = None
    return inp


def send_ctrl_v() -> None:
    """模拟 Ctrl+V（SendInput + 扫描码，Electron 能正确识别）。"""
    events = [
        _mk_input(VK_CONTROL, 0),
        _mk_input(VK_V, 0),
        _mk_input(VK_V, KEYEVENTF_KEYUP),
        _mk_input(VK_CONTROL, KEYEVENTF_KEYUP),
    ]
    _send_input(events)
    time.sleep(0.08)


def send_enter() -> None:
    """模拟回车（发送 ScanCode 1，Chromium 靠扫描码区分回车与换行）。"""
    events = [
        _mk_input(VK_RETURN, 0, scan=0x1C),
        _mk_input(VK_RETURN, KEYEVENTF_KEYUP | KEYEVENTF_SCANCODE, scan=0x1C),
    ]
    _send_input(events)
    time.sleep(0.08)


def type_unicode(text: str) -> None:
    """直接输入 Unicode 文本（KEYEVENTF_UNICODE，逐字符）。"""
    for ch in text:
        code = ord(ch)
        events = [
            _mk_input(0, KEYEVENTF_UNICODE, scan=code),
            _mk_input(0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, scan=code),
        ]
        _send_input(events)
        time.sleep(0.005)


# ---------------------------------------------------------------- 对外接口


def inject_text(
    text: str,
    send: bool = True,
    use_clipboard: bool = True,
    focus: bool = True,
    verify: bool = True,
) -> dict:
    """把文本注入 WorkBuddy 输入框。

    返回结果字典：ok / method / hwnd / message / verified
    """
    if not text.strip():
        return {"ok": False, "message": "文本为空"}

    hwnd = find_workbuddy_window()
    if not hwnd:
        return {"ok": False, "message": "未找到 WorkBuddy 窗口"}

    if focus:
        focus_window(hwnd)
        time.sleep(0.2)

    method = "clipboard"
    if use_clipboard:
        if not set_clipboard(text):
            method = "unicode"
            if focus:
                focus_window(hwnd)
            type_unicode(text)
    else:
        method = "unicode"
        if focus:
            focus_window(hwnd)
        type_unicode(text)

    time.sleep(0.4)

    verified = None
    if verify and method == "clipboard":
        # 粘贴后剪贴板内容不变即说明粘贴被目标接收（Electron 会清空剪贴板）
        time.sleep(0.3)
        verified = get_clipboard() != text

    if send:
        send_enter()
        time.sleep(0.4)

    return {
        "ok": True,
        "method": method,
        "hwnd": hwnd,
        "verified": verified,
        "message": "已注入并发送" if send else "已注入输入框（未发送）",
    }


def input_is_busy() -> bool:
    """粗判当前 WorkBuddy 是否正忙（输入框可能被后台任务状态占用）。

    通过窗口标题判断不可靠，改用剪贴板探测：若粘贴后剪贴板仍持有
    原文，说明输入框没有接收粘贴。
    """
    hwnd = find_workbuddy_window()
    if not hwnd:
        return True

    probe = "__WB_BUSY_PROBE__"
    if not set_clipboard(probe):
        return True
    focus_window(hwnd)
    time.sleep(0.15)
    send_ctrl_v()
    time.sleep(0.35)
    consumed = get_clipboard() != probe
    # 清掉探测文本
    if consumed:
        send_enter()
    set_clipboard("")
    return not consumed
