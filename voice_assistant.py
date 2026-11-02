"""WorkBuddy 语音助手 —— 主程序（悬浮窗版）。

用法：
    按住 Ctrl+Alt+V 说话，松手即发送；也可以直接按住悬浮球面板上的麦克风按钮。
    WorkBuddy 处理完会朗读回复，全过程显示在悬浮窗里。

完整链路：
    热键/鼠标按下 → 录音 → 松手停止 → 注入 WorkBuddy 对话
    → 读取回复 → 显示在悬浮窗 → 语音播报

关键设计：
  - 转写复用 WorkBuddy 内置的 AudioTranscribe，不自建 ASR。做法是把录音
    文件路径连同转写指令一起注入对话，由 WorkBuddy 自己调用工具完成。
  - 注入走桌面版 UI（CDP 直连 DOM，失败降级剪贴板/ACP），消息以真实用户
    身份出现在对话中，复用完整上下文与工具权限，用户全程可见可干预。
  - 回复从最近活跃的会话 jsonl 增量读取，不与 UI 抢焦点。
  - 悬浮窗用 Win32 分层窗口实现：置顶、圆角透明、点击不抢游戏焦点。

命令：
    python voice_assistant.py              # 启动守护模式（悬浮窗 + 按住说话）
    python voice_assistant.py --once       # 录一次处理完退出
    python voice_assistant.py --text "..." # 直接注入文本
    python voice_assistant.py --tts "..."  # 只朗读文本
    python voice_assistant.py --devices   # 列出录音设备
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import wb_acp
import wb_cdp
import wb_hotkey
import wb_inject
import wb_mic
import wb_overlay
import wb_session
import wb_tts

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "recordings"
LOG_FILE = BASE_DIR / "voice-assistant.log"
POSITION_FILE = BASE_DIR / "overlay-position.json"
CONFIG_FILE = BASE_DIR / "voice-assistant-config.json"
DEFAULT_HOTKEY = "Ctrl+Alt+V"

# 单实例互斥句柄，必须保持存活
_MUTEX_HANDLE = None

# 交给 WorkBuddy 的转写指令。要求输出固定标签，方便悬浮窗分栏显示。
TRANSCRIBE_PROMPT = (
    "请用 AudioTranscribe 工具转写音频文件 `{path}`。\n"
    "如果没有有效语音，输出：<transcript></transcript><final>没有听到</final>\n"
    "否则输出：<transcript>识别出来的原文</transcript>"
    "<final>把原文当作用户指令执行后的结论，中文简洁，不超过 100 字</final>\n"
    "只输出这两个标签本身，不要有任何其他内容。"
)

TEXT_PROMPT = (
    "请处理下面的用户指令，把最终结论用中文简洁说明（不超过 100 字），"
    "严格用 <final>结论内容</final> 格式包裹，不要输出其他内容。\n"
    "用户指令：{text}"
)


def load_hotkey() -> str:
    """读取已保存的快捷键；配置缺失或无效时使用默认值。"""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return wb_hotkey.parse_hotkey(data.get("hotkey", DEFAULT_HOTKEY))[2]
    except Exception:
        return DEFAULT_HOTKEY


def save_hotkey(text: str) -> None:
    CONFIG_FILE.write_text(
        json.dumps({"hotkey": text}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    # pythonw.exe 没有控制台，sys.stdout 是 None，
    # print 会抛异常——必须先落盘，再尝试打印。
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def acquire_single_instance() -> bool:
    """确保全局只有一个实例在跑。

    两个实例会抢同一个窗口类、抢同一个全局键盘钩子，
    容易互相把对方的窗口/钩子干掉，表现就是进程莫名消失。
    返回 True 表示拿到了实例锁。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Global\\WorkBuddyVoiceAssistant")
        # ERROR_ALREADY_EXISTS = 183
        already = kernel32.GetLastError() == 183
        if already:
            return False
        # 句柄要保持存活，存到模块级变量里
        global _MUTEX_HANDLE
        _MUTEX_HANDLE = handle
        return True
    except Exception:
        return True  # 拿不到锁就别拦用户


