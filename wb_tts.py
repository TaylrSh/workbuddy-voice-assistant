"""语音播报 —— 双引擎：Windows 本地 SAPI（pyttsx3）+ 微软在线 TTS（edge-tts）。

选型说明：
  - pyttsx3：走系统 SAPI，离线可用、零延迟。中文音质一般，但作为兜底永远可靠
  - edge-tts：微软 Edge 浏览器的在线朗读接口，中文自然度好得多，需联网

优先用 edge-tts；失败或未安装时自动回落到 pyttsx3。

播报实现：合成出 WAV/MP3 后用 winsound 播放（Windows 自带，无需播放器）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import wave
from pathlib import Path

# 常用中文音色
VOICE_ZH_CN = "zh-CN-XiaoxiaoNeural"
VOICE_ZH_CN_MALE = "zh-CN-YunxiNeural"

# 朗读前先过滤的标记，避免把 Markdown 念出来
_STRIP_PATTERNS = (
    "```",
    "**",
    "##",
    "#",
    "`",
)


def clean_for_speech(text: str) -> str:
    """把模型回复清洗成适合朗读的纯文本。"""
    if not text:
        return ""

    out = text
    for pattern in _STRIP_PATTERNS:
        out = out.replace(pattern, " ")

    # 去掉链接
    import re

    out = re.sub(r"https?://\S+", " ", out)
    # 去掉行内代码的反引号内容标记
    out = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", out)
    # 表格分隔线
    out = re.sub(r"^\s*\|?[\s\-:|]+\|[\s\-:|]*$", " ", out, flags=re.M)
    # 连续空白压缩
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _play_wav(path: str | Path) -> None:
    """用 winsound 播放 wav。"""
    import winsound

    winsound.PlaySound(str(path), winsound.SND_FILENAME)


def _play_with_sapi(text: str, rate: int = 0, volume: int = 100) -> bool:
    """用系统 SAPI 朗读（离线）。返回是否成功。"""
    try:
        import pyttsx3
    except ImportError:
        return False

    try:
        engine = pyttsx3.init()
        # 优先挑中文音色
        for voice in engine.getProperty("voices"):
            try:
                lang = (voice.languages or [""])[0]
            except Exception:
                lang = ""
            if str(lang).startswith("zh") or "Huihui" in voice.name or "Yaoyao" in voice.name:
                engine.setProperty("voice", voice.id)
                break

        if rate:
            engine.setProperty("rate", rate)
        engine.setProperty("volume", volume)
        engine.say(text)
        engine.runAndWait()
        return True
    except Exception:
        return False


async def _edge_synth(text: str, voice: str, out_path: Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))


def _edge_synth_sync(text: str, voice: str, out_path: Path) -> bool:
    """同步调用 edge-tts。返回是否成功。"""
    try:
        asyncio.run(_edge_synth(text, voice, out_path))
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:
        return False


def speak(
    text: str,
    voice: str = VOICE_ZH_CN,
    prefer_online: bool = True,
    rate: int = 0,
    volume: int = 100,
) -> bool:
    """朗读文本。返回是否成功播报。

    prefer_online=True 时优先用 edge-tts（音质好），失败回落 SAPI。
    """
    cleaned = clean_for_speech(text)
    if not cleaned:
        return False

    if prefer_online:
        tmp = Path(tempfile.gettempdir()) / f"wb_tts_{os.getpid()}_{threading.get_ident()}.mp3"
        try:
            if _edge_synth_sync(cleaned, voice, tmp):
                try:
                    import winsound

                    winsound.PlaySound(str(tmp), winsound.SND_FILENAME)
                    return True
                except Exception:
                    # 播放失败也视作合成成功，降级到 SAPI 兜底
                    pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    return _play_with_sapi(cleaned, rate=rate, volume=volume)


def list_voices() -> list[str]:
    """列出 edge-tts 可用的中文音色。

    edge-tts 7.x 的 list_voices 是协程，返回普通 list。
    """
    try:
        import asyncio

        import edge_tts

        async def _fetch() -> list[str]:
            voices = edge_tts.list_voices()
            if hasattr(voices, "__await__"):
                voices = await voices
            result = []
            for v in voices or []:
                if str(v.get("Locale", "")).startswith("zh-CN"):
                    result.append(f"{v.get('ShortName')} | {v.get('Gender', '')}")
            return result

        return asyncio.run(_fetch())
    except Exception:
        return []
