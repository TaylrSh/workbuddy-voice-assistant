---
name: workbuddy-voice-assistant
description: "Install, start, stop, or inspect the Windows floating push-to-talk voice assistant. Use when the user asks to install/启用/启动/停止 WorkBuddy 语音助手，查看它的运行状态，或设置全局语音快捷键。"
version: 1.0.0
author: TaylrSh
license: MIT
agent_created: true
---

# WorkBuddy 语音助手

管理一个 Windows 桌面悬浮语音助手：按住自定义全局快捷键录音，松开后由 WorkBuddy 处理指令，并在悬浮面板显示和朗读回复。

## 使用范围

仅支持 Windows 10/11。脚本会：

1. 将程序复制到 `%LOCALAPPDATA%\WorkBuddyVoiceAssistant`。
2. 在该目录创建独立 Python 虚拟环境并安装依赖。
3. 启动或停止悬浮助手进程。

不要假定程序已经安装。每次操作前先运行状态脚本确认。

## 安装

在技能目录下运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1
```

安装需要 Python 3.11 或更高版本以及网络连接。安装不会创建开机启动项，也不会自动启动悬浮窗。

## 启动与停止

启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1
```

停止：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop.ps1
```

查看状态：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/status.ps1
```

三个脚本都输出单行 JSON，先读取 `ok`、`running`、`pids`、`installDir` 和 `log` 字段，再向用户报告结果，不要编造进程状态。

## 使用说明

- 默认快捷键为 `Ctrl+Alt+V`，可在面板的“快捷键”栏目中修改。
- 面板右上角 `×` 只收起当前面板，不退出程序。
- 退出程序应使用停止脚本，不要结束无关的 Python 进程。
- 首次录音会请求麦克风权限。
- 录音、配置和日志保存在 `%LOCALAPPDATA%\WorkBuddyVoiceAssistant\app`。
- 语音转写由 WorkBuddy 完成；TTS 可能联网。不要声称所有处理都完全离线。

## 故障排查

1. `python not found`：安装 Python 3.11+，勾选“Add Python to PATH”。
2. 安装依赖失败：检查网络和 pip 源，重新运行安装脚本。
3. 已安装但启动失败：读取状态脚本返回的 `log` 末尾 30 行，只报告真实错误。
4. 快捷键无反应：检查是否与其他软件冲突；可在悬浮面板中更换组合键。
5. 全屏游戏看不到悬浮球：建议使用无边框窗口化全屏。

## 安全边界

- 不得在用户未明确要求时启用开机启动。
- 不得静默采集、上传或删除录音。
- 停止操作只能结束安装目录下 `voice_assistant.py` 对应的进程。
- 安装脚本不会删除用户已有配置或录音。
