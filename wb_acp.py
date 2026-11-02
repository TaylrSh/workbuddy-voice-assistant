"""WorkBuddy 会话发现与 ACP 客户端。

桌面版 WorkBuddy 为每个会话启动一个 codebuddy CLI 进程，监听 127.0.0.1 的随机端口。
进程元数据记录在 ~/.workbuddy/sessions/<pid>.json，含 url / sessionId / cwd。

本模块负责：
  1. 发现当前活跃会话（优先取最近心跳的那个）
  2. 通过 ACP 协议把文本注入该会话
  3. 读取流式回复

ACP 鉴权要点（实测）：
  - POST <url>/api/v1/acp/connect      -> 返回 {connectionId, sessionToken}
  - POST <url>/api/v1/acp              -> 需带 acp-connection-id / acp-session-token
                                          以及 Accept: application/json, text/event-stream
  - session/new                        -> 返回的 sessionId 即桌面版当前会话
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SESSIONS_DIR = Path.home() / ".workbuddy" / "sessions"


@dataclass
class WorkBuddySession:
    """一个运行中的 WorkBuddy 会话。"""

    pid: int
    session_id: str
    url: str
    cwd: str
    kind: str = "interactive"
    updated_at: int = 0
    candidates: list[str] = field(default_factory=list)

    @property
    def acp_url(self) -> str:
        return f"{self.url}/api/v1/acp"

    @property
    def connect_url(self) -> str:
        return f"{self.url}/api/v1/acp/connect"


@dataclass
class AcpConnection:
    """ACP 连接凭据。"""

    base_url: str
    connection_id: str
    session_token: str
    _next_id: int = field(default=1)

    @property
    def acp_url(self) -> str:
        return f"{self.base_url}/api/v1/acp"

    @property
    def connect_url(self) -> str:
        return f"{self.base_url}/api/v1/acp/connect"

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "acp-connection-id": self.connection_id,
            "acp-session-token": self.session_token,
        }


def discover_sessions(sessions_dir: Path | None = None) -> list[WorkBuddySession]:
    """扫描 ~/.workbuddy/sessions/，返回所有存活的会话，按活跃度倒序。

    新版桌面版的会话文件不再记录 url，需要按 pid 反查监听端口。
    """
    directory = sessions_dir or SESSIONS_DIR
    if not directory.is_dir():
        return []

    found: list[WorkBuddySession] = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        session_id = data.get("sessionId")
        if not session_id:
            continue
        kind = data.get("kind") or "interactive"
        if kind == "prewarm":
            continue

        pid = int(data.get("pid") or 0)
        url = data.get("url") or data.get("endpoint")
        candidates = [url.rstrip("/")] if url else []
        # 新版会话文件不带 url，需要按 pid 反查监听端口
        for port in _ports_by_pid(pid):
            candidate = f"http://127.0.0.1:{port}"
            if candidate not in candidates:
                candidates.append(candidate)

        found.append(
            WorkBuddySession(
                pid=pid,
                session_id=session_id,
                url=(candidates[0] if candidates else "").rstrip("/"),
                candidates=candidates,
                cwd=data.get("cwd") or "",
                kind=kind,
                updated_at=int(data.get("updatedAt") or 0),
            )
        )

    found.sort(key=lambda s: s.updated_at, reverse=True)
    return found


def _ports_by_pid(pid: int) -> list[int]:
    """列出某进程监听的所有本地端口。"""
    if not pid:
        return []
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        out = result.stdout or ""
    except Exception:
        return []

    ports: list[int] = []
    for line in out.splitlines():
        if not line.strip().endswith(str(pid)):
            continue
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        local = parts[1]
        if ":" not in local:
            continue
        host, _, port = local.rpartition(":")
        if host in ("127.0.0.1", "0.0.0.0", "::1", "[::1]"):
            try:
                value = int(port)
            except ValueError:
                continue
            if value not in ports:
                ports.append(value)
    return ports


def pick_session(
    prefer_cwd: str | None = None,
    sessions_dir: Path | None = None,
    probe: bool = True,
) -> WorkBuddySession | None:
    """选出一个目标会话。

    优先匹配 prefer_cwd；否则取最近活跃的。仅当 probe=True 时才实际探测端口存活。
    """
    sessions = discover_sessions(sessions_dir)
    if not sessions:
        return None

    if prefer_cwd:
        normalized = os.path.normcase(os.path.normpath(prefer_cwd))
        for session in sessions:
            if os.path.normcase(os.path.normpath(session.cwd)) == normalized:
                return session

    if not probe:
        return sessions[0]

    # 探测：挑第一个能真正建立 ACP 连接的会话
    for session in sessions:
        if _is_alive(session):
            return session
    return None


def _is_alive(session: WorkBuddySession) -> bool:
    """探测会话是否可用：逐个候选端口做 TCP 连接测试。"""
    import socket
    from urllib.parse import urlparse

    candidates = session.candidates or [session.url]
    for base in candidates:
        if not base:
            continue
        parsed = urlparse(base)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            continue
    return False


def connect(session: WorkBuddySession, timeout: float = 15.0) -> AcpConnection:
    """建立 ACP 连接，返回带凭据的连接对象。

    一个 CLI 进程可能监听多个端口，这里逐个尝试，找出真正提供
    ACP 服务的那个。
    """
    candidates = session.candidates or [session.url]
    last_error: Exception | None = None

    for base in candidates:
        if not base:
            continue
        try:
            req = urllib.request.Request(
                f"{base}/api/v1/acp/connect",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "acp-connection-id": f"voice-{os.getpid()}-{int(time.time() * 1000)}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            continue

        session.url = base
        return AcpConnection(
            base_url=base,
            connection_id=payload["connectionId"],
            session_token=payload["sessionToken"],
        )

    raise RuntimeError(f"无法连接会话的 ACP 端点：{last_error}")


def _read_sse(resp: Any) -> Iterator[dict[str, Any]]:
    """从 SSE 响应中逐条产出 JSON 消息。"""
    event_name: str | None = None
    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        if not line:
            event_name = None
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


def _post(conn: AcpConnection, payload: dict[str, Any], timeout: float = 300.0) -> Any:
    req = urllib.request.Request(
        conn.acp_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=conn.headers(),
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def initialize(conn: AcpConnection, timeout: float = 30.0) -> dict[str, Any]:
    """完成 ACP 协议握手。"""
    with _post(
        conn,
        {
            "jsonrpc": "2.0",
            "id": conn._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True}
                },
            },
        },
        timeout=timeout,
    ) as resp:
        for message in _read_sse(resp):
            if "result" in message:
                return message["result"]
    raise RuntimeError("ACP initialize 未返回结果")


def new_session(conn: AcpConnection, cwd: str, timeout: float = 60.0) -> str:
    """创建/附着会话，返回 sessionId。

    用相同 cwd 再次调用会附着到桌面版已存在的会话，因此拿到的就是当前对话。
    """
    with _post(
        conn,
        {
            "jsonrpc": "2.0",
            "id": conn._next_id,
            "method": "session/new",
            "params": {"cwd": cwd, "mcpServers": []},
        },
        timeout=timeout,
    ) as resp:
        for message in _read_sse(resp):
            if "result" in message:
                return message["result"]["sessionId"]
    raise RuntimeError("session/new 未返回 sessionId")


FINAL_OPEN = "<final>"
FINAL_CLOSE = "</final>"


def send_prompt(
    conn: AcpConnection,
    session_id: str,
    text: str,
    timeout: float = 600.0,
) -> Iterator[str]:
    """向会话发送提示词，逐段产出回复文本。

    注意：会话被压缩过时，模型可能先重放 <cb_summary> 摘要内容，
    这些内容同样以 agent_message_chunk 形式出现。因此本函数额外提供
    extract_final() 用于提取被 <final> 标记包裹的结论段。
    """
    req = urllib.request.Request(
        conn.acp_url,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": conn._next_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            }
        ).encode("utf-8"),
        headers=conn.headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for message in _read_sse(resp):
            params = message.get("params") or {}
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                chunk = update.get("content") or {}
                piece = chunk.get("text")
                if piece:
                    yield piece


def extract_final(text: str) -> str:
    """从回复里提取 <final>…</final> 之间的结论。

    没有标记时退化为：丢弃 <cb_summary>…</cb_summary> 之后的内容。
    """
    if FINAL_OPEN in text and FINAL_CLOSE in text:
        start = text.rfind(FINAL_OPEN)
        end = text.rfind(FINAL_CLOSE)
        if end > start:
            return text[start + len(FINAL_OPEN) : end].strip()

    # 退化路径：去掉压缩摘要块
    if "<cb_summary>" in text and "</cb_summary>" in text:
        tail = text.rsplit("</cb_summary>", 1)[1]
        return tail.strip()

    return text.strip()


def extract_tag(text: str, tag: str) -> str:
    """提取 <tag>…</tag> 之间的内容；没有标记时返回空串。"""
    opener, closer = f"<{tag}>", f"</{tag}>"
    if opener in text and closer in text:
        start = text.rfind(opener)
        end = text.rfind(closer)
        if end > start:
            return text[start + len(opener) : end].strip()
    return ""
