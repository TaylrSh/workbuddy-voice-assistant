"""麦克风录音 —— 主实现用 sounddevice(PortAudio/WASAPI)，零安装依赖亦可回退 winmm。

设计要点：
  - 主路径 sounddevice：Windows 10+ 的 WASAPI 通路，设备选择明确，行为稳定
  - 备用路径 winmm：纯 ctypes，无需任何第三方包；本机实测 waveInPrepareHeader
    稳定返回 MMSYSERR_INVALHANDLE(11)，因此只作兜底，不作默认
  - 输出统一为 WAV：PCM 16bit 单声道 16kHz，WorkBuddy 的 AudioTranscribe 直接可吃

实测结论：
  - sounddevice 可枚举到多个 Realtek 麦克风，默认输入设备即"Microsoft 声音映射器"
  - 16kHz 单声道重采样由 sounddevice 内部完成，无需 numpy 手动重采样
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import threading
import time
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1
BITS_PER_SAMPLE = 16
BLOCK_ALIGN = CHANNELS * BITS_PER_SAMPLE // 8
BYTES_PER_SEC = SAMPLE_RATE * BLOCK_ALIGN

WAVE_MAPPER = -1
MMSYSERR_NOERROR = 0
WHDR_DONE = 0x00000001


class MicrophoneUnavailable(RuntimeError):
    """麦克风不可用。"""


class StopFlag:
    """线程安全的停止标记。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def is_set(self) -> bool:
        return self._event.is_set()

    def request_stop(self) -> None:
        self._event.set()


# --------------------------------------------------------------------------
# sounddevice 路径（主）
# --------------------------------------------------------------------------

def _sd():
    try:
        import sounddevice  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise MicrophoneUnavailable(
            "未安装 sounddevice，无法录音。请执行：pip install sounddevice numpy"
        ) from exc
    return sounddevice


def list_input_devices() -> list[str]:
    """列出可用的录音设备名（sounddevice 优先，失败则退回 winmm）。"""
    names: list[str] = []
    try:
        sd = _sd()
        for dev in sd.query_devices():
            if dev["max_input_channels"] > 0:
                names.append(f"{dev['name']} [{dev['default_samplerate']:.0f}Hz]")
    except Exception:
        pass

    if not names:
        names = _winmm_devices()
    return names


def pick_input_device(prefer: str | None = None) -> int | None:
    """挑选输入设备索引。

    prefer 给出时按名称子串匹配；否则优先"麦克风"，其次用系统默认输入设备。
    """
    sd = _sd()
    devices = sd.query_devices()
    candidates = [
        (i, d)
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]
    if not candidates:
        return None

    if prefer:
        lowered = prefer.lower()
        for i, d in candidates:
            if lowered in d["name"].lower():
                return i

    # 优先带"麦克风"字样的真实采集设备
    for i, d in candidates:
        if "麦克风" in d["name"] or "microphone" in d["name"].lower():
            return i

    try:
        default = sd.query_devices(kind="input")["index"]
        return int(default)
    except Exception:
        return candidates[0][0]


def record(
    path: str | Path,
    max_seconds: float = 60.0,
    device: str | None = None,
    stop_flag: StopFlag | None = None,
    on_start=None,
    on_progress=None,
    on_level=None,
) -> Path:
    """录音并写入 WAV 文件，返回路径。

    device 为设备名关键字；None 表示自动挑选。
    stop_flag 被 request_stop() 标记后提前结束（用于「松手即停」）。
    on_start / on_progress 是可选回调，用于把状态回报给调用方。
    on_level 是可选回调，收到 0~1 的实时音量，用于驱动波形动画。
    """
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sd = _sd()
    dev_index = pick_input_device(device)
    if dev_index is None:
        raise MicrophoneUnavailable("系统没有可用的录音设备。")

    frames: list[bytes] = []
    collected = bytearray()

    def callback(indata, frames_count, time_info, status):  # noqa: ANN001
        if status:
            # 溢出等状态不致命，继续采集
            pass
        collected.extend(bytes(indata))
        if on_level is not None:
            try:
                raw = bytes(indata)
                if raw:
                    peak = int(np.abs(np.frombuffer(raw, dtype=np.int16)).max())
                    on_level(min(1.0, peak / 12000.0))
            except Exception:
                pass

    try:
        stream = sd.InputStream(
            device=dev_index,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=1024,
            callback=callback,
        )
    except Exception as exc:
        raise MicrophoneUnavailable(f"打开录音设备失败：{exc}") from exc

    deadline = time.monotonic() + max_seconds
    started = time.monotonic()
    if on_start:
        try:
            on_start()
        except Exception:
            pass

    with stream:
        stream.start()
        while time.monotonic() < deadline:
            if stop_flag is not None and stop_flag.is_set():
                break
            time.sleep(0.02)
            if on_progress and int((time.monotonic() - started) * 10) % 10 == 0:
                try:
                    on_progress(time.monotonic() - started)
                except Exception:
                    pass
        stream.stop()

    data = bytes(collected)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(BITS_PER_SAMPLE // 8)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(data)

    return out_path


def duration_of(wav_path: str | Path) -> float:
    """读取 wav 时长（秒）。"""
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate() or SAMPLE_RATE
    return frames / float(rate)


def peak_amplitude(wav_path: str | Path) -> int:
    """返回 wav 的峰值振幅；0 表示全静音。"""
    with wave.open(str(wav_path), "rb") as wf:
        data = wf.readframes(wf.getnframes())
    if not data:
        return 0
    samples = struct.unpack(f"<{len(data) // 2}h", data[: (len(data) // 2) * 2])
    return max((abs(s) for s in samples), default=0)


def has_speech(wav_path: str | Path, threshold: int = 300) -> bool:
    """粗判 wav 是否含有有效声音（峰值超过阈值）。"""
    return peak_amplitude(wav_path) >= threshold


# --------------------------------------------------------------------------
# winmm 路径（兜底，纯 ctypes 无依赖）
# --------------------------------------------------------------------------

class WAVEHDR(ctypes.Structure):
    # x64 下 dwUser / reserved 是 DWORD_PTR（8 字节）
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wt.DWORD),
        ("dwBytesRecorded", wt.DWORD),
        ("dwUser", ctypes.c_ulonglong),
        ("dwFlags", wt.DWORD),
        ("dwLoops", wt.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_ulonglong),
    ]


def _winmm():
    lib = ctypes.WinDLL("winmm")
    lib.waveInOpen.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    lib.waveInOpen.restype = ctypes.c_uint
    lib.waveInPrepareHeader.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(WAVEHDR),
        ctypes.c_ulong,
    ]
    lib.waveInPrepareHeader.restype = ctypes.c_uint
    lib.waveInStart.argtypes = [ctypes.c_void_p]
    lib.waveInStart.restype = ctypes.c_uint
    lib.waveInStop.argtypes = [ctypes.c_void_p]
    lib.waveInStop.restype = ctypes.c_uint
    lib.waveInUnprepareHeader.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(WAVEHDR),
        ctypes.c_ulong,
    ]
    lib.waveInUnprepareHeader.restype = ctypes.c_uint
    lib.waveInClose.argtypes = [ctypes.c_void_p]
    lib.waveInClose.restype = ctypes.c_uint
    lib.waveInGetNumDevs.argtypes = []
    lib.waveInGetNumDevs.restype = ctypes.c_uint
    lib.waveInGetDevCapsW.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
    lib.waveInGetDevCapsW.restype = ctypes.c_uint
    return lib


