"""离线全链路验证：模拟会话文件，验证监听、文本解析和 final 提取。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import wb_acp
import wb_session

MARKER = "VOICE_E2E_PROBE"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "session.jsonl"
        reply_record = {
            "type": "message",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": f"<transcript>{MARKER}</transcript><final>{MARKER}</final>",
            }],
        }
        copy.write_text("", encoding="utf-8")

        watcher = wb_session.RecentSessionWatcher.create()
        watcher._offsets = {copy: 0}
        watcher._buffers = {copy: ""}

        with copy.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(reply_record, ensure_ascii=False) + "\n")

        got = watcher.poll()
        print(f"[1] 轮询到 {len(got)} 条消息")
        if not got:
            print("[FAIL] 监听器没读到任何消息")
            return 1

        text = "\n".join(got)
        final = wb_acp.extract_tag(text, "final")
        print(f"[2] 提取 <final>: {final!r}")
        if final != MARKER:
            print(f"[FAIL] final 不等于 {MARKER}")
            return 1

        usage_record = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "<final>结论内容</final>"}],
            "message": {"usage": {"input_tokens": 1, "output_tokens": 2}},
        }
        if wb_acp.extract_tag(wb_session._extract_text(usage_record), "final") != "结论内容":
            print("[FAIL] usage 元数据场景没通过")
            return 1
        print("[3] usage 元数据场景: OK")

    print("\n全链路离线验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
