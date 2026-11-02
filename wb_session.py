"""监听 WorkBuddy 会话文件，提取最新回复文本。

WorkBuddy 桌面版把每轮对话写进 ~/.workbuddy/projects/<编码后的目录>/<sessionId>.jsonl。
当助手完成回复后，最后一条 assistant 消息即为本次结果。

文件是追加写入的，因此用「文件大小 + 已读偏移」做增量读取最省资源。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

PROJECTS_DIR = Path.home() / ".workbuddy" / "projects"


def encode_cwd(cwd: str) -> str:
    """把工作目录编码成 projects 下的目录名。

    规则：盘符冒号与路径分隔符统一换成 '-'，首尾不加分隔符。
    例：C:\\Users\\PC\\WorkBuddy\\2026-10-28-01-23-52
        -> c-Users-PC-WorkBuddy-2026-10-28-01-23-52
    """
    normalized = cwd.replace("\\", "/").replace(":", "")
    parts = [p for p in normalized.split("/") if p]
    return "-".join(parts).lower()


def find_session_file(cwd: str, session_id: str | None = None) -> Path | None:
    """定位某个工作目录（或会话）对应的 jsonl 文件。"""
    if session_id:
        for base in PROJECTS_DIR.iterdir() if PROJECTS_DIR.is_dir() else []:
            candidate = base / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate

    encoded = encode_cwd(cwd)
    directory = PROJECTS_DIR / encoded
    if not directory.is_dir():
        return None

    files = sorted(directory.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


TEXT_BLOCK_TYPES = ("text", "output_text", "input_text")
NON_TEXT_BLOCK_TYPES = ("tool_use", "thinking", "input_text", "tool_result")


def _content_of(record: dict):
    """取记录里的内容容器。

    注意：最终回复记录同时带有顶层 `content` 和一个 `message` 字段，
    而 `message` 里装的是 usage 计费元数据（{"usage": {...}}），**不是**内容。
    所以顶层 content 优先，只有顶层缺失时才回落到 record["message"]["content"]。
    """
    content = record.get("content")
    if content is not None:
        return content
    message = record.get("message")
    if isinstance(message, dict):
        return message.get("content")
    return None


def _role_of(record: dict):
    """取记录的角色。"""
    role = record.get("role")
    if role is None:
        message = record.get("message")
        if isinstance(message, dict):
            role = message.get("role")
    return role


def _extract_text(record: dict) -> str:
    """从一条 jsonl 记录里抽出文本内容。

    新版桌面版把 content 直接放在记录顶层，块类型是 output_text / input_text；
    旧版是 record["message"]["content"]，块类型是 text。两种都兼容。
    """
    content = _content_of(record)

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in TEXT_BLOCK_TYPES:
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def is_assistant_text(record: dict) -> bool:
    """判断记录是否为助手的文字消息（排除工具调用与用户消息）。"""
    if record.get("type") not in ("message", "assistant"):
        return False

    role = _role_of(record)
    content = _content_of(record)

    if role not in (None, "assistant"):
        return False

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in NON_TEXT_BLOCK_TYPES:
                return False

    return bool(_extract_text(record).strip())


@dataclass
class SessionWatcher:
    """增量监听会话文件，等待新的助手回复。"""

    path: Path
    offset: int = 0
    _buffer: str = ""

    @classmethod
    def create(cls, cwd: str, session_id: str | None = None) -> "SessionWatcher | None":
        target = find_session_file(cwd, session_id)
        if target is None:
            return None
        # 从文件末尾开始监听，只看新增内容
        return cls(path=target, offset=target.stat().st_size)

    def poll(self) -> list[str]:
        """读取自上次调用以来新增的助手消息文本。"""
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size <= self.offset:
            return []

        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()

        self._buffer += chunk
        # 只处理完整的行
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()

        messages: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if is_assistant_text(record):
                text = _extract_text(record).strip()
                if text:
                    messages.append(text)
        return messages

    def wait_for_reply(self, timeout: float = 180.0, interval: float = 1.0) -> str:
        """等待一条新的助手回复。超时返回空串。"""
        deadline = time.monotonic() + timeout
        collected: list[str] = []
        while time.monotonic() < deadline:
            collected.extend(self.poll())
            if collected:
                time.sleep(interval)  # 等后续片段
                collected.extend(self.poll())
                return "\n".join(collected)
            time.sleep(interval)
        return "\n".join(collected)


def recent_session_files(count: int = 4) -> list[Path]:
    """最近修改过的会话文件（按时间倒序）。"""
    if not PROJECTS_DIR.is_dir():
        return []
    files = sorted(
        PROJECTS_DIR.rglob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return files[:count]


class RecentSessionWatcher:
    """同时盯住最近活跃的几个会话文件。

    语音是注入到**界面当前打开的那个任务**里，回复落到哪个 jsonl 并不确定
    （取决于用户当时在看哪个任务），所以不去猜具体哪个文件，而是盯住最近
    改动过的几个，谁先出现新的助手消息就用谁。
    """

    def __init__(self, count: int = 4) -> None:
        self.count = count
        self._offsets: dict[Path, int] = {}
        self._buffers: dict[Path, str] = {}

    @classmethod
    def create(cls, count: int = 4) -> "RecentSessionWatcher":
        watcher = cls(count)
        watcher.refresh()
        return watcher

    def refresh(self) -> None:
        """把新出现的会话文件纳入监听（从文件末尾开始）。"""
        for path in recent_session_files(self.count):
            if path not in self._offsets:
                try:
                    self._offsets[path] = path.stat().st_size
                except OSError:
                    continue
                self._buffers[path] = ""

    def poll(self) -> list[str]:
        """读取所有被监听文件里的新增助手消息。"""
        self.refresh()
        messages: list[str] = []
        for path in list(self._offsets.keys()):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = self._offsets[path]
            if size <= offset:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    self._offsets[path] = fh.tell()
            except OSError:
                continue

            buffer = self._buffers.get(path, "") + chunk
            lines = buffer.split("\n")
            self._buffers[path] = lines.pop()

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if is_assistant_text(record):
                    text = _extract_text(record).strip()
                    if text:
                        messages.append(text)
        return messages

    def wait_for_reply(self, timeout: float = 180.0, interval: float = 0.8) -> str:
        """等待一条新的助手回复。超时返回空串。"""
        deadline = time.monotonic() + timeout
        collected: list[str] = []
        while time.monotonic() < deadline:
            collected.extend(self.poll())
            if collected:
                time.sleep(interval)
                collected.extend(self.poll())
                return "\n".join(collected)
            time.sleep(interval)
        return "\n".join(collected)
