"""全局热键：按住 Ctrl+Alt+V 说话，松开发送。

需要同时监听 keydown 与 keyup，因此用低级键盘钩子（WH_KEYBOARD_LL）
而不是 RegisterHotKey（后者只在按下时触发）。

实现要点：
  - 钩子回调必须常驻，不能被 GC 回收
  - 回调里只做最小工作（设置事件标志），实际处理放到主线程
  - 按住时以固定间隔重复触发，方便边说边看时长
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)

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
# CallNextHookEx 返回 LRESULT（整数），声明成 c_void_p 会让回调返回值类型不对
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostThreadMessageW.restype = wt.BOOL

# GetModuleHandleW / GetCurrentThreadId 在 kernel32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wt.DWORD

WH_KEYBOARD_LL = 13

# 键盘钩子报告的是扩展键码，必须一并识别左右键
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_QUIT = 0x0012

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_V = 0x56
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002

MODIFIER_VKS = {
    "Ctrl": (VK_CONTROL,),
    "Alt": (VK_MENU,),
    "Shift": (0x10,),
    "Win": (0x5B, 0x5C),
}
MODIFIER_ALIASES = {
    "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "menu": "Alt",
    "shift": "Shift", "win": "Win", "windows": "Win", "cmd": "Win",
}
KEY_VKS = {chr(code): code for code in range(ord("A"), ord("Z") + 1)}
KEY_VKS.update({str(code): ord(str(code)) for code in range(10)})
KEY_VKS.update({f"F{i}": 0x6F + i for i in range(1, 25)})
KEY_VKS.update({
    "Space": 0x20, "Tab": 0x09, "Insert": 0x2D, "Delete": 0x2E,
    "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
})
KEY_ALIASES = {
    "spacebar": "Space", "esc": "Escape", "del": "Delete",
    "pgup": "PageUp", "pgdn": "PageDown",
}


def parse_hotkey(text: str) -> tuple[frozenset[str], int, str]:
    """解析 Ctrl+Shift+Q 形式的组合键，返回修饰键、主键码和标准文本。"""
    parts = [part.strip() for part in text.replace("-", "+").split("+") if part.strip()]
    if not parts:
        raise ValueError("请输入组合键，例如 Ctrl+Shift+Q")
    modifiers: set[str] = set()
    main_keys: list[str] = []
    for part in parts:
        lower = part.lower()
        if lower in MODIFIER_ALIASES:
            modifiers.add(MODIFIER_ALIASES[lower])
            continue
        canonical = KEY_ALIASES.get(lower, part.upper() if len(part) == 1 else part.title())
        if canonical == "Escape" or canonical not in KEY_VKS:
            raise ValueError(f"不支持的主键：{part}")
        main_keys.append(canonical)
    if len(main_keys) != 1:
        raise ValueError("组合键必须且只能包含一个主键")
    key_name = main_keys[0]
    ordered = [name for name in ("Ctrl", "Alt", "Shift", "Win") if name in modifiers]
    canonical_text = "+".join(ordered + [key_name])
    return frozenset(modifiers), KEY_VKS[key_name], canonical_text


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class PushToTalk:
    """按住 Ctrl+Alt+V 录音，松手停止。"""

    def __init__(self, key: int = VK_V, modifiers: frozenset[str] | None = None) -> None:
        self.key = key
        self.modifiers = modifiers or frozenset({"Ctrl", "Alt"})
        self.held = threading.Event()      # 按下时置位
        self.released = threading.Event()   # 松开时置位
        self._ctrl_down = False
        self._alt_down = False
        self._key_down = False
        self._hook_handle = None
        self._thread: threading.Thread | None = None
        self._proc = None
        self._poll_thread: threading.Thread | None = None
        self._stopped = threading.Event()

    # ---------------- 轮询实现（不依赖钩子） ----------------

    def start_polling(self) -> bool:
        """用 GetAsyncKeyState 轮询代替全局键盘钩子。

        低级键盘钩子（WH_KEYBOARD_LL）是 ctypes 回调，一旦回调里出任何问题，
        pywin32 会直接终止整个进程（用户看到的就是「按热键闪退」）。
        轮询虽然没那么「实时」（约 30ms 延迟），但**绝对不会崩**，
        对于按住说话这种场景完全够用。
        """
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        time.sleep(0.2)
        return True

    def configure(self, modifiers: frozenset[str], key: int) -> None:
        """轮询线程会在下一轮读取新组合键，无需重启监听器。"""
        self.modifiers = modifiers
        self.key = key
        self.held.clear()
        self.released.set()

    def _poll_loop(self) -> None:
        key = False
        while not self._stopped.is_set():
            # 高位 0x8000 表示当前按下
            k = user32.GetAsyncKeyState(self.key) & 0x8000
            modifiers_down = all(
                any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in MODIFIER_VKS[name])
                for name in self.modifiers
            )

            combo = bool(modifiers_down and k)
            if combo and not key:
                key = True
                self._key_down = True
                self.held.set()
                self.released.clear()
                self._log("轮询检测到热键按下")
            elif not combo and key:
                key = False
                self._key_down = False
                self.held.clear()
                self.released.set()

            time.sleep(0.03)

    def stop_polling(self) -> None:
        self._stopped.set()

    # ---------------- 钩子回调 ----------------

    def _callback(self, code: int, wparam: int, lparam: int) -> int:
        # 注意：这是 ctypes 回调，异常绝不能逃出去——
        # pywin32 遇到回调异常会直接终止整个进程（表现为闪退）。
        try:
            if code == HC_ACTION:
                data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = data.vkCode

                # wParam 低 16 位是消息号。WM_KEYUP=0x101 的 bit 0 是
                # 「扩展键」标志（不是重复标志），所以这里只比低 16 位原值：
                #   0x0100 = WM_KEYDOWN，0x0101 = WM_KEYUP
                # 若用 & ~3 掩码，KEYUP 会被误判成 KEYDOWN（按住不放不松手）。
                msg = wparam & 0xFFFF
                is_down = msg == WM_KEYDOWN
                is_up = msg == WM_KEYUP

                # 键盘钩子拿到的是「扩展键码」：
                # 左Ctrl=0xA2、右Ctrl=0xA3、左Alt=0xA4、右Alt=0xA5，
                # 与 VK_CONTROL(0x11)/VK_MENU(0x12) 不相等——
                # 只比 VK_* 的话左右键都识别不到，热键永远不触发。
                if vk in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
                    self._ctrl_down = is_down
                    if not is_down and self._key_down:
                        # 松开修饰键时也要结束按住状态，否则会卡在「一直按住」
                        self._key_down = False
                        self.held.clear()
                        self.released.set()
                elif vk in (VK_MENU, VK_LMENU, VK_RMENU):
                    self._alt_down = is_down
                    if not is_down and self._key_down:
                        self._key_down = False
                        self.held.clear()
                        self.released.set()
                elif vk == self.key:
                    if is_down:
                        # 只在 Ctrl 与 Alt 都按下时才算
                        if self._ctrl_down and self._alt_down:
                            if not self._key_down:
                                self._key_down = True
                                self.held.set()
                                self.released.clear()
                    elif is_up:
                        if self._key_down:
                            self._key_down = False
                            self.held.clear()
                            self.released.set()
        except Exception:
            pass
        # CallNextHookEx 的第一个参数传 None 表示"当前钩子"。
        # 绝不能传 self._hook_handle——它在钩子线程里赋值，
        # 早期回调时可能还是 None，ctypes 回调里抛异常会让 pywin32
        # 直接终止整个进程（表现为按快捷键就闪退）。
        try:
            return user32.CallNextHookEx(None, code, wparam, lparam)
        except Exception:
            return 0

    def _log(self, message: str) -> None:
        """往日志文件追加一行（钩子回调里调用，必须绝不出错）。"""
        try:
            import time
            from pathlib import Path

            path = Path(__file__).resolve().parent / "voice-assistant.log"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] [hook] {message}\n")
                fh.flush()
        except Exception:
            pass

    # ---------------- 线程管理 ----------------

    def _run(self) -> None:
        self._proc = HOOKPROC(self._callback)
        module = kernel32.GetModuleHandleW(None)
        self._hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc, module, 0
        )
        if not self._hook_handle:
            return
        # 泵消息循环，让钩子持续生效
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass

    def start(self) -> bool:
        """启动钩子线程。"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        time.sleep(0.4)
        return self._hook_handle is not None

    def stop(self) -> None:
        if self._hook_handle:
            user32.PostThreadMessageW(
                kernel32.GetCurrentThreadId(), WM_QUIT, 0, 0
            )
            user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None

    def wait_until_released(self, timeout: float | None = None) -> bool:
        """等待用户松手。返回是否成功等到。"""
        if not self.held.is_set():
            return False
        return self.released.wait(timeout)


def main() -> None:
    """自检：按住 Ctrl+Alt+V 打印事件。"""
    listener = PushToTalk()
    if not listener.start():
        print("钩子安装失败，可能需要管理员权限")
        return
    print("已就绪：按住 Ctrl+Alt+V，松开时提示。")
    try:
        while True:
            if listener.held.wait(0.2):
                print("按下")
                listener.wait_until_released()
                print("松开")
    except KeyboardInterrupt:
        listener.stop()


if __name__ == "__main__":
    main()
