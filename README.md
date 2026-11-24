# 按住说话语音输入

按住快捷键说话，松开后文字粘贴到当前输入框。模型在本地 CPU 上运行，不占显存，不联网（首次下载模型除外）。

## 在一台新电脑上安装

1. 安装 Python 3.12 或 3.13（python.org，勾选 “Add to PATH”）和 Git。
2. 下载代码（仓库是私有的，需要先在这台电脑上登录 GitHub，比如 `gh auth login`）：
   `git clone https://github.com/APieceOfToast123/voice-input C:\voice-input`
3. 双击 `C:\voice-input\install.bat`。它会：
   - 建 `.venv` 并安装依赖；
   - 让你选麦克风（输入编号）；
   - 自动找微信的安装位置；
   - 把桌宠的钩子合并进这台电脑的 `~/.claude/settings.json`（原文件先备份到 `backup`）；
   - 设好开机自启，并在后台启动。第一次启动会下载 SenseVoice 模型（约 160 MB）。
4. 把手柄连到这台电脑，在 8BitDo App 里确认按键映射（见下方按键表）。
5. 打开记事本，按住 B 说一句话，松开，看能不能出字。

以后想重新选麦克风或重挂钩子，再运行一次 `.venv\Scripts\python voice_input.py --setup` 即可。

**同步更新**：在 `C:\voice-input` 里运行 `git pull`，然后右键桌宠「退出」再重新启动（或重启电脑）。

## 配置文件

- `config.toml`：所有电脑共用的默认值，随仓库同步。
- `config.local.toml`：只跟这台电脑有关的设置（麦克风名字、微信路径、右键菜单里改的颜色和开关），覆盖 `config.toml` 的同名项，**不上传**。
- `ui_state.json`：桌宠的位置，程序自己维护，不上传。

## 桌宠

屏幕右下角有一只像素桌宠（团子 / 小猫 / 小机器人，在 `config.toml` 的 `[pet]` 里选）。

| 什么时候 | 桌宠做什么 |
|---|---|
| 平时 | 呼吸起伏、偶尔眨眼；`sleep_minutes` 分钟没动静就睡觉冒 z |
| 按住说话 | 两侧冒声波，旁边弹出录音胶囊（计时 + 音量格）；松开后胶囊显示「转写中…」→「已粘贴」 |
| 你给 Claude 发了消息 | 抱着小键盘敲字，直到 Claude 做完 |
| Claude 做完 | 跳起来冒星星，叮一声（上行三连音），气泡显示回复开头 |
| Claude 需要你确认 | 头顶冒「!」左右晃，叮咚一声；单击桌宠或你回复后消失 |

- **拖动**换位置，位置记在 `ui_state.json`。在屏幕右半边时胶囊和气泡放左边，左半边时放右边。
- **单击**：摸摸头（小手拍拍、眯眼、冒一颗心），同时把 Claude 切到前台（已经在前台就不动）、关掉「需要你」的提醒、叫醒它。鼠标移到桌宠上会变成小手。
- **右键**菜单：五项提醒开关（会写进 `config.local.toml`）、查看用量（打开 Claude 的用量页面）、录音颜色、复位到右下角、打开配置文件、打开日志、退出。
- 拖动、单击、右键都不会抢走你正在打字的窗口的焦点。
- Claude 的事件来自 Claude Code 的钩子：`~/.claude/settings.json` 里的 `UserPromptSubmit` / `Stop` / `StopFailure` / `Notification` 都调用 `pet_hook.py`，它把事件发给桌宠。桌宠没开时钩子什么也不做。
- 剩余额度：桌面版 Claude 没有官方的读取方式，所以桌宠不显示数字，右键「查看用量」直接打开官方页面。

## 按键

| 手柄 | 快捷键 | 行为 |
|---|---|---|
| B（按住） | Ctrl+Alt+Shift+F9 | 说话，松开后粘贴 |
| A（按住） | Ctrl+Alt+Shift+F10 | 说话，松开后粘贴并回车 |
| R1 | Ctrl+Alt+Shift+F12 | 取消（作废这一句，不粘贴） |
| + | Ctrl+Alt+Shift+F8 | 唤起 Claude；已在前台时再按一下 = 最小化 |
| − | Ctrl+Alt+Shift+F11 | 唤起微信（关到托盘时会重新调出）；已在前台时再按一下 = 最小化 |
| X | Tab | 手柄直接发，不经过本程序 |
| Y、→ | Enter | 手柄直接发，不经过本程序 |
| R2 | Ctrl+Enter | 手柄直接发，不经过本程序 |
| ↑ / ↓ | 方向键 | 手柄直接发，不经过本程序 |
| ←、L1、L2 | — | 暂未定义 |

手柄型号：8BitDo Micro。

快捷键全部在 `config.toml` 里改；手柄映射在 8BitDo 的 App 里改。

## 其他命令

- `python voice_input.py --list-devices`：列出麦克风，把名字里的一段填进 `audio.device`。
- `python voice_input.py --transcribe 某段录音.wav`：单独测 CPU 转写耗时。

## 排查

- 日志在 `logs\voice_input.log`，悬浮窗右键菜单里可以直接打开。
- “快捷键注册失败”：该组合键被别的程序占了，换一个。
- 粘贴不进去：前台窗口如果是以管理员身份运行的，普通权限的本程序无法向它发按键。此时文字会留在剪贴板里。
- 微信里粘贴后剪贴板恢复得太早导致贴出旧内容：调大 `paste.restore_delay`。
- 总是认错某个术语：在 `[replacements]` 里加一条替换。
- 桌宠找不到了（比如跑到拔掉的副屏上）：重启程序会自动回到主屏右下角。
- Claude 做完了桌宠没反应：看 `~/.claude/settings.json` 里的钩子是否还在；钩子在每次会话开始时读取，改完要新开会话。
