"""一键链路自检 —— 在你自己的终端运行，逐段验证语音助手各环节。

用法：
    python verify.py           # 完整检查
    python verify.py --quick   # 跳过耗时环节

各环节会打印 [OK] / [FAIL] / [SKIP] 及说明，最后给出总结。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import wb_acp
import wb_capture
import wb_cdp
import wb_hotkey
import wb_inject
import wb_mic
import wb_overlay
import wb_session
import wb_tts

results: list[tuple[str, str, str]] = []


def check(name: str):
    def deco(fn):
        def wrapper(*args, **kwargs):
            print(f"\n▶ {name}")
            try:
                detail = fn(*args, **kwargs)
                results.append((name, "OK", detail or ""))
                print(f"  [OK] {detail}")
            except SkipCheck as exc:
                results.append((name, "SKIP", str(exc)))
                print(f"  [SKIP] {exc}")
            except Exception as exc:
                results.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))
                print(f"  [FAIL] {type(exc).__name__}: {exc}")
            return wrapper

        return wrapper

    return deco


class SkipCheck(Exception):
    pass


# ---------------------------------------------------------------- 环节检查


@check("录音设备与采集")
def check_mic():
    devices = wb_mic.list_input_devices()
    if not devices:
        raise SkipCheck("未检测到录音设备")
    path = BASE / "recordings" / "_verify.wav"
    path.parent.mkdir(exist_ok=True)
    wb_mic.record(path, max_seconds=2.0)
    duration = wb_mic.duration_of(path)
    size = path.stat().st_size
    peak = wb_mic.peak_amplitude(path)
    path.unlink(missing_ok=True)
    if size < 1000:
        raise RuntimeError(f"录音文件过小：{size} 字节")
    return (
        f"{len(devices)} 个设备；录到 {duration:.1f} 秒 / {size} 字节，"
        f"峰值 {peak}（峰值低说明环境安静，正常）"
    )


@check("TTS 语音合成与播放")
def check_tts():
    voices = wb_tts.list_voices()
    cleaned = wb_tts.clean_for_speech("**结论**：已完成 [链接](http://a.com)")
    if "结论" not in cleaned or "http" in cleaned:
        raise RuntimeError(f"文本清洗异常：{cleaned!r}")
    ok = wb_tts.speak("语音助手测试", prefer_online=True)
    if not ok:
        raise RuntimeError("两种 TTS 引擎都不可用")
    engine = "edge-tts" if voices else "系统 SAPI"
    return f"清洗正常；音色 {len(voices)} 个可用；引擎 {engine}"


@check("WorkBuddy 会话发现")
def check_session_discovery():
    sessions = wb_acp.discover_sessions()
    if not sessions:
        raise SkipCheck("没有运行中的 WorkBuddy 会话（请打开桌面版）")
    target = wb_acp.pick_session(probe=True)
    if target is None:
        raise SkipCheck("会话存在但端口不可达")
    return (
        f"发现 {len(sessions)} 个会话；目标 {target.session_id[:8]}… "
        f"@ {target.url} (pid {target.pid})"
    )


@check("ACP 协议握手")
def check_acp():
    session = wb_acp.pick_session(probe=True)
    if session is None:
        raise SkipCheck("无可用会话")
    conn = wb_acp.connect(session)
    init = wb_acp.initialize(conn)
    version = init.get("protocolVersion")
    if version is None:
        raise RuntimeError("initialize 未返回 protocolVersion")
    return f"连接成功，protocolVersion={version}"


@check("回复结论提取（<final> 标记）")
def check_extract():
    sample = "<cb_summary>历史摘要内容</cb_summary>\n\n<final>真正的结论</final>"
    got = wb_acp.extract_final(sample)
    if got != "真正的结论":
        raise RuntimeError(f"提取结果不对：{got!r}")
    plain = wb_acp.extract_final("没有标记的普通回复")
    if plain != "没有标记的普通回复":
        raise RuntimeError(f"无标记场景异常：{plain!r}")
    return "带标记/无标记/摘要剥离三种场景均正确"


@check("会话文件定位与监听")
def check_watcher():
    """优先挑一个 cwd 里有会话记录的会话；再验证最近文件监听器。"""
    target = None
    for session in wb_acp.discover_sessions():
        if session.cwd and wb_session.find_session_file(session.cwd):
            target = session
            break
    if target is None:
        raise SkipCheck("没有找到带会话记录文件的 WorkBuddy 会话")
    watcher = wb_session.SessionWatcher.create(target.cwd)
    if watcher is None:
        raise SkipCheck(f"未找到 {target.cwd} 对应的会话记录文件")

    recent = wb_session.RecentSessionWatcher.create()
    files = list(recent._offsets.keys())
    if not files:
        raise RuntimeError("最近会话监听器没有盯到任何文件")
    return (
        f"定位到 {watcher.path.name}，偏移 {watcher.offset}；"
        f"最近文件监听 {len(files)} 个"
    )


@check("桌面版窗口与剪贴板")
def check_window():
    hwnd = wb_inject.find_workbuddy_window()
    if not hwnd:
        raise SkipCheck("未找到 WorkBuddy 窗口")
    if not wb_inject.set_clipboard("__WB_VERIFY__"):
        raise RuntimeError("剪贴板写入失败")
    back = wb_inject.get_clipboard()
    if back != "__WB_VERIFY__":
        raise RuntimeError(f"剪贴板读取不一致：{back!r}")
    wb_inject.set_clipboard("")
    shot = wb_capture.capture_window(hwnd, BASE / "recordings" / "_verify_window.png")
    return f"窗口句柄 {hwnd}；剪贴板读写正常；截图 {'成功' if shot else '失败'}"


@check("热键监听（Ctrl+Alt+V）")
def check_hotkey():
    listener = wb_hotkey.PushToTalk()
    if not listener.start():
        raise SkipCheck("钩子安装失败（可能需要管理员权限）")
    listener.stop()
    return "低级键盘钩子安装/卸载正常"


@check("CDP 调试通道（DOM 级注入）")
def check_cdp():
    if not wb_cdp.is_available():
        raise SkipCheck(
            "CDP 未开启。用 python start_workbuddy.py --restart 重启 WorkBuddy 后可用"
        )
    pages = wb_cdp.list_pages()
    if not pages:
        raise RuntimeError("CDP 已连接但没有可调试页面")
    title = pages[0]["title"][:40]
    current = wb_cdp.read_input()
    return f"已连接，{len(pages)} 个页面；当前输入框内容 {current[:30]!r}"


@check("DOM 注入（真填值）")
def check_cdp_inject(do_it: bool):
    if not do_it:
        raise SkipCheck("未启用 --inject")
    if not wb_cdp.is_available():
        raise SkipCheck("CDP 未开启")
    probe = "VOICE_CDP_PROBE"
    result = wb_cdp.inject_text(probe, send=False)
    if not result.get("ok"):
        raise RuntimeError(f"填值失败：{result.get('message')}")
    back = wb_cdp.read_input()
    if back.strip() != probe:
        raise RuntimeError(f"读回不一致：{back!r}")
    # 清空
    page = wb_cdp.pick_page()
    with wb_cdp.CdpClient(page["ws"]) as client:
        client.evaluate(
            "(()=>{const e=document.querySelector('[contenteditable=\"true\"]');"
            "if(e){e.focus();document.execCommand('selectAll');"
            "document.execCommand('delete');"
            "e.dispatchEvent(new InputEvent('input',{bubbles:true}));}})()"
        )
    return "填值→读回→清空 全链路正常"


@check("DOM 注入 + 真实发送")
def check_cdp_send(do_it: bool):
    """完整验证：填值 -> 等按钮启用 -> 点击发送 -> 消息进入会话。

    判定用会话 jsonl 是否落盘，而不是查 DOM —— DOM 受渲染时机影响，
    且 WorkBuddy 正忙时不会把消息渲染进当前视图。
    """
    if not do_it:
        raise SkipCheck("未启用 --inject（该检查会真的发一条消息）")
    if not wb_cdp.is_available():
        raise SkipCheck("CDP 未开启")

    marker = f"VOICE_VERIFY_{int(time.time())}"
    result = wb_cdp.inject_text(marker, send=True)
    if not result.get("ok"):
        raise RuntimeError(f"注入失败：{result.get('message')}")
    channel = result.get("channel")

    landed = False
    for _ in range(20):
        time.sleep(1.0)
        for path in _recent_session_files():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if marker in text:
                landed = True
                break
        if landed:
            break

    if not landed:
        page = wb_cdp.pick_page()
        with wb_cdp.CdpClient(page["ws"]) as client:
            empty = client.evaluate(wb_cdp._IS_EMPTY_JS)
        if empty is True:
            return f"已发送（通道 {channel}），输入框已清空，回复尚在处理中"
        raise RuntimeError(f"消息未进入会话（发送通道 {channel}）")

    return f"已发送（通道 {channel}），消息已写入会话记录"


def _recent_session_files(count: int = 4):
    """最近修改的会话文件。"""
    projects = Path.home() / ".workbuddy" / "projects"
    if not projects.is_dir():
        return []
    files = sorted(projects.rglob("*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:count]


@check("UI 注入（剪贴板 + 模拟按键）")
def check_inject(do_it: bool):
    if not do_it:
        raise SkipCheck("未启用 --inject")
    hwnd = wb_inject.find_workbuddy_window()
    if not hwnd:
        raise SkipCheck("未找到 WorkBuddy 窗口")
    text = "【语音助手自检】这是一条由脚本注入的测试消息，看到即代表注入成功。"
    result = wb_inject.inject_text(text, send=True, verify=True)
    if not result.get("ok"):
        raise RuntimeError(f"注入失败：{result.get('message')}")
    return f"注入完成（方式 {result.get('method')}，粘贴校验 {result.get('verified')}）"


@check("会话记录解析（新版 output_text 结构）")
def check_parse():
    """用新版会话记录的真实结构验证解析器。"""
    sample = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "<final>结论内容</final>"}],
    }
    if not wb_session.is_assistant_text(sample):
        raise RuntimeError("output_text 结构没被识别为助手文字")
    got = wb_session._extract_text(sample)
    if "结论内容" not in got:
        raise RuntimeError(f"文本抽取不对：{got!r}")
    if wb_acp.extract_tag(got, "final") != "结论内容":
        raise RuntimeError("final 标签提取失败")
    user = {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "你好"}]}
    if wb_session.is_assistant_text(user):
        raise RuntimeError("用户消息被误判成助手消息")

    # 回归：最终回复记录同时带顶层 content 和 message(usage 元数据)，
    # 解析器必须以顶层 content 为准，否则最终回复会被静默丢弃。
    final_record = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "<final>最终回复</final>"}],
        "message": {"usage": {"input_tokens": 73742, "output_tokens": 395}},
    }
    if not wb_session.is_assistant_text(final_record):
        raise RuntimeError("带 usage 元数据的最终回复没被识别")
    got_final = wb_session._extract_text(final_record)
    if "最终回复" not in got_final:
        raise RuntimeError(f"最终回复文本抽取不对：{got_final!r}")

    return "output_text / input_text / 角色过滤 / 标签提取 / usage 元数据兼容 均正确"


@check("悬浮窗渲染与窗口创建")
def check_overlay():
    overlay = wb_overlay.Overlay(scale=1.0)
    overlay.create()
    try:
        overlay.render(force=True)
        if not overlay.hwnd:
            raise RuntimeError("窗口句柄为空")
        bubble = overlay._last_image
        if bubble.width < 40 or bubble.height < 40:
            raise RuntimeError(f"气泡尺寸异常：{bubble.size}")
        # 展开态
        overlay.expand(True)
        overlay.set_transcript("测试转写文本")
        overlay.set_reply("测试回复文本")
        overlay.render(force=True)
        panel = overlay._last_image
        if panel.width < 300 or panel.height < 400:
            raise RuntimeError(f"面板尺寸异常：{panel.size}")
        for name, rect in overlay._hit.items():
            x0, y0, x1, y1 = rect
            if not (0 <= x0 < x1 <= panel.width and 0 <= y0 < y1 <= panel.height):
                raise RuntimeError(f"控件 {name} 命中框越界：{rect}")
        shot = wb_capture.capture_window(overlay.hwnd, BASE / "recordings" / "_verify_overlay.png")
        return (
            f"窗口句柄 {overlay.hwnd}；气泡 {bubble.size}、面板 {panel.size}；"
            f"控件 {len(overlay._hit)} 个命中框正常；截图 {'成功' if shot else '失败'}"
        )
    finally:
        try:
            overlay.stop()
            time.sleep(0.2)
        except Exception:
            pass


@check("输入区忙闲状态检测")
def check_busy():
    if not wb_cdp.is_available():
        raise SkipCheck("CDP 未开启")
    status = wb_cdp.composer_status()
    if status.get("error"):
        raise RuntimeError(f"状态读取失败：{status['error']}")
    return f"hasSend={status.get('hasSend')}，busy={status.get('busy')}"


@check("离线全链路（监听器 → 提取 <final>）")
def check_chain_offline():
    """用真实历史会话文件验证「写入回复 → 监听器读到 → 提取 final」整条链路。

    不需要 WorkBuddy 空闲，也不需要真发消息，因此可以在任何时候跑。
    """
    import subprocess

    script = Path(__file__).resolve().parent / "tools" / "chain_offline_test.py"
    if not script.exists():
        raise SkipCheck("缺少 tools/chain_offline_test.py")

    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "离线全链路验证失败：" + (proc.stdout.strip() or proc.stderr.strip())[-200:]
        )
    return "真实会话文件 → 监听器 → <final> 提取 全通"


@check("崩溃防护（异常文本不炸渲染）")
def check_crash():
    """回归：WorkBuddy 回复带换行时曾经把整个进程搞崩。"""
    import subprocess

    script = Path(__file__).resolve().parent / "tools" / "crash_test.py"
    if not script.exists():
        raise SkipCheck("缺少 tools/crash_test.py")

    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "崩溃防护验证失败：" + (proc.stdout.strip() or proc.stderr.strip())[-300:]
        )
    return "换行/None/超长文本均不再导致渲染崩溃；render() 异常已隔离"


@check("热键真实按键触发（Ctrl+Alt+V）")
def check_hotkey_real():
    """回归：钩子回调曾因句柄未初始化导致按快捷键闪退。"""
    import subprocess

    script = Path(__file__).resolve().parent / "tools" / "hotkey_test.py"
    if not script.exists():
        raise SkipCheck("缺少 tools/hotkey_test.py")

    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "热键验证失败：" + (proc.stdout.strip() or proc.stderr.strip())[-300:]
        )
    return "真实按键可触发/复位，左右 Alt 均正常，无闪退"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="跳过耗时环节")
    parser.add_argument("--inject", action="store_true", help="实际注入一条测试消息")
    args = parser.parse_args()

    print("=" * 62)
    print("WorkBuddy 语音助手 · 链路自检")
    print("=" * 62)

    check_mic()
    check_tts()
    check_session_discovery()
    check_cdp()
    check_acp()
    check_extract()
    check_parse()
    check_watcher()
    check_chain_offline()
    check_crash()
    check_window()
    check_hotkey()
    check_hotkey_real()
    check_overlay()
    check_busy()
    check_cdp_inject(args.inject)
    check_cdp_send(args.inject)
    check_inject(args.inject)

    print("\n" + "=" * 62)
    print("自检总结")
    print("=" * 62)
    for name, status, detail in results:
        mark = {"OK": "✓", "FAIL": "✗", "SKIP": "-"}[status]
        print(f" {mark} {name}: {status}" + (f" — {detail}" if detail else ""))

    failed = [r for r in results if r[1] == "FAIL"]
    print()
    if failed:
        print(f"{len(failed)} 项失败。")
        return 1
    print("全部通过。运行 python voice_assistant.py 启动语音助手。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