def _install_thread_excepthook() -> None:
    """子线程里的异常也要落盘——否则线程死了主进程还傻活着。"""
    import threading

    def hook(args):
        import traceback

        detail = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        stamp = datetime.now().isoformat(timespec="seconds")
        text = f"\n===== [{stamp}] 线程异常 ({args.thread.name}) =====\n{detail}"
        sys.stderr.write(text)
        sys.stderr.flush()
        try:
            with LOG_FILE.open("a", encoding="utf-8") as lf:
                lf.write(text + "\n")
        except OSError:
            pass

    threading.excepthook = hook


def install_crash_logger() -> None:
    """把未捕获异常与崩溃转储写进日志文件。

    之前守护进程崩过一次却毫无痕迹——stderr 只进了控制台管道，
    窗口一关就丢了。这里让所有异常都有据可查。
    """
    crash_file = BASE_DIR / "voice-assistant-crash.log"
    try:
        fh = crash_file.open("a", encoding="utf-8")
    except OSError:
        fh = None

    def _handle(exc_type, exc_value, exc_tb):
        if exc_type is KeyboardInterrupt:
            return
        try:
            import traceback

            detail = "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            )
        except Exception:
            detail = f"{exc_type.__name__}: {exc_value}\n"
        stamp = datetime.now().isoformat(timespec="seconds")
        text = f"\n===== [{stamp}] 未捕获异常 =====\n{detail}"
        sys.stderr.write(text)
        sys.stderr.flush()
        try:
            with LOG_FILE.open("a", encoding="utf-8") as lf:
                lf.write(text + "\n")
        except OSError:
            pass
        if fh is not None:
            try:
                fh.write(text + "\n")
                fh.flush()
            except OSError:
                pass

    sys.excepthook = _handle
    if fh is not None:
        try:
            faulthandler.enable(fh)
        except Exception:
            pass


class NullOverlay:
    """没有悬浮窗时的空实现，保证主流程不必到处判断。"""

    def set_state(self, state: str, hint: str | None = None) -> None: ...
    def set_transcript(self, text: str) -> None: ...
    def set_reply(self, text: str) -> None: ...
    def set_hint(self, text: str) -> None: ...
    def push_level(self, level: float) -> None: ...
    def clear_levels(self) -> None: ...
    def expand(self, expanded: bool = True) -> None: ...
    def toggle(self) -> None: ...
    def set_input_text(self, text: str) -> None: ...


