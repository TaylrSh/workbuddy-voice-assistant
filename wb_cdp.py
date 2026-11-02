"""通过 Chrome DevTools Protocol 直接操作 WorkBuddy 渲染进程。

这是最可靠的注入方式：完全绕开键盘模拟，直接在页面 DOM 里填值并触发事件。

前提：启动 WorkBuddy 时设置环境变量 WORKBUDDY_REMOTE_DEBUGGING_PORT，
桌面版会据此开启 CDP 端口（见 resources/app.asar 的 applyCliCommandLineSwitches）。

用法：
    python wb_cdp.py            # 列出可用页面
    python wb_cdp.py --inject "文字"   # 注入并发送
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

DEFAULT_PORT = 9333
DEBUG_ENV_KEY = "WORKBUDDY_REMOTE_DEBUGGING_PORT"


def port() -> int:
    """CDP 端口：优先环境变量，其次默认 9333。"""
    raw = os.environ.get(DEBUG_ENV_KEY, "")
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def _get(path: str, timeout: float = 4.0) -> Any:
    url = f"http://127.0.0.1:{port()}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else None


def is_available() -> bool:
    """CDP 是否可用。"""
    try:
        _get("/json/version")
        return True
    except Exception:
        return False


def list_pages() -> list[dict]:
    """列出可调试的页面。"""
    try:
        pages = _get("/json/list") or []
    except Exception:
        return []
    return [
        {
            "id": p.get("id"),
            "title": p.get("title"),
            "url": (p.get("url") or "")[:80],
            "type": p.get("type"),
            "ws": p.get("webSocketDebuggerUrl"),
        }
        for p in pages
        if p.get("type") == "page" and p.get("webSocketDebuggerUrl")
    ]


def pick_page(prefer_title: str | None = None) -> dict | None:
    """挑一个 WorkBuddy 页面。"""
    pages = list_pages()
    if not pages:
        return None
    if prefer_title:
        for page in pages:
            if prefer_title.lower() in (page["title"] or "").lower():
                return page
    for page in pages:
        if "workbuddy" in (page["url"] or "").lower() or "WorkBuddy" in (page["title"] or ""):
            return page
    return pages[0]


# ---------------------------------------------------------------- CDP 调用


class CdpClient:
    """最小 WebSocket 客户端（只实现发送 JSON 文本帧）。"""

    def __init__(self, ws_url: str, timeout: float = 15.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._sock = None
        self._next_id = 0

    def __enter__(self) -> "CdpClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        import base64
        import socket
        import ssl
        from urllib.parse import urlparse

        parsed = urlparse(self.ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

        self._sock = socket.create_connection((host, port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        # 读握手响应
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手失败")
            buffer += chunk
        if b"101" not in buffer.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket 握手被拒绝：{buffer[:120]!r}")

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # --- 帧编解码 ---

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        header = bytearray()
        header.append(0x80 | opcode)
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        assert self._sock is not None
        self._sock.sendall(bytes(header) + masked)

    def _recv_exact(self, count: int) -> bytes:
        assert self._sock is not None
        data = b""
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("连接已关闭")
            data += chunk
        return data

    def _recv_frame(self) -> tuple[int, bytes]:
        head = self._recv_exact(2)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exect(8) if False else self._recv_exact(8), "big")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        """调用一个 CDP 方法并等待结果。"""
        import time

        self._next_id += 1
        message_id = self._next_id
        self._send_frame(
            json.dumps(
                {"id": message_id, "method": method, "params": params or {}}
            ).encode("utf-8")
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise ConnectionError("目标关闭了连接")
            if opcode not in (0x1, 0x2):
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method} 失败：{message['error']}")
                return message.get("result", {})
        raise TimeoutError(f"{method} 超时")

    def evaluate(self, expression: str, timeout: float = 20.0) -> Any:
        """在页面里执行 JS，返回结果值。"""
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        details = result.get("exceptionDetails")
        if details:
            raise RuntimeError(f"JS 执行出错：{details.get('text')} {details.get('exception', {}).get('description', '')}")
        return (result.get("result") or {}).get("value")


# ---------------------------------------------------------------- 注入脚本


def _fill_script(text: str) -> str:
    """生成填值脚本。

    WorkBuddy 的输入框是 Slate 编辑器。只改 DOM（execCommand）React 感知不到，
    发送按钮会一直禁用。必须拿到 Slate 的 editor 实例并调用它的
    insertText / insertData，真正写入编辑器状态。

    editor 的取法：沿 React fiber 向上找 props.editor（Slate Editable 的
    props）或 props.value（Slate 组件的 value=editor）。
    """
    payload = json.dumps({"text": text}, ensure_ascii=False)
    return r"""
    (function(payload){
      const text = payload.text;
      const el = document.querySelector('[contenteditable="true"]');
      if (!el) return { ok:false, reason:'找不到输入框' };

      // 1) 沿 fiber 链找 Slate editor
      let editor = null;
      try {
        const fk = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
        if (fk) {
          let fiber = el[fk], guard = 0;
          while (fiber && guard++ < 40) {
            const p = fiber.memoizedProps || {};
            if (p.editor && typeof p.editor.insertText === 'function') {
              editor = p.editor; break;
            }
            const v = p.value;
            if (v && typeof v === 'object' && typeof v.insertText === 'function'
                && Array.isArray(v.children)) {
              editor = v; break;
            }
            fiber = fiber.return;
          }
        }
      } catch (e) { /* 忽略 */ }

      let viaSlate = false;
      let slateErr = '';
      if (editor) {
        // 先用 DOM 清空（可靠），再让 Slate 写新内容
        try {
          el.focus();
          const sel0 = window.getSelection();
          if (sel0 && sel0.rangeCount) {
            const range0 = document.createRange();
            range0.selectNodeContents(el);
            sel0.removeAllRanges(); sel0.addRange(range0);
          }
          document.execCommand('selectAll');
          document.execCommand('delete');
        } catch (e) { slateErr = 'clearDom:' + (e && e.message ? e.message : e); }

        // 写入文本（editor.insertText 会用当前 selection）
        try {
          const last = editor.children[editor.children.length - 1];
          const len = (last.children || []).map(c => (c.text || '')).join('').length;
          editor.apply({
            type: 'set_selection',
            properties: null,
            newProperties: {
              anchor: { path: [editor.children.length - 1, last.children.length - 1], offset: len },
              focus: { path: [editor.children.length - 1, last.children.length - 1], offset: len }
            }
          });
          editor.insertText(text);
          viaSlate = true;
        } catch (e) {
          slateErr += ' insert:' + (e && e.message ? e.message : e);
        }
      }

      if (!viaSlate) {
        el.focus();
        try { document.execCommand('insertText', false, text); } catch (e) {}
      }

      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text }));

      const domText = (el.value !== undefined ? el.value : el.textContent) || '';
      let slateLen = null;
      if (editor && Array.isArray(editor.children)) {
        slateLen = editor.children.map(n =>
          (n.children || []).map(c => (c.text || '')).join('')).join('').length;
      }
      return { ok:true, tag: el.tagName, viaSlate, slateErr,
               value: domText.slice(0,100), length: domText.length,
               slateLen };
    })(%s)
    """ % payload


# 触发发送：优先调用组件的 onSend（最可靠），其次点「发送」按钮，最后模拟回车
_FIND_SEND_JS = r"""
(()=>{
  const el=document.querySelector('[contenteditable="true"]');
  if(!el) return {err:'no input'};
  const fk=Object.keys(el).find(k=>k.startsWith('__reactFiber$'));
  let f=fk? el[fk] : null, guard=0, hooks=null;
  while(f && guard++<40){
    const p=f.memoizedProps||{};
    if(typeof p.onSend==='function'){
      hooks={hasOnSend:true, hasBefore: typeof p.onBeforeSend==='function',
             disabled: p.disabled, onSendLen: p.onSend.length,
             beforeLen: p.onBeforeSend? p.onBeforeSend.length : -1};
      break;
    }
    f=f.return;
  }
  // 顺带找发送按钮
  let btn=null;
  for(const b of document.querySelectorAll('button')){
    const a=(b.getAttribute('aria-label')||'');
    if(a.includes('发送')){
      const r=b.getBoundingClientRect();
      btn={x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2),
           disabled:b.disabled||b.getAttribute('aria-disabled')==='true'};
      break;
    }
  }
  return {hooks, btn};
})()
"""


# 判断输入框是否已清空（发出去后 Slate 内容为空）。
# 注意：placeholder 是真实 DOM 节点，不能用 textContent 判断，要看 Slate 的 children。
_IS_EMPTY_JS = r"""
(()=>{
  const el=document.querySelector('[contenteditable="true"]');
  if(!el) return true;
  const fk=Object.keys(el).find(k=>k.startsWith('__reactFiber$'));
  let f=fk? el[fk] : null, guard=0;
  while(f && guard++<40){
    const p=f.memoizedProps||{};
    const ed = p.editor || (p.value && typeof p.value.insertText==='function' ? p.value : null);
    if(ed && Array.isArray(ed.children)){
      const txt = ed.children.map(n=>(n.children||[]).map(c=>(c.text||'')).join('')).join('');
      if(txt.length===0) return true;
    }
    f=f.return;
  }
  // 退化：DOM 里若只剩 placeholder 也算空
  const t=(el.textContent||'').trim();
  return t==='' || t.indexOf('帮你做些什么')>=0;
})()
"""


# 忙闲状态：WorkBuddy 跑任务时「发送」按钮会变成「停止」，
# 所以「有没有发送按钮」比「按钮是否禁用」更能反映能不能发消息。
_STATUS_JS = r"""
(()=>{
  let send=null, stop=null;
  for(const b of document.querySelectorAll('button')){
    const a=(b.getAttribute('aria-label')||'');
    if(a.includes('停止')||a.includes('中断')) stop=a;
    else if(a.includes('发送')) send={disabled: b.disabled||b.getAttribute('aria-disabled')==='true'};
  }
  return {hasSend: !!send, sendDisabled: send? send.disabled : null, busy: !!stop && !send};
})()
"""


def composer_status(page_title: str | None = None) -> dict:
    """返回输入区状态：{'hasSend': bool, 'busy': bool}。

    busy=True 表示 WorkBuddy 正在跑任务，输入框不接受新消息。
    """
    page = pick_page(page_title)
    if page is None:
        return {"hasSend": False, "busy": False, "error": "no page"}
    try:
        with CdpClient(page["ws"]) as client:
            result = client.evaluate(_STATUS_JS)
    except Exception as exc:
        return {"hasSend": False, "busy": False, "error": str(exc)}
    if not isinstance(result, dict):
        return {"hasSend": False, "busy": False}
    return result


def is_ready(page_title: str | None = None) -> bool:
    """WorkBuddy 是否空闲、可以接收新消息。"""
    status = composer_status(page_title)
    if status.get("error"):
        return False
    return not status.get("busy")


def _trigger_send(client: CdpClient, text: str) -> str:
    """触发发送，返回使用的通道名；发送未生效时返回空串。

    关键：填值后 React 需要重渲染，「发送」按钮才会从 disabled 变为可用。
    所以要轮询等待按钮启用，再点击。
    """
    deadline = time.monotonic() + 4.0
    button = None
    while time.monotonic() < deadline:
        info = client.evaluate(_FIND_SEND_JS)
        if isinstance(info, dict):
            button = info.get("btn")
            if button and not button.get("disabled"):
                break
        time.sleep(0.25)

    # 1) 点击「发送」按钮
    if button and not button.get("disabled"):
        _click_at(client, int(button["x"]), int(button["y"]))
        time.sleep(0.9)
        if client.evaluate(_IS_EMPTY_JS) is True:
            return "button"
        time.sleep(0.5)

    # 2) 调用组件的 onSend
    script = r"""
    (function(payload){
      const el=document.querySelector('[contenteditable="true"]');
      if(!el) return 'no input';
      const fk=Object.keys(el).find(k=>k.startsWith('__reactFiber$'));
      let f=fk? el[fk] : null, guard=0;
      while(f && guard++<40){
        const p=f.memoizedProps||{};
        if(typeof p.onSend==='function'){
          try {
            if(typeof p.onBeforeSend==='function'){
              const ok = p.onBeforeSend(payload.text);
              if(ok === false) return 'before-rejected';
            }
            p.onSend(payload.text);
            return 'onSend';
          } catch(e){ return 'err:'+(e&&e.message?e.message:e); }
        }
        f=f.return;
      }
      return 'no onSend';
    })(%s)
    """ % json.dumps({"text": text}, ensure_ascii=False)
    result = client.evaluate(script)
    if isinstance(result, str) and result.startswith("onSend"):
        time.sleep(0.6)
        if client.evaluate(_IS_EMPTY_JS) is True:
            return "onSend"

    # 3) 模拟回车
    _press_enter(client)
    time.sleep(0.8)
    if client.evaluate(_IS_EMPTY_JS) is True:
        return "enter"
    # 输入框里的文字还在，说明消息没发出去
    return ""


def _click_at(client: CdpClient, x: int, y: int) -> None:
    """用 CDP 真实鼠标事件点击坐标（moved → pressed → released）。"""
    try:
        client.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
            timeout=8.0,
        )
    except Exception:
        pass
    for event_type in ("mousePressed", "mouseReleased"):
        try:
            client.call(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                    "buttons": 1 if event_type == "mousePressed" else 0,
                },
                timeout=8.0,
            )
        except Exception:
            pass


def _send_via_button(client: CdpClient) -> bool:
    """填值后点击「发送」按钮。返回是否成功点击。

    先用 CDP 真实鼠标事件点击；若无反应则退回 DOM 原生 click()
    （React 的 onClick 委托在 document 上，DOM click 同样能触发）。
    """
    button = client.evaluate(_FIND_SEND_JS)
    if not isinstance(button, dict) or button.get("disabled"):
        return False

    _click_at(client, int(button["x"]), int(button["y"]))
    time.sleep(0.4)

    # 校验：输入框是否已清空（发出去了）
    remaining = client.evaluate(
        "(()=>{const e=document.querySelector('[contenteditable=\"true\"]');"
        "return e?(e.textContent||'').trim().length:0;})()"
    )
    if isinstance(remaining, (int, float)) and remaining == 0:
        return True

    # 退回 DOM 原生点击
    client.evaluate(
        "(()=>{const cands=[...document.querySelectorAll('button')];"
        "for(const el of cands){const a=(el.getAttribute('aria-label')||'');"
        "if(a.includes('发送')&&!el.disabled){el.click();return 'dom-click';}}"
        "return 'nf';})()"
    )
    time.sleep(0.4)
    remaining = client.evaluate(
        "(()=>{const e=document.querySelector('[contenteditable=\"true\"]');"
        "return e?(e.textContent||'').trim().length:0;})()"
    )
    return isinstance(remaining, (int, float)) and remaining == 0


def inject_text(text: str, send: bool = True, page_title: str | None = None) -> dict:
    """在 WorkBuddy 页面里填入文本并（可选）发送。

    填值走 execCommand（React 兼容），发送优先点击「发送」按钮，
    按钮不可用时回退到模拟回车。
    """
    page = pick_page(page_title)
    if page is None:
        return {"ok": False, "message": "没有找到可调试的页面（CDP 未开启？）"}

    with CdpClient(page["ws"]) as client:
        result = client.evaluate(_fill_script(text))
        if not (isinstance(result, dict) and result.get("ok")):
            return {"ok": False, "message": f"填值失败：{result}"}

        if not result.get("slateLen") and not result.get("length"):
            return {"ok": False, "message": "填值后输入框为空", "detail": result}

        channel = None

        if send:
            # 触发发送：onSend -> 按钮 -> 回车
            channel = _trigger_send(client, text)
            time.sleep(0.8)
            if not channel:
                # 输入框里的文字还在 —— 消息没发出去
                return {
                    "ok": False,
                    "message": "发送未生效（WorkBuddy 可能正在忙）",
                    "detail": result,
                    "channel": None,
                }

    return {
        "ok": True,
        "message": "已注入" + ("并发送" if send else ""),
        "detail": result,
        "channel": channel if send else None,
    }


def _press_enter(client: CdpClient) -> None:
    """用 CDP 原生按键事件发送回车。"""
    for event_type in ("rawKeyDown", "char", "keyUp"):
        params = {
            "type": event_type,
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
            "text": "\r" if event_type == "char" else "",
            "unmodifiedText": "\r" if event_type == "char" else "",
        }
        try:
            client.call("Input.dispatchKeyEvent", params, timeout=8.0)
        except Exception:
            pass


def read_input(page_title: str | None = None) -> str:
    """读取当前输入框内容（调试用）。"""
    page = pick_page(page_title)
    if page is None:
        return ""
    js = r"""
    (function(){
      const cands = Array.from(document.querySelectorAll(
        '[contenteditable="true"], textarea, input[type="text"]'
      ));
      for (const c of cands) {
        const r = c.getBoundingClientRect();
        if (r.width > 200 && r.height > 20) return (c.value || c.textContent || '').slice(0,200);
      }
      return '';
    })()
    """
    with CdpClient(page["ws"]) as client:
        value = client.evaluate(js)
    return value if isinstance(value, str) else ""


if __name__ == "__main__":
    import sys

    if not is_available():
        print(f"CDP 未开启。请用环境变量 {DEBUG_ENV_KEY}=9333 启动 WorkBuddy。")
        print("或者运行: python wb_cdp.py --hint")
        sys.exit(1)

    args = sys.argv[1:]
    if not args:
        for page in list_pages():
            print(f"  [{page['type']}] {page['title'][:50]} | {page['url'][:60]}")
    elif args[0] == "--inject" and len(args) >= 2:
        print(inject_text(args[1], send=("--no-send" not in args)))
    elif args[0] == "--read":
        print(repr(read_input()))