def _winmm_devices() -> list[str]:
    names: list[str] = []

    class CAPS(ctypes.Structure):
        _fields_ = [
            ("wProduct", wt.WORD),
            ("wVersion", wt.WORD),
            ("wMid", ctypes.c_ulong),
            ("wPid", ctypes.c_ulong),
            ("vDriverVersion", ctypes.c_ulong),
            ("szPname", wt.WCHAR * 32),
            ("dwFormats", ctypes.c_ulong),
            ("wChannels", wt.WORD),
            ("wReserved1", wt.WORD),
        ]

    try:
        lib = _winmm()
        for index in range(lib.waveInGetNumDevs()):
            caps = CAPS()
            if lib.waveInGetDevCapsW(
                ctypes.c_uint(index), ctypes.byref(caps), ctypes.sizeof(caps)
            ) == 0:
                names.append(caps.szPname.strip())
    except Exception:
        pass
    return names


def record_winmm(
    path: str | Path,
    max_seconds: float = 60.0,
    stop_flag: StopFlag | None = None,
) -> Path:
    """winmm 录音（兜底路径）。本机实测 prepareHeader 返回 11，故默认不启用。"""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lib = _winmm()
    wfx = ctypes.create_string_buffer(
        struct.pack(
            "<HHIIHHH",
            1,
            CHANNELS,
            SAMPLE_RATE,
            BYTES_PER_SEC,
            BLOCK_ALIGN,
            BITS_PER_SAMPLE,
            0,
        ),
        18,
    )

    handle = ctypes.c_void_p()
    rc = lib.waveInOpen(
        ctypes.byref(handle),
        ctypes.c_int(WAVE_MAPPER),
        ctypes.cast(wfx, ctypes.c_void_p),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_void_p(0),
    )
    if rc != MMSYSERR_NOERROR:
        raise MicrophoneUnavailable(f"winmm 打开麦克风失败，错误码 {rc}")

    buffer = ctypes.create_string_buffer(4096 * BLOCK_ALIGN)
    header = WAVEHDR()
    header.lpData = ctypes.cast(buffer, ctypes.c_void_p)
    header.dwBufferLength = 4096 * BLOCK_ALIGN

    chunks: list[bytes] = []
    try:
        rc = lib.waveInPrepareHeader(handle, ctypes.byref(header), 0)
        if rc != MMSYSERR_NOERROR:
            raise MicrophoneUnavailable(f"winmm prepareHeader 失败，错误码 {rc}")
        lib.waveInStart(handle)

        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            if stop_flag is not None and stop_flag.is_set():
                break
            time.sleep(0.02)
            recorded = header.dwBytesRecorded
            if recorded > 0:
                chunks.append(buffer.raw[:recorded])
                header.dwBytesRecorded = 0
            if header.dwFlags & WHDR_DONE:
                header.dwFlags = 0
                lib.waveInPrepareHeader(handle, ctypes.byref(header), 0)
                lib.waveInStart(handle)

        lib.waveInStop(handle)
    finally:
        lib.waveInUnprepareHeader(handle, ctypes.byref(header), 0)
        lib.waveInClose(handle)

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(BITS_PER_SAMPLE // 8)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(chunks))

    return out_path