class VoiceAssistant:
    """语音助手主体。"""

    def __init__(
        self,
        cwd: str | None = None,
        voice: str = wb_tts.VOICE_ZH_CN,
        speak_replies: bool = True,
        device: str | None = None,
        timeout: float = 300.0,
        overlay=None,
        wait_idle: float = 60.0,
    ) -> None:
        self.cwd = cwd
        self.voice = voice
        self.speak_replies = speak_replies
        self.device = device
        self.timeout = timeout
        self.wait_idle = wait_idle
        self.hotkey_text = DEFAULT_HOTKEY
        self.overlay = overlay or NullOverlay()
        self._acp: tuple | None = None
        self._busy = False
        self._lock = threading.Lock()
        self._stop_flag: wb_mic.StopFlag | None = None

    # ---------------- 会话定位 ----------------

    def resolve_cwd(self) -> str:
        if self.cwd:
            return self.cwd
        # 优先挑一个 cwd 里有会话记录文件的会话，否则读不到回复
        for session in wb_acp.discover_sessions():
            if session.cwd and wb_session.find_session_file(session.cwd):
                return session.cwd
        session = wb_acp.pick_session(probe=True)
        if session is None:
            raise RuntimeError("没有找到运行中的 WorkBuddy 会话，请先打开桌面版。")
        return session.cwd

    # ---------------- 录音 ----------------

    def start_recording(self) -> bool:
        """开始录音。返回 False 表示当前忙，不接受新的录音。"""
        with self._lock:
            if self._busy:
                self.overlay.set_hint("上一个任务还在处理中…")
                return False
            self._busy = True
            stop = wb_mic.StopFlag()
            self._stop_flag = stop

        log("开始录音")
        self.overlay.clear_levels()
        self.overlay.set_transcript("")
        self.overlay.set_state(wb_overlay.STATE_RECORDING, "正在录音… 松开发送")
        log("已进入录音状态")

        def worker() -> None:
            try:
                wav = self._record(stop)
                log(f"录音线程拿到 wav={wav}")
                if wav is not None:
                    log("开始处理 wav")
                    self.handle_wav(wav)
                    log("处理完成")
            except Exception as exc:
                log(f"处理失败：{exc}")
                self.overlay.set_state(wb_overlay.STATE_ERROR, f"出错：{exc}")
                time.sleep(2.5)
                self.overlay.set_state(wb_overlay.STATE_IDLE, f"按住 {self.hotkey_text} 说话")
            finally:
                log("录音线程 finally 开始")
                with self._lock:
                    self._busy = False
                log("录音线程 finally 结束")

        threading.Thread(target=worker, daemon=True).start()
        return True

    def stop_recording(self) -> None:
        if self._stop_flag is not None:
            self._stop_flag.request_stop()

    def _record(self, stop: wb_mic.StopFlag) -> Path | None:
        """录音并做基本校验；不合格返回 None。"""
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = AUDIO_DIR / f"rec_{stamp}.wav"

        wb_mic.record(
            path,
            max_seconds=120.0,
            device=self.device,
            stop_flag=stop,
            on_level=self.overlay.push_level,
        )

        duration = wb_mic.duration_of(path)
        peak = wb_mic.peak_amplitude(path)
        log(f"录音结束 {duration:.1f} 秒（峰值 {peak}）")

        if duration < 0.4 or not wb_mic.has_speech(path, threshold=80):
            path.unlink(missing_ok=True)
            self.overlay.set_transcript("")
            self.overlay.set_state(wb_overlay.STATE_IDLE, "没听清，再说一次？")
            return None
        return path

    def record_once(self, max_seconds: float = 60.0) -> Path:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = AUDIO_DIR / f"rec_{stamp}.wav"
        log("开始录音…")
        wb_mic.record(path, max_seconds=max_seconds, device=self.device)
        duration = wb_mic.duration_of(path)
        peak = wb_mic.peak_amplitude(path)
        log(f"录音结束 {duration:.1f} 秒（峰值 {peak}）")
        if not wb_mic.has_speech(path, threshold=80):
            path.unlink(missing_ok=True)
            raise RuntimeError("未检测到有效语音")
        return path

    # ---------------- 注入与回复 ----------------

    def inject(self, text: str, retries: int = 3, wait_idle: float = 60.0) -> dict:
        """把文本注入 WorkBuddy 输入框并发送。

        三级降级：
          1. CDP 直接操作 DOM —— 最可靠，不依赖焦点与合成键盘
          2. 剪贴板 + 模拟按键 —— CDP 未开启时的备选
          3. ACP 协议注入 —— 兜底，功能正常但消息不在界面上显示

        WorkBuddy 正在跑任务时输入框不可用，这里会先等它空闲（排队），
        再注入并校验消息是否真的发出去了。
        """
        # ACP 会话跨语音指令复用，避免每句话都重新发现端口、握手和建会话。
        if self._acp is not None:
            return {"ok": True, "channel": "acp", "message": "复用 ACP 后台会话"}

        if wb_cdp.is_available():
            # 1) 等 WorkBuddy 空闲
            if wait_idle > 0:
                deadline = time.monotonic() + wait_idle
                while time.monotonic() < deadline:
                    status = wb_cdp.composer_status()
                    if status.get("error"):
                        break
                    if not status.get("busy"):
                        break
                    left = int(deadline - time.monotonic())
                    self.overlay.set_hint(f"WorkBuddy 正在忙，排队等待…（{left}s）")
                    time.sleep(1.5)

            # 2) 注入 + 校验
            for _ in range(retries):
                result = wb_cdp.inject_text(text, send=True)
                if result.get("ok"):
                    return {**result, "channel": "cdp"}
                log(f"CDP 注入失败：{result.get('message')}")
                time.sleep(2.0)

        # 3) ACP 后台协议：不激活 WorkBuddy 窗口，避免游戏被切出全屏
        acp_error = None
        try:
            session = wb_acp.pick_session(prefer_cwd=self.resolve_cwd())
            if session is None:
                raise RuntimeError("没有可用会话")
            conn = wb_acp.connect(session)
            wb_acp.initialize(conn)
            session_id = wb_acp.new_session(conn, session.cwd)
            self._acp = (conn, session_id)
            log("CDP 不可用，已改用 ACP 后台通信")
            return {"ok": True, "channel": "acp", "message": "已通过 ACP 后台通信"}
        except Exception as exc:
            acp_error = exc
            log(f"ACP 后台通信失败：{exc}")

        # 4) 剪贴板 + 模拟按键仅作最后兜底（可能切换窗口焦点）
        last: dict = {}
        for _ in range(retries):
            last = wb_inject.inject_text(text, send=True, verify=True)
            if last.get("ok"):
                return {**last, "channel": "ui"}
            time.sleep(1.5)

        raise RuntimeError(
            f"注入失败（ACP: {acp_error}；UI: {last.get('message')}）"
        )

    def _wait_reply(self, watcher, timeout: float) -> str:
        """等待回复，同时把耗时回报给悬浮窗。

        正常情况回复里带 <final>，见到就返回；
        万一 WorkBuddy 没按格式输出，则在**连续 25 秒没有新消息**后兜底返回，
        避免长任务里被中间的叙述消息提前截断。
        """
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        collected: list[str] = []
        last_activity: float | None = None

        while time.monotonic() < deadline:
            new_messages = watcher.poll()
            if new_messages:
                collected.extend(new_messages)
                last_activity = time.monotonic()

            if any("<final>" in m for m in collected):
                time.sleep(0.8)
                collected.extend(watcher.poll())
                return "\n".join(collected)

            # 兜底：只有在「安静」一段时间后才返回，而不是第一条消息后就计时
            if collected and last_activity is not None:
                if time.monotonic() - last_activity > 25:
                    return "\n".join(collected)

            elapsed = int(time.monotonic() - started)
            self.overlay.set_hint(f"WorkBuddy 处理中… {elapsed}s")
            time.sleep(0.8)
        return "\n".join(collected)

    def process(self, prompt: str) -> str:
        """注入指令并取回回复。"""
        watcher = wb_session.RecentSessionWatcher.create()
        log("发送到 WorkBuddy…")
        result = self.inject(prompt, wait_idle=self.wait_idle)
        channel = result.get("channel", "ui")

        if channel == "acp":
            conn, session_id = self._acp
            pieces: list[str] = []
            for piece in wb_acp.send_prompt(conn, session_id, prompt):
                pieces.append(piece)
                sys.stdout.write(piece)
                sys.stdout.flush()
            print()
            return "".join(pieces)

        return self._wait_reply(watcher, self.timeout)

    # ---------------- 输出 ----------------

    def speak(self, text: str) -> None:
        if self.speak_replies and text.strip():
            log("朗读回复…")
            wb_tts.speak(text, voice=self.voice)

    def _finish(self, reply: str) -> str:
        """解析回复、更新悬浮窗、朗读。"""
        transcript = wb_acp.extract_tag(reply, "transcript")
        final = wb_acp.extract_tag(reply, "final") or wb_acp.extract_final(reply)

        self.overlay.set_transcript(transcript or "（未识别到语音）")
        self.overlay.set_reply(final or reply)
        self.overlay.set_state(wb_overlay.STATE_SPEAKING, "朗读回复…")
        print(f"\n[WorkBuddy] {final or reply}\n")
        self.speak(final or reply)
        self.overlay.set_state(wb_overlay.STATE_IDLE, f"按住 {self.hotkey_text} 说话")
        return final or reply

    # ---------------- 完整流程 ----------------

    def handle_wav(self, wav: Path) -> int:
        self.overlay.set_state(wb_overlay.STATE_THINKING, "转写中…")
        prompt = TRANSCRIBE_PROMPT.format(path=str(wav).replace("\\", "/"))
        reply = self.process(prompt)
        self._finish(reply)
        return 0

    def submit_text(self, text: str) -> str:
        """处理一条文字指令。"""
        with self._lock:
            if self._busy:
                self.overlay.set_hint("上一个任务还在处理中…")
                return ""
            self._busy = True
        try:
            self.overlay.set_state(wb_overlay.STATE_THINKING, "处理中…")
            prompt = TEXT_PROMPT.format(text=text)
            reply = self.process(prompt)
            self._finish(reply)
            return reply
        except Exception as exc:
            log(f"处理失败：{exc}")
            self.overlay.set_state(wb_overlay.STATE_ERROR, f"出错：{exc}")
            time.sleep(2.5)
            self.overlay.set_state(wb_overlay.STATE_IDLE, f"按住 {self.hotkey_text} 说话")
            return ""
        finally:
            with self._lock:
                self._busy = False

    def run_once(self, max_seconds: float = 60.0) -> int:
        try:
            wav = self.record_once(max_seconds=max_seconds)
        except RuntimeError as exc:
            log(f"跳过：{exc}")
            return 1
        return self.handle_wav(wav)

    def run_text(self, text: str) -> int:
        prompt = TEXT_PROMPT.format(text=text)
        reply = self.process(prompt)
        if reply:
            print(f"\n[WorkBuddy] {reply}\n")
            self.speak(reply)
        return 0


def hotkey_worker(hotkey: wb_hotkey.PushToTalk, assistant: VoiceAssistant,
                  stop_event: threading.Event) -> None:
    """监听热键：按下开始录音，松手停止。

    单次交互出错（比如麦克风被占用、注入失败）只记日志，
    不能让整个守护进程退出。
    """
    while not stop_event.is_set():
        try:
            if hotkey.held.wait(0.05):
                started = assistant.start_recording()
                hotkey.wait_until_released()
                if started:
                    assistant.stop_recording()
                time.sleep(0.25)
        except Exception as exc:
            log(f"热键处理出错（已忽略）：{type(exc).__name__}: {exc}")
            time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkBuddy 语音助手：按住说话")
    parser.add_argument("--once", action="store_true", help="录一次音并处理后退出")
    parser.add_argument("--text", help="直接注入文本（跳过录音）")
    parser.add_argument("--tts", help="只朗读这段文字")
    parser.add_argument("--cwd", help="目标 WorkBuddy 会话的工作目录")
    parser.add_argument("--max-seconds", type=float, default=60.0, help="单次录音上限")
    parser.add_argument("--timeout", type=float, default=300.0, help="等待回复超时")
    parser.add_argument("--voice", default=wb_tts.VOICE_ZH_CN, help="TTS 音色")
    parser.add_argument("--no-speak", action="store_true", help="不朗读回复")
    parser.add_argument("--device", help="录音设备名关键字")
    parser.add_argument("--devices", action="store_true", help="列出录音设备")
    parser.add_argument("--hotkey-test", action="store_true", help="仅测试热键监听")
    parser.add_argument("--no-overlay", action="store_true", help="不显示悬浮窗")
    parser.add_argument("--dark", action="store_true", help="悬浮窗深色主题")
    parser.add_argument("--scale", type=float, default=1.0, help="悬浮窗缩放")
    parser.add_argument("--wait-idle", type=float, default=60.0,
                        help="WorkBuddy 忙碌时最多等待空闲的秒数")
    args = parser.parse_args()

    if args.devices:
        print("可用录音设备：")
        for name in wb_mic.list_input_devices():
            print(f"  - {name}")
        return 0

    if args.tts:
        wb_tts.speak(args.tts, voice=args.voice)
        return 0

    if args.hotkey_test:
        listener = wb_hotkey.PushToTalk()
        if not listener.start():
            print("热键钩子安装失败")
            return 1
        print("按住 Ctrl+Alt+V 测试…")
        while True:
            if listener.held.wait(0.2):
                print("已按下")
                listener.wait_until_released()
                print("已松开")
        return 0

    assistant = VoiceAssistant(
        cwd=args.cwd,
        voice=args.voice,
        speak_replies=not args.no_speak,
        device=args.device,
        timeout=args.timeout,
        wait_idle=args.wait_idle,
    )

    if args.text:
        return assistant.run_text(args.text)

    if args.once:
        return assistant.run_once(max_seconds=args.max_seconds)

    # ---------------- 守护模式 ----------------
    if not acquire_single_instance():
        log("已有实例在运行，本次启动放弃（避免两个实例互相干扰）。")
        return 0

    stop_event = threading.Event()

    hotkey_text = load_hotkey()
    assistant.hotkey_text = hotkey_text
    modifiers, key, hotkey_text = wb_hotkey.parse_hotkey(hotkey_text)
    hotkey = wb_hotkey.PushToTalk(key=key, modifiers=modifiers)

    def change_hotkey(text: str) -> str:
        new_modifiers, new_key, canonical = wb_hotkey.parse_hotkey(text)
        save_hotkey(canonical)
        hotkey.configure(new_modifiers, new_key)
        assistant.hotkey_text = canonical
        log(f"快捷键已更新：{canonical}")
        return canonical

    # 用 GetAsyncKeyState 轮询而不是低级键盘钩子——
    # 钩子是 ctypes 回调，出问题会让 pywin32 直接杀进程（按热键就闪退）。
    hotkey_ok = hotkey.start_polling()
    if hotkey_ok:
        log(f"热键监听已启动（{hotkey_text}，轮询模式）")
    else:
        log("热键监听启动失败，可改用鼠标点悬浮窗上的麦克风。")

    overlay = None
    if not args.no_overlay:
        overlay = wb_overlay.Overlay(
            on_mic_down=assistant.start_recording,
            on_mic_up=assistant.stop_recording,
            on_submit=lambda text: threading.Thread(
                target=assistant.submit_text, args=(text,), daemon=True
            ).start(),
            on_quit=stop_event.set,
            on_hotkey_change=change_hotkey,
            dark=args.dark,
            scale=args.scale,
            position_file=POSITION_FILE,
            hotkey_text=hotkey_text,
        )
        assistant.overlay = overlay

    threading.Thread(
        target=hotkey_worker, args=(hotkey, assistant, stop_event), daemon=True
    ).start()

    log(f"语音助手已启动：按住 {hotkey_text} 说话，松手发送；Ctrl+C 退出。")

    if overlay is not None:
        try:
            overlay.create()
            log(f"悬浮窗已创建 hwnd={overlay.hwnd}")
        except Exception:
            import traceback
            log("悬浮窗创建失败：\n" + traceback.format_exc())
            return 1
        overlay.run()          # 主线程跑窗口消息循环
        stop_event.set()
        hotkey.stop_polling()
        hotkey.stop()
        return 0

    # 没有悬浮窗时的传统轮询模式
    try:
        while not stop_event.is_set():
            if hotkey.held.wait(0.2):
                assistant.start_recording()
                hotkey.wait_until_released()
                assistant.stop_recording()
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        hotkey.stop_polling()
        hotkey.stop()
    log("已退出")
    return 0


if __name__ == "__main__":
    install_crash_logger()
    _install_thread_excepthook()
    # 崩溃诊断：把原生崩溃（访问违例等）的栈写到文件。
    # faulthandler.enable 必须在 dump_traceback_later 之前调用，
    # 否则定时转储不会生效（之前查不到原因就是因为这个）。
    try:
        hang_file = open(BASE_DIR / "voice-assistant-hang.log", "a", encoding="utf-8")
        faulthandler.enable(hang_file)
        faulthandler.dump_traceback_later(20, repeat=True, file=hang_file)
    except Exception:
        pass
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback

        log("守护进程异常退出：\n" + traceback.format_exc())
        raise
