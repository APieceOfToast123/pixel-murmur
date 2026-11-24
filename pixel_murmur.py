"""Pixel Murmur：为 vibe coding 而生的语音输入 + 像素桌宠（Windows，纯 CPU，本地模型）。

按住全局快捷键 -> 录音；松开 -> 本地转写 -> 剪贴板 + Ctrl+V 粘贴 ->（可选）回车。
屏幕上的像素桌宠显示录音状态，并通过 Claude Code 钩子（pet_hook.py）对开工、完成、
需要确认做出反应。拖动换位置，单击摸头并唤起 Claude，右键打开菜单。

用法：
    python pixel_murmur.py                     前台运行（看得到日志）
    pythonw pixel_murmur.py                    后台运行（无窗口）
    python pixel_murmur.py --list-devices      列出输入设备
    python pixel_murmur.py --transcribe a.wav  用当前模型转写一个 wav，并报告耗时
    python pixel_murmur.py --install-autostart 开机自启（写入当前用户的注册表 Run 项）
    python pixel_murmur.py --remove-autostart
    python pixel_murmur.py --setup             新电脑一键设置：选麦克风、找微信、挂 Claude 钩子、开机自启

实现上的几个取舍（都是为了稳定）：
- 快捷键用 RegisterHotKey，不装全局键盘钩子。钩子在 Python 进程忙时会拖慢整机键盘，
  还可能被系统悄悄摘掉；RegisterHotKey 没有这些问题，而且自带“长按不重复”。
- 松开靠轮询 GetAsyncKeyState 判断，不依赖“松开事件”，再加最长录音时间兜底。
- 所有 Win32 调用走 ctypes，不依赖 pywin32 / keyboard / pynput。
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import logging
import math
import os
import queue
import re
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np

IS_WIN = sys.platform == "win32"
BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.toml"              # 共用默认值，随仓库同步
LOCAL_CONFIG_PATH = BASE / "config.local.toml"  # 本机设置，覆盖上面的同名项，不上传
LOG_PATH = BASE / "logs" / "pixel_murmur.log"
UI_STATE_PATH = BASE / "ui_state.json"  # 程序自己维护：悬浮窗位置
MODELS_DIR = BASE / "models"
APP_NAME = "PixelMurmur"
LEGACY_APP_NAMES = ("VoiceInput",)  # 改名前的开机自启项，--setup 时顺手删掉
TARGET_SR = 16000

SENSEVOICE_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
SENSEVOICE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    + SENSEVOICE_NAME + ".tar.bz2"
)

log = logging.getLogger(APP_NAME)

# pythonw 下没有标准输出，第三方库一 print 就会抛异常，先接到空设备上。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
    _HAS_CONSOLE = False
else:
    _HAS_CONSOLE = True


# ───────────────────────────── 配置 ─────────────────────────────

DEFAULTS: dict = {
    "hotkeys": {
        "talk": "ctrl+alt+shift+f9",
        "talk_send": "ctrl+alt+shift+f10",
        "cancel": "ctrl+alt+shift+f12",
        "focus": "ctrl+alt+shift+f8",
        "focus_wechat": "ctrl+alt+shift+f11",
    },
    "audio": {
        "device": "",
        "fallback_to_default": False,
        "max_seconds": 120,
        "min_seconds": 0.3,
        "trim_head": 0.15,
        "trim_tail": 0.10,
        "silence_rms": 0.002,
        "retry_seconds": 5,
    },
    "model": {
        "backend": "sensevoice",
        "threads": 4,
        "language": "zh",
        "idle_unload_minutes": 0,
        "sensevoice_dir": "",
        "whisper_model": "small",
        "whisper_compute_type": "int8",
        "whisper_prompt": "以下是普通话的句子，夹杂 Unity、Unreal Engine、Claude Code 等英文术语。",
    },
    "paste": {
        "restore_clipboard": True,
        "restore_delay": 0.4,
        "enter_delay": 0.15,
        "send_on_timeout": False,
    },
    "feedback": {
        "beep": True,
        "beep_start_hz": 880,
        "beep_stop_hz": 660,
        "beep_ms": 120,
        "beep_volume": 0.6,
        "beep_two_tone": True,
        "log_text": True,
    },
    "overlay": {
        "scale": 1.0,
        "opacity": 0.92,
        "recording_color": "#E5484D",
    },
    "pet": {
        "character": "blob",
        "body_color": "#E08A5C",
        "pixel_size": 4,
        "fps": 8,
        "sound_volume": 0.3,
        "done_sound": True,
        "done_bubble": True,
        "alert": True,
        "typing": True,
        "sleep": True,
        "sleep_minutes": 5,
    },
    "focus": {"process_names": ["claude.exe"], "title_contains": "", "launch": ""},
    "focus_wechat": {"process_names": ["weixin.exe", "wechat.exe"], "title_contains": "",
                     "launch": ""},
    "replacements": {},
}


def load_config() -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):  # 后读的覆盖先读的
        if not path.exists():
            continue
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for section, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(section), dict):
                cfg[section].update(value)
            else:
                cfg[section] = value
    return cfg


_LOCAL_HEADER = ("# 本机设置：只跟这台电脑有关，会覆盖 config.toml 里的同名项。这个文件不上传 GitHub。\n"
                 "# 右键桌宠菜单里改的录音颜色和提醒开关也会自动写到这里。\n")


def write_config_value(section: str, key: str, value: str) -> None:
    """把一项设置写进 config.local.toml（本机设置）：改写 [section] 下 key 那一行，
    没有就加上；保留其他内容和注释。value 是 TOML 字面量，例如 '"#E5484D"'。"""
    path = LOCAL_CONFIG_PATH
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) \
        if path.exists() else [_LOCAL_HEADER]
    start = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and not s.startswith("[["):
            if start is not None:
                end = i
                break
            if s.split("#")[0].strip() == f"[{section}]":
                start = i
    if start is not None and end is None:
        end = len(lines)
    pat = re.compile(rf'^(\s*{re.escape(key)}\s*=\s*)("[^"]*"|[^\s#]+)(.*)$', re.S)
    if start is not None:
        for i in range(start + 1, end):
            m = pat.match(lines[i])
            if m:
                lines[i] = m.group(1) + value + m.group(3)
                break
        else:
            lines.insert(start + 1, f"{key} = {value}\n")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", f"[{section}]\n", f"{key} = {value}\n"]
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, path)


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if _HAS_CONSOLE:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


# ───────────────────────────── 音频处理 ─────────────────────────────

def resample(x: np.ndarray, sr: int, target: int = TARGET_SR) -> np.ndarray:
    """单声道 float32 重采样：先低通（降采样时），再线性插值。"""
    if sr == target or len(x) == 0:
        return x.astype(np.float32, copy=False)
    if sr > target:
        fc = 0.45 * target / sr  # 归一化截止频率（周期/采样点）
        half = int(np.ceil(sr / target)) * 32
        k = np.arange(-half, half + 1)
        h = 2 * fc * np.sinc(2 * fc * k) * np.hamming(len(k))
        h /= h.sum()
        x = np.convolve(x, h, mode="same")
    n_out = int(len(x) * target / sr)
    pos = np.arange(n_out) * (sr / target)
    return np.interp(pos, np.arange(len(x)), x).astype(np.float32)


def to_mono(x: np.ndarray) -> np.ndarray:
    """多声道时取能量最大的那一路（大疆接收器可能一路是主音轨、一路是安全音轨或空）。"""
    if x.ndim == 1:
        return x
    if x.shape[1] == 1:
        return x[:, 0]
    energy = (x.astype(np.float64) ** 2).sum(axis=0)
    return x[:, int(np.argmax(energy))]


# ───────────────────────────── 转写 ─────────────────────────────

class Transcriber:
    """模型的加载、卸载和转写。只用 CPU。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg["model"]
        self._lock = threading.Lock()
        self._model = None
        self.last_used = time.monotonic()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            t0 = time.monotonic()
            backend = self.cfg["backend"]
            if backend == "sensevoice":
                self._model = self._load_sensevoice()
            elif backend == "whisper":
                self._model = self._load_whisper()
            else:
                raise ValueError(f"未知的 model.backend: {backend}")
            self.last_used = time.monotonic()
            log.info("模型已加载：%s（%.1f 秒）", backend, time.monotonic() - t0)

    def unload(self) -> None:
        with self._lock:
            if self._model is not None:
                self._model = None
                import gc
                gc.collect()
                log.info("模型已卸载（闲置）")

    def _sensevoice_dir(self) -> Path:
        if self.cfg.get("sensevoice_dir"):
            return Path(self.cfg["sensevoice_dir"])
        d = MODELS_DIR / SENSEVOICE_NAME
        if not (d / "model.int8.onnx").exists():
            self._download_sensevoice()
        return d

    @staticmethod
    def _download_sensevoice() -> None:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MODELS_DIR / "download.tar.bz2.part"
        log.info("首次运行，下载 SenseVoice 模型（约 160 MB）：%s", SENSEVOICE_URL)
        with urllib.request.urlopen(SENSEVOICE_URL, timeout=30) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, next_report = 0, 0.1
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done / total >= next_report:
                    log.info("下载进度 %d%%", int(done * 100 / total))
                    next_report += 0.1
        with tarfile.open(tmp, "r:bz2") as tar:
            tar.extractall(MODELS_DIR, filter="data")
        tmp.unlink(missing_ok=True)
        log.info("模型下载完成")

    def _load_sensevoice(self):
        import sherpa_onnx
        d = self._sensevoice_dir()
        lang = self.cfg["language"]
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=int(self.cfg["threads"]),
            provider="cpu",
            language="" if lang == "auto" else lang,
            use_itn=True,  # 输出带标点、数字转阿拉伯数字
        )

    def _load_whisper(self):
        from faster_whisper import WhisperModel
        return WhisperModel(
            self.cfg["whisper_model"],
            device="cpu",
            compute_type=self.cfg["whisper_compute_type"],
            cpu_threads=int(self.cfg["threads"]),
            download_root=str(MODELS_DIR / "whisper"),
        )

    def transcribe(self, audio: np.ndarray) -> str:
        """audio：16 kHz 单声道 float32。"""
        self.load()
        with self._lock:
            self.last_used = time.monotonic()
            if self.cfg["backend"] == "sensevoice":
                stream = self._model.create_stream()
                stream.accept_waveform(TARGET_SR, audio)
                self._model.decode_stream(stream)
                text = stream.result.text
            else:
                lang = self.cfg["language"]
                segments, _ = self._model.transcribe(
                    audio,
                    language=None if lang == "auto" else lang,
                    initial_prompt=self.cfg.get("whisper_prompt") or None,
                    beam_size=5,
                    condition_on_previous_text=False,
                )
                text = "".join(s.text for s in segments)
            self.last_used = time.monotonic()
        return text.strip()


def apply_replacements(text: str, table: dict) -> str:
    for src, dst in table.items():
        text = text.replace(src, dst)
    return text


# ───────────────────────────── Win32 ─────────────────────────────

if IS_WIN:
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wt.DWORD)
    ole32.CoInitializeEx.restype = ctypes.c_long
    COINIT_MULTITHREADED = 0x0

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _sig(func, restype, *argtypes):
        func.restype = restype
        func.argtypes = argtypes

    _sig(user32.RegisterHotKey, wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
    _sig(user32.UnregisterHotKey, wt.BOOL, wt.HWND, ctypes.c_int)
    _sig(user32.GetMessageW, ctypes.c_int, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
    _sig(user32.PostThreadMessageW, wt.BOOL, wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
    _sig(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
    _sig(user32.SendInput, wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _sig(user32.MapVirtualKeyW, wt.UINT, wt.UINT, wt.UINT)
    _sig(user32.OpenClipboard, wt.BOOL, wt.HWND)
    _sig(user32.CloseClipboard, wt.BOOL)
    _sig(user32.EmptyClipboard, wt.BOOL)
    _sig(user32.EnumClipboardFormats, wt.UINT, wt.UINT)
    _sig(user32.GetClipboardData, wt.HANDLE, wt.UINT)
    _sig(user32.SetClipboardData, wt.HANDLE, wt.UINT, wt.HANDLE)
    _sig(user32.RegisterClipboardFormatW, wt.UINT, wt.LPCWSTR)
    _sig(user32.EnumWindows, wt.BOOL, WNDENUMPROC, wt.LPARAM)
    _sig(user32.IsWindowVisible, wt.BOOL, wt.HWND)
    _sig(user32.IsIconic, wt.BOOL, wt.HWND)
    _sig(user32.ShowWindow, wt.BOOL, wt.HWND, ctypes.c_int)
    _sig(user32.SetForegroundWindow, wt.BOOL, wt.HWND)
    _sig(user32.GetForegroundWindow, wt.HWND)
    _sig(user32.GetWindow, wt.HWND, wt.HWND, wt.UINT)
    _sig(user32.GetWindowTextW, ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    _sig(user32.GetWindowThreadProcessId, wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
    _sig(kernel32.GlobalAlloc, wt.HGLOBAL, wt.UINT, ctypes.c_size_t)
    _sig(kernel32.GlobalFree, wt.HGLOBAL, wt.HGLOBAL)
    _sig(kernel32.GlobalLock, wt.LPVOID, wt.HGLOBAL)
    _sig(kernel32.GlobalUnlock, wt.BOOL, wt.HGLOBAL)
    _sig(kernel32.GlobalSize, ctypes.c_size_t, wt.HGLOBAL)
    _sig(kernel32.CreateMutexW, wt.HANDLE, wt.LPVOID, wt.BOOL, wt.LPCWSTR)
    _sig(kernel32.OpenProcess, wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    _sig(kernel32.CloseHandle, wt.BOOL, wt.HANDLE)
    _sig(kernel32.QueryFullProcessImageNameW, wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR,
         ctypes.POINTER(wt.DWORD))
    _sig(kernel32.GetCurrentThreadId, wt.DWORD)
    _sig(user32.GetAncestor, wt.HWND, wt.HWND, wt.UINT)
    _sig(user32.TranslateMessage, wt.BOOL, ctypes.POINTER(wt.MSG))
    _sig(user32.DispatchMessageW, ctypes.c_ssize_t, ctypes.POINTER(wt.MSG))

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
VK_RETURN, VK_V = 0x0D, 0x56
SW_MINIMIZE = 6
KEYEVENTF_KEYUP = 0x2
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x2
# 这些剪贴板格式的句柄不是普通内存块，无法按字节保存，跳过。
_CF_SKIP = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
_CLIP_SAVE_LIMIT = 64 << 20

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "pause": 0x13,
               "scrolllock": 0x91, "insert": 0x2D, "home": 0x24, "end": 0x23,
               "pageup": 0x21, "pagedown": 0x22}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+shift+f9' -> (修饰键掩码, 虚拟键码)"""
    mods, vk = 0, None
    for part in spec.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _NAMED_KEYS:
            vk = _NAMED_KEYS[part]
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"无法识别的按键：{part!r}（来自 {spec!r}）")
    if vk is None:
        raise ValueError(f"快捷键缺少主键：{spec!r}")
    return mods, vk


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def wait_keys_released(extra_vks: tuple[int, ...] = (), timeout: float = 2.0) -> bool:
    """等用户松开所有修饰键（否则我们发的 Ctrl+V 会变成 Ctrl+Alt+Shift+V）。"""
    vks = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN) + tuple(extra_vks)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(key_down(v) for v in vks):
            return True
        time.sleep(0.01)
    return False


def send_keys(*vks: int) -> None:
    """依次按下再逆序松开，例如 send_keys(VK_CONTROL, VK_V)。"""
    seq = [(v, 0) for v in vks] + [(v, KEYEVENTF_KEYUP) for v in reversed(vks)]
    arr = (INPUT * len(seq))()
    for i, (vk, flags) in enumerate(seq):
        arr[i].type = 1  # INPUT_KEYBOARD
        arr[i].u.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
    sent = user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
    if sent != len(seq):
        raise OSError(f"SendInput 只发出了 {sent}/{len(seq)} 个事件（前台窗口可能是管理员权限）")


# ── 剪贴板 ──

class _Clipboard:
    def __enter__(self):
        for _ in range(20):  # 别的程序可能正占着剪贴板，重试约 0.5 秒
            if user32.OpenClipboard(None):
                return self
            time.sleep(0.025)
        raise OSError("无法打开剪贴板")

    def __exit__(self, *exc):
        user32.CloseClipboard()


def _set_clip_bytes(fmt: int, data: bytes) -> None:
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
    if not h:
        raise MemoryError("GlobalAlloc 失败")
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    if not user32.SetClipboardData(fmt, h):
        kernel32.GlobalFree(h)
        raise OSError("SetClipboardData 失败")


def clipboard_snapshot() -> list[tuple[int, bytes]]:
    """尽量完整地保存当前剪贴板（文字、图片 DIB、文件列表等）。"""
    saved, total = [], 0
    with _Clipboard():
        fmt = 0
        while fmt := user32.EnumClipboardFormats(fmt):
            if fmt in _CF_SKIP or 0x200 <= fmt <= 0x3FF:
                continue
            h = user32.GetClipboardData(fmt)
            if not h:
                continue
            size = kernel32.GlobalSize(h)
            if not size or total + size > _CLIP_SAVE_LIMIT:
                continue
            p = kernel32.GlobalLock(h)
            if not p:
                continue
            try:
                saved.append((fmt, ctypes.string_at(p, size)))
                total += size
            finally:
                kernel32.GlobalUnlock(h)
    return saved


def clipboard_restore(saved: list[tuple[int, bytes]]) -> None:
    with _Clipboard():
        user32.EmptyClipboard()
        for fmt, data in saved:
            try:
                _set_clip_bytes(fmt, data)
            except Exception:
                pass


def clipboard_set_text(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    with _Clipboard():
        user32.EmptyClipboard()
        _set_clip_bytes(CF_UNICODETEXT, data)
        # 告诉 Win+V 剪贴板历史和云同步：这条是临时内容，别记录。
        for name in ("ExcludeClipboardContentFromMonitorProcessing",
                     "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
            try:
                _set_clip_bytes(user32.RegisterClipboardFormatW(name), b"\x00\x00\x00\x00")
            except Exception:
                pass


def paste_text(text: str, cfg: dict, trigger_vk: int) -> bool:
    """粘贴到当前焦点。失败时文字留在剪贴板里，返回 False。"""
    pc = cfg["paste"]
    saved = None
    if pc["restore_clipboard"]:
        try:
            saved = clipboard_snapshot()
        except Exception:
            log.warning("保存原剪贴板失败，本次不恢复", exc_info=True)
    clipboard_set_text(text)
    if not wait_keys_released((trigger_vk,)):
        log.warning("修饰键一直没松开（蓝牙断连？），文字已留在剪贴板，请手动粘贴")
        return False
    send_keys(VK_CONTROL, VK_V)
    time.sleep(float(pc["restore_delay"]))  # 等目标程序读完剪贴板
    if saved is not None:
        try:
            clipboard_restore(saved)
        except Exception:
            log.warning("恢复原剪贴板失败", exc_info=True)
    return True


# ── 窗口 ──

def _process_path(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _process_name(pid: int) -> str:
    return os.path.basename(_process_path(pid)).lower()


def find_window(process_names: list[str], title_contains: str = "") -> int | None:
    names = {n.lower() for n in process_names}
    needle = title_contains.lower()
    found: list[int] = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # 4 = GW_OWNER
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if not title.value or (needle and needle not in title.value.lower()):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_name(pid.value) in names:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None


def foreground_root_if(process_names: list[str], title_contains: str = "") -> int | None:
    """前台窗口属于目标程序（且标题符合）时，返回它的顶层窗口句柄，否则 None。"""
    fg = user32.GetForegroundWindow()
    if not fg:
        return None
    root = user32.GetAncestor(fg, 3) or fg  # GA_ROOTOWNER：对话框等算到主窗口头上
    if user32.IsIconic(root):  # 刚最小化的窗口有时仍被系统当成前台，要算作“不在前台”
        return None
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    if _process_name(pid.value) not in {n.lower() for n in process_names}:
        return None
    if title_contains:
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(root, title, 512)
        if title_contains.lower() not in title.value.lower():
            return None
    return root


def focus_window(hwnd: int) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.SetForegroundWindow(hwnd) and user32.GetForegroundWindow() == hwnd:
        return True
    # 系统拒绝抢前台时的常用办法：先轻点一下 Alt 再试。
    try:
        send_keys(VK_MENU)
    except OSError:
        pass
    user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd


# ───────────────────────────── 提示音 ─────────────────────────────

def _tone_wav(freqs: list[float], ms: float, volume: float, sr: int = 44100,
              wave_shape: str = "sine") -> bytes:
    """合成一段提示音（多个频率依次播放）。
    sine：平稳的正弦，首尾各 5 ms 淡入淡出防爆音（语音输入用）。
    chip：方波 + 指数衰减，像素游戏那种“叮”（桌宠用）。"""
    import wave
    n_each = max(int(sr * ms / 1000 / len(freqs)), 1)
    t = np.arange(n_each) / sr
    fade = min(int(sr * 0.005), n_each // 2)
    env = np.ones(n_each)
    if fade:
        env[:fade] = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
    if wave_shape == "chip":
        env = env * np.exp(-6.9 * t / (n_each / sr))  # 每个音结束时衰减到约 -60 dB
        y = np.concatenate([np.sign(np.sin(2 * np.pi * f * t)) * 0.6 * env for f in freqs])
    else:
        y = np.concatenate([np.sin(2 * np.pi * f * t) * env for f in freqs])
    pcm = (y * min(max(volume, 0.0), 1.0) * 32767 * 0.9).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def play_wav(data: bytes) -> None:
    """在后台线程里播放一段内存中的 wav（新的一声会打断上一声）。"""
    if not IS_WIN:
        return
    import winsound
    threading.Thread(target=winsound.PlaySound,
                     args=(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT),
                     daemon=True).start()


class Beeper:
    """开始 / 结束 / 取消 / 出错提示音。自己合成波形，所以时长、音量、双音都可调。"""

    def __init__(self, fc: dict):
        self.enabled = bool(fc["beep"]) and IS_WIN
        vol, ms, two = float(fc["beep_volume"]), float(fc["beep_ms"]), bool(fc["beep_two_tone"])
        f1, f2 = float(fc["beep_start_hz"]), float(fc["beep_stop_hz"])
        self._wavs = {
            "start": _tone_wav([f1 * 0.75, f1] if two else [f1], ms, vol),  # 上扬
            "stop": _tone_wav([f2, f2 * 0.75] if two else [f2], ms, vol),   # 下降
            "cancel": _tone_wav([440], 90, vol),
            "error": _tone_wav([300], 220, vol),
        }

    def __call__(self, kind: str) -> None:
        if self.enabled:
            play_wav(self._wavs[kind])  # SND_MEMORY 不能异步播放，所以放到线程里同步播


# ───────────────────────────── 悬浮窗 ─────────────────────────────
# 分层窗口（UpdateLayeredWindow）+ Pillow 画图：逐像素透明、抗锯齿。
# 不抢焦点（WS_EX_NOACTIVATE），整块当标题栏用来拖动，右键弹原生菜单。
# 其他线程只改状态字段，真正的绘制都在主线程的 WM_TIMER 里做。

if IS_WIN:
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    comdlg32 = ctypes.WinDLL("comdlg32")
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                    ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                    ("dwFlags", wt.DWORD)]

    class CHOOSECOLORW(ctypes.Structure):
        _fields_ = [("lStructSize", wt.DWORD), ("hwndOwner", wt.HWND), ("hInstance", wt.HWND),
                    ("rgbResult", wt.DWORD), ("lpCustColors", ctypes.POINTER(wt.DWORD)),
                    ("Flags", wt.DWORD), ("lCustData", wt.LPARAM), ("lpfnHook", ctypes.c_void_p),
                    ("lpTemplateName", wt.LPCWSTR)]

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wt.DWORD), ("lpData", ctypes.c_void_p)]

    _sig(user32.RegisterClassExW, wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
    _sig(user32.CreateWindowExW, wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
         wt.HINSTANCE, wt.LPVOID)
    _sig(user32.DefWindowProcW, LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    _sig(user32.DestroyWindow, wt.BOOL, wt.HWND)
    _sig(user32.SetTimer, ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p)
    _sig(user32.UpdateLayeredWindow, wt.BOOL, wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
         ctypes.POINTER(wt.SIZE), wt.HDC, ctypes.POINTER(wt.POINT), wt.DWORD,
         ctypes.POINTER(BLENDFUNCTION), wt.DWORD)
    _sig(user32.GetDC, wt.HDC, wt.HWND)
    _sig(user32.ReleaseDC, ctypes.c_int, wt.HWND, wt.HDC)
    _sig(user32.GetWindowRect, wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
    _sig(user32.MonitorFromPoint, wt.HANDLE, wt.POINT, wt.DWORD)
    _sig(user32.GetMonitorInfoW, wt.BOOL, wt.HANDLE, ctypes.POINTER(MONITORINFO))
    _sig(user32.SystemParametersInfoW, wt.BOOL, wt.UINT, wt.UINT, wt.LPVOID, wt.UINT)
    _sig(user32.CreatePopupMenu, wt.HMENU)
    _sig(user32.AppendMenuW, wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
    _sig(user32.TrackPopupMenu, wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, wt.HWND, ctypes.c_void_p)
    _sig(user32.DestroyMenu, wt.BOOL, wt.HMENU)
    _sig(user32.PostMessageW, wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    _sig(user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
    _sig(user32.LoadCursorW, wt.HANDLE, wt.HINSTANCE, ctypes.c_void_p)
    _sig(user32.SetCursor, wt.HANDLE, wt.HANDLE)
    _sig(kernel32.GetModuleHandleW, wt.HMODULE, wt.LPCWSTR)
    _sig(gdi32.CreateCompatibleDC, wt.HDC, wt.HDC)
    _sig(gdi32.DeleteDC, wt.BOOL, wt.HDC)
    _sig(gdi32.CreateDIBSection, wt.HBITMAP, wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
         ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
    _sig(gdi32.SelectObject, wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
    _sig(gdi32.DeleteObject, wt.BOOL, wt.HGDIOBJ)
    _sig(comdlg32.ChooseColorW, wt.BOOL, ctypes.POINTER(CHOOSECOLORW))

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x8, 0x80, 0x80000, 0x8000000
WM_NULL, WM_TIMER, WM_NCHITTEST, WM_MOUSEACTIVATE = 0x0000, 0x0113, 0x0084, 0x0021
WM_NCRBUTTONUP, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE = 0x00A5, 0x0231, 0x0232
WM_NCLBUTTONDOWN, WM_SETCURSOR, IDC_HAND = 0x00A1, 0x0020, 32649
HTCAPTION, MA_NOACTIVATE, SW_SHOWNOACTIVATE = 2, 3, 4
MF_STRING, MF_POPUP, MF_SEPARATOR, MF_CHECKED = 0x0, 0x10, 0x800, 0x8
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
SPI_GETWORKAREA, MONITOR_DEFAULTTONULL, MONITOR_DEFAULTTONEAREST = 0x30, 0, 2
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
WM_COPYDATA = 0x004A
PET_MSG_MAGIC = 0x50455431  # 'PET1'：pet_hook.py 发来的 WM_COPYDATA 标记

_FONT_FILES = [("C:/Windows/Fonts/msyh.ttc", 1), ("C:/Windows/Fonts/msyh.ttc", 0),
               ("C:/Windows/Fonts/segoeui.ttf", 0)]


def _hex_rgb(s: str, fallback=(229, 72, 77)) -> tuple[int, int, int]:
    s = str(s).strip().lstrip("#")
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except (ValueError, IndexError):
        return fallback


def set_dpi_awareness() -> None:
    """按显示器 DPI 感知，悬浮窗才不会在高分屏上被系统拉伸发糊。"""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


# 像素桌宠：16×16 角色，画在 28×26 的格子里（四周留出放星星、声波、“!”、键盘的位置）。
# 字符含义：o 描边  b 身体  h 高光  c 腮红/嘴  e 眼睛  s 屏幕  y 天线灯
_PET_SPRITES = {
    "blob": [
        "................",
        "................",
        "................",
        ".....oooooo.....",
        "....obbbbbbo....",
        "...obbhhbbbbo...",
        "..obbhbbbbbbbo..",
        "..obbbbbbbbbbo..",
        ".obbbebbbbebbbo.",
        ".obbbebbbbebbbo.",
        ".obcbbbbbbbbcbo.",
        ".obbbbbooobbbbo.",
        ".obbbbbbbbbbbbo.",
        "..obbbbbbbbbbo..",
        "...oooooooooo...",
        "................",
    ],
    "cat": [
        "................",
        "..o.........o...",
        ".obo.......obo..",
        ".obbo.....obbo..",
        ".obbbooooobbbo..",
        ".obbbbbbbbbbbo..",
        "obbhbbbbbbbbbbo.",
        "obbebbbbbbebbbo.",
        "obbebbbbbbebbbo.",
        "obcbbbbobbbbcbo.",
        "obbbbbobobbbbbo.",
        ".obbbbbbbbbbbo..",
        ".obbbbbbbbbbbo..",
        "..obbo...obbo...",
        "..ooo.....ooo...",
        "................",
    ],
    "bot": [
        ".......o........",
        ".......y........",
        "....oooooooo....",
        "...obbbbbbbbo...",
        "...obssssssbo...",
        "...obsesseshbo..",
        "...obsesseshbo..",
        "...obssssssbo...",
        "...obbbbbbbbo...",
        "....oooooooo....",
        ".....obbbbo.....",
        "....obbccbbo....",
        "...obobbbbobo...",
        "....obbbbbbo....",
        "....oo....oo....",
        "................",
    ],
}
PET_GW, PET_GH, PET_OX, PET_OY = 28, 26, 6, 7


def _shade(rgb, f: float) -> tuple[int, int, int]:
    """f > 0 变亮（向白色靠），f < 0 变暗。"""
    return tuple(max(0, min(255, round(v + (255 - v) * f if f > 0 else v * (1 + f)))) for v in rgb)


class Overlay:
    """桌宠悬浮窗。平时是像素小人；按住说话时旁边弹录音胶囊；
    Claude Code 的钩子（pet_hook.py）发来事件时做动作、叫一声、冒气泡。"""

    COLORS = [("红", "#E5484D"), ("橙", "#F76B15"), ("蓝", "#3E63DD"), ("绿", "#30A46C")]
    PET_OPTS = [("done_sound", "做完时叫一声"), ("done_bubble", "做完时气泡显示回复开头"),
                ("alert", "需要确认时提醒"), ("typing", "干活时敲键盘"), ("sleep", "闲置时睡觉")]
    USAGE_URL = "https://claude.ai/settings/usage"
    # 胶囊的版式（逻辑像素，乘以 DPI 缩放和 overlay.scale）
    PAD_X, H, GAP, DOT, MARGIN = 14, 38, 10, 10, 16
    BG = (32, 32, 32)
    TEXT, SUBTEXT = (242, 242, 242), (169, 169, 169)
    BUSY, DONE, ERROR, INFO = (245, 165, 36), (61, 214, 140), (185, 140, 255), (61, 214, 140)

    def __init__(self, cfg: dict, on_quit, on_click=None):
        self.cfg = cfg
        oc, pc = cfg["overlay"], cfg["pet"]
        self.scale = float(oc["scale"])
        self.opacity = min(max(float(oc["opacity"]), 0.0), 1.0)
        self.color = _hex_rgb(oc["recording_color"])
        self.pet_char = pc["character"] if pc["character"] in _PET_SPRITES else "blob"
        self.pet_color = _hex_rgb(pc["body_color"], (224, 138, 92))
        self.pet_ps = max(1, int(pc["pixel_size"]))
        self.pet_fps = max(1.0, float(pc["fps"]))
        self.pet_opt = {k: bool(pc[k]) for k, _ in self.PET_OPTS}
        self.sleep_minutes = float(pc["sleep_minutes"])
        vol = min(max(float(pc["sound_volume"]), 0.0), 1.0) * 0.25
        self._snd_done = _tone_wav([784, 988, 1319], 330, vol, wave_shape="chip")   # 上行三连音
        self._snd_alert = _tone_wav([1047, 784], 360, vol, wave_shape="chip")       # 下行叮咚
        self.on_quit = on_quit
        self.on_click = on_click
        self.hwnd = None
        self._lock = threading.Lock()
        self._base = "loading"          # 语音输入：loading / idle / recording / busy
        self._msg = None                # (kind, text, 截止时间)：done / error / info
        self._t0 = 0.0
        self._levels = [0.0] * 5
        self._dirty = True
        now = time.monotonic()
        # 桌宠状态（只在主线程里读写）
        self._sessions: dict[str, float] = {}   # 正在干活的 Claude 会话 -> 开始时间
        self._celebrate_until = 0.0
        self._alert_until = 0.0
        self._pat_until = 0.0           # 被摸头（单击）动作的结束时间
        self._bubble = None             # (文字, 截止时间, 种类)
        self._activity = now            # 最近一次有动静的时间，用来判断睡觉
        self._pet_state, self._pet_t0 = "idle", now
        self._last_key = None
        self._anchor = None             # 桌宠格子左上角的屏幕坐标（物理像素）
        self._ax = self._ay = 0         # 桌宠格子左上角在窗口图像里的位置
        self._pet_w = 0
        self._moving = False
        self._move_rect = None
        self._fonts: dict = {}
        self._shadow_cache: dict = {}
        self._custom_colors = (wt.DWORD * 16)() if IS_WIN else None
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: F401
            self.enabled = True
        except Exception:
            log.warning("未安装 Pillow，不显示悬浮窗")
            self.enabled = False

    # ── 给其他线程用的接口（只改字段） ──

    def set(self, state: str) -> None:
        with self._lock:
            if state == "recording":
                self._t0 = time.monotonic()
                self._levels = [0.0] * 5
                self._msg = None
            self._base = state
            self._dirty = True
        self._activity = time.monotonic()

    def message(self, text: str, kind: str = "error", seconds: float = 4.0) -> None:
        with self._lock:
            self._msg = (kind, text, time.monotonic() + seconds)
            self._dirty = True

    def done(self) -> None:
        self.message("已粘贴", "done", 0.9)

    def push_level(self, rms: float) -> None:
        """录音回调里调用：把音量（RMS）换算成 0..1，-55 dB 为 0，-15 dB 为 1。"""
        db = 20 * math.log10(rms + 1e-9)
        lvl = min(max((db + 55) / 40, 0.0), 1.0)
        with self._lock:
            self._levels = self._levels[1:] + [lvl]

    # ── Claude Code 事件（主线程，WM_COPYDATA） ──

    def pet_event(self, ev: dict) -> None:
        now = time.monotonic()
        name = ev.get("event") or ""
        sid = ev.get("session") or "?"
        self._activity = now
        if name == "UserPromptSubmit":
            self._sessions[sid] = now
            self._clear_alert()
        elif name in ("Stop", "StopFailure"):
            self._sessions.pop(sid, None)
            self._clear_alert()
            if name == "Stop":
                self._celebrate_until = now + 2.2
                if self.pet_opt["done_sound"]:
                    play_wav(self._snd_done)
                if self.pet_opt["done_bubble"]:
                    self._bubble = ((ev.get("text") or "").strip() or "做好了！", now + 5, "done")
        elif name == "Notification":
            kind = ev.get("type") or ""
            text = (ev.get("message") or "").lower()
            needs_you = kind in ("permission_prompt", "elicitation_dialog") or \
                (not kind and "permission" in text)
            if needs_you and self.pet_opt["alert"]:
                self._alert_until = now + 600
                play_wav(self._snd_alert)
                self._bubble = ("需要你确认一下", self._alert_until, "alert")
        self._dirty = True

    def _clear_alert(self) -> None:
        self._alert_until = 0.0
        if self._bubble and self._bubble[2] == "alert":
            self._bubble = None

    def _on_click(self) -> None:
        """单击桌宠：摸摸头（一个小动作），关掉“需要你”的提醒、叫醒它，并把 Claude 切到前台。"""
        self._clear_alert()
        self._celebrate_until = 0.0
        self._activity = time.monotonic()
        self._pat_until = self._activity + 1.4
        self._dirty = True
        log.info("单击桌宠")
        if self.on_click:
            self.on_click()

    def _pet_anim(self, now: float, base: str) -> str:
        if base == "recording":
            return "listen"
        if self._alert_until > now:
            return "alert"
        if self._pat_until > now:
            return "pat"
        if self._celebrate_until > now:
            return "celebrate"
        # 30 分钟还没收到 Stop（比如会话被直接关掉）就不再算在干活
        self._sessions = {k: v for k, v in self._sessions.items() if now - v < 1800}
        if self._sessions and self.pet_opt["typing"]:
            return "typing"
        if self.pet_opt["sleep"] and self.sleep_minutes > 0 \
                and now - self._activity > self.sleep_minutes * 60:
            return "sleep"
        return "idle"

    # ── 主线程：窗口 ──

    def create(self) -> None:
        if not (self.enabled and IS_WIN):
            return
        self._proc = WNDPROC(self._wndproc)  # 保持引用，防止被回收
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
        wc.lpszClassName = "PixelMurmurOverlay"
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
            "PixelMurmurOverlay", "语音输入", WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
        if not self.hwnd:
            log.error("创建悬浮窗失败（错误码 %d）", ctypes.get_last_error())
            self.enabled = False
            return
        self._anchor = self._load_anchor()
        self._render(force=True)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetTimer(self.hwnd, 1, 40, None)

    def destroy(self) -> None:
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_NCHITTEST:
                return HTCAPTION            # 整块都能拖（全透明的像素系统会直接穿透）
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_SETCURSOR:
                # 整块被当成标题栏，系统默认给箭头；这里换成小手，提示“可以点”
                user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND)))
                return 1
            if msg == WM_NCLBUTTONDOWN and wp == HTCAPTION:
                # 交给系统处理拖动；它会一直等到松开鼠标才返回。
                # 返回后窗口没挪过位置 = 单击（只点不拖时系统不一定发“拖动结束”消息）。
                before = self._window_rect()
                user32.DefWindowProcW(hwnd, msg, wp, lp)
                if self._window_rect() == before:
                    self._on_click()
                return 0
            if msg == WM_ENTERSIZEMOVE:
                self._moving = True
                return 0
            if msg == WM_EXITSIZEMOVE:
                self._moving = False
                self._anchor_from_window()
                self._save_anchor()
                self._render(force=True)    # 拖过屏幕中线时换胶囊、气泡的方向
                return 0
            if msg == WM_NCRBUTTONUP:
                self._show_menu()
                return 0
            if msg == WM_COPYDATA:
                cds = ctypes.cast(ctypes.c_void_p(lp), ctypes.POINTER(COPYDATASTRUCT)).contents
                if cds.dwData == PET_MSG_MAGIC and 0 < cds.cbData < 65536:
                    ev = json.loads(ctypes.string_at(cds.lpData, cds.cbData).decode("utf-8"))
                    self.pet_event(ev)
                return 1
            if msg == WM_TIMER:
                self._render()
                return 0
        except Exception:
            log.exception("悬浮窗消息处理出错")
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ── 位置 ──

    def _dpi_k(self) -> float:
        try:
            dpi = user32.GetDpiForWindow(self.hwnd) or 96
        except Exception:
            dpi = 96
        return dpi / 96

    def _k(self) -> float:
        return self._dpi_k() * self.scale

    def _cell(self) -> int:
        """桌宠一个像素块在屏幕上的物理像素数（取整，保证像素边缘锐利）。"""
        return max(1, round(self.pet_ps * self._dpi_k()))

    def _window_rect(self) -> tuple[int, int, int, int]:
        rc = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rc))
        return rc.left, rc.top, rc.right, rc.bottom

    def _home(self) -> tuple[int, int]:
        rc = wt.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rc), 0)
        s, dk = self._cell(), self._dpi_k()
        # 身体右边离屏幕右边 12 像素，身体底边离任务栏 6 像素
        return (rc.right - round(12 * dk) - (PET_OX + 16) * s,
                rc.bottom - round(6 * dk) - (PET_OY + 15) * s)

    def reset_position(self) -> None:
        self._anchor = self._home()
        self._save_anchor()
        self._render(force=True)

    def _load_anchor(self) -> tuple[int, int]:
        try:
            d = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
            x, y = int(d["pet_x"]), int(d["pet_y"])
            s = self._cell()
            center = wt.POINT(x + (PET_OX + 8) * s, y + (PET_OY + 8) * s)
            # 记住的位置已经不在任何屏幕上（比如拔了副屏），回到主屏右下角
            if user32.MonitorFromPoint(center, MONITOR_DEFAULTTONULL):
                return x, y
        except Exception:
            pass
        return self._home()

    def _save_anchor(self) -> None:
        try:
            UI_STATE_PATH.write_text(
                json.dumps({"pet_x": self._anchor[0], "pet_y": self._anchor[1]}), encoding="utf-8")
        except Exception:
            log.warning("保存悬浮窗位置失败", exc_info=True)

    def _anchor_from_window(self) -> None:
        rc = wt.RECT()
        if user32.GetWindowRect(self.hwnd, ctypes.byref(rc)):
            self._anchor = (rc.left + self._ax, rc.top + self._ay)

    def _expand_left(self) -> bool:
        """桌宠在屏幕右半边时，胶囊和气泡往左边放。"""
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        cx = self._anchor[0] + self._pet_w // 2
        mon = user32.MonitorFromPoint(wt.POINT(cx, self._anchor[1]), MONITOR_DEFAULTTONEAREST)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return True
        return cx > (mi.rcWork.left + mi.rcWork.right) / 2

    # ── 菜单 ──

    def _show_menu(self) -> None:
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cur = "#%02X%02X%02X" % self.color
        sub = user32.CreatePopupMenu()
        for i, (name, hx) in enumerate(self.COLORS):
            user32.AppendMenuW(sub, MF_STRING | (MF_CHECKED if hx == cur else 0), 100 + i, name)
        user32.AppendMenuW(sub, MF_SEPARATOR, 0, None)
        custom = not any(hx == cur for _, hx in self.COLORS)
        user32.AppendMenuW(sub, MF_STRING | (MF_CHECKED if custom else 0), 110, "自定义…")
        menu = user32.CreatePopupMenu()
        for i, (key, name) in enumerate(self.PET_OPTS):
            user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if self.pet_opt[key] else 0), 200 + i, name)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 4, "查看用量")
        user32.AppendMenuW(menu, MF_POPUP, sub, "录音颜色")
        user32.AppendMenuW(menu, MF_STRING, 1, "复位到右下角")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 2, "打开配置文件")
        user32.AppendMenuW(menu, MF_STRING, 3, "打开日志")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 9, "退出")
        # 菜单要求所属窗口在前台，否则点别处关不掉；关掉后把焦点还给原来的窗口。
        prev = user32.GetForegroundWindow()
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)  # 子菜单随之销毁
        if prev and cmd not in (2, 3, 4, 110):
            user32.SetForegroundWindow(prev)
        if 100 <= cmd < 100 + len(self.COLORS):
            self._set_color(self.COLORS[cmd - 100][1])
        elif cmd == 110:
            self._pick_custom_color()
            if prev:
                user32.SetForegroundWindow(prev)
        elif 200 <= cmd < 200 + len(self.PET_OPTS):
            self._toggle(self.PET_OPTS[cmd - 200][0])
        elif cmd == 1:
            self.reset_position()
        elif cmd == 2:
            os.startfile(CONFIG_PATH)
        elif cmd == 3:
            os.startfile(LOG_PATH)
        elif cmd == 4:
            os.startfile(self.USAGE_URL)
        elif cmd == 9:
            self.on_quit()

    def _toggle(self, key: str) -> None:
        self.pet_opt[key] = not self.pet_opt[key]
        self.cfg["pet"][key] = self.pet_opt[key]
        if key == "alert" and not self.pet_opt[key]:
            self._clear_alert()
        try:
            write_config_value("pet", key, "true" if self.pet_opt[key] else "false")
        except Exception:
            log.warning("写回 config.toml 失败", exc_info=True)
        log.info("桌宠设置 %s = %s", key, self.pet_opt[key])
        self._dirty = True

    def _set_color(self, hx: str) -> None:
        self.color = _hex_rgb(hx)
        self.cfg["overlay"]["recording_color"] = hx
        try:
            write_config_value("overlay", "recording_color", f'"{hx}"')
        except Exception:
            log.warning("写回 config.toml 失败", exc_info=True)
        log.info("录音颜色改为 %s", hx)
        self._dirty = True

    def _pick_custom_color(self) -> None:
        r, g, b = self.color
        cc = CHOOSECOLORW()
        cc.lStructSize = ctypes.sizeof(CHOOSECOLORW)
        cc.hwndOwner = self.hwnd
        cc.rgbResult = r | (g << 8) | (b << 16)
        cc.lpCustColors = self._custom_colors
        cc.Flags = 0x1 | 0x2  # CC_RGBINIT | CC_FULLOPEN
        if comdlg32.ChooseColorW(ctypes.byref(cc)):
            v = cc.rgbResult
            self._set_color("#%02X%02X%02X" % (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))

    # ── 绘制 ──

    def _font(self, px: float):
        from PIL import ImageFont
        px = max(int(round(px)), 6)
        if px not in self._fonts:
            font = None
            for path, idx in _FONT_FILES:
                try:
                    font = ImageFont.truetype(path, px, index=idx)
                    break
                except Exception:
                    continue
            self._fonts[px] = font or ImageFont.load_default(px)
        return self._fonts[px]

    def _snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            if self._msg and now > self._msg[2]:
                self._msg = None
                self._dirty = True
            st = {"base": self._base, "msg": self._msg, "t0": self._t0,
                  "levels": list(self._levels), "dirty": self._dirty, "now": now}
            self._dirty = False
        return st

    def _render(self, force: bool = False) -> None:
        if not self.hwnd:
            return
        from PIL import Image
        st = self._snapshot()
        now, base = st["now"], st["base"]
        anim = self._pet_anim(now, base)
        if anim != self._pet_state:
            self._pet_state, self._pet_t0 = anim, now
        el = now - self._pet_t0
        if self._bubble and self._bubble[1] <= now:
            self._bubble = None
        pill_live = base in ("recording", "busy")
        key = (anim, int(el * self.pet_fps), base, st["msg"], self._bubble)
        if not (force or st["dirty"] or pill_live or key != self._last_key):
            return
        self._last_key = key
        if self._moving:
            self._anchor_from_window()
        k, s = self._k(), self._cell()
        self._pet_w = PET_GW * s
        left = self._expand_left()
        items = [(self._draw_pet(anim, el, s), 0, 0)]   # 桌宠格子放在 (0, 0)，其他东西相对它摆
        pill = None
        if base == "recording":
            secs = int(now - st["t0"])
            pill = self._draw_pill(k, self.color, "录音中", f"{secs // 60}:{secs % 60:02d}",
                                   st["levels"], "pulse", now)
        elif st["msg"]:
            kind, text, _ = st["msg"]
            accent = {"done": self.DONE, "info": self.INFO}.get(kind, self.ERROR)
            pill = self._draw_pill(k, accent, text[:30], "", None,
                                   "check" if kind == "done" else "dot", now)
        elif base == "busy":
            pill = self._draw_pill(k, self.BUSY, "转写中…", "", None, "blink", now)
        if pill:
            img = pill[0]
            # 胶囊放在桌宠格子外面（格子两侧留着画声波），不挡身体也不挡声波。
            # 胶囊图四周带一圈阴影留白（MARGIN），可见边缘要扣掉它。
            shadow, gap = round(self.MARGIN * k), round(2 * self._dpi_k())
            x = -gap - (img.width - shadow) if left else PET_GW * s + gap - shadow
            items.append((img, x, (PET_OY + 8) * s - img.height // 2))
        if self._bubble:
            b = self._draw_bubble(self._dpi_k(), self._bubble[0])
            bx = (PET_OX + 16) * s - b.width if left else PET_OX * s
            items.append((b, bx, (PET_OY - 7) * s - b.height))
        min_x = min(x for _, x, _ in items)
        min_y = min(y for _, _, y in items)
        max_x = max(x + im.width for im, x, _ in items)
        max_y = max(y + im.height for im, _, y in items)
        canvas = Image.new("RGBA", (max_x - min_x, max_y - min_y), (0, 0, 0, 0))
        for im, x, y in items:
            canvas.alpha_composite(im, (x - min_x, y - min_y))
        self._ax, self._ay = -min_x, -min_y
        self._blit(canvas, self._anchor[0] - self._ax, self._anchor[1] - self._ay)

    def _pet_pixels(self, anim: str, el: float) -> list:
        rows = _PET_SPRITES[self.pet_char]
        body = self.pet_color
        pal = {"o": (42, 35, 32), "b": body, "h": _shade(body, 0.45), "c": (242, 143, 160),
               "e": (29, 29, 29), "s": (22, 32, 44), "y": (255, 213, 74)}
        if self.pet_char == "bot":
            pal["e"], pal["c"] = (94, 234, 212), _shade(body, -0.35)
        cyc = int(el * self.pet_fps)
        dx = dy = 0
        if anim == "idle":
            dy = int(el * 1.2) % 2
        elif anim == "typing":
            dy = cyc % 2
        elif anim == "celebrate":
            dy = [0, -2, -4, -5, -5, -4, -2, 0, 0, 0][cyc % 10]
        elif anim == "alert":
            dx = [0, 1, 0, -1][cyc % 4]
        elif anim == "sleep":
            dy = int(el * 0.8) % 2
        elif anim == "pat":
            dy = [1, 1, 0, 0][cyc % 4]                     # 被拍一下就往下缩一点
        blink = anim in ("sleep", "pat") or (anim != "celebrate" and (el % 3.2) > 3.0)
        eye_rows = [y for y, r in enumerate(rows) if "e" in r]
        filler = "s" if self.pet_char == "bot" else "b"
        out = []
        for y, r in enumerate(rows):
            for x, ch in enumerate(r):
                if ch == ".":
                    continue
                if ch == "e":
                    if blink:
                        # 闭眼：只留一条线（小机器人的屏幕是深色，线保持亮色才看得见）
                        closed = "e" if self.pet_char == "bot" else "o"
                        ch = closed if y == eye_rows[-1] else filler
                    elif anim == "celebrate" and y == eye_rows[0]:
                        ch = filler                                    # 开心：眯眼
                out.append((x + dx, y + dy, pal[ch]))

        def add(x, y, c):
            out.append((x, y, c))

        if anim == "typing":
            for kx in range(2, 14):
                add(kx, 16, (75, 85, 99))
                add(kx, 17, (55, 65, 81))
            for kx in range(3, 13, 2):
                add(kx, 16, (229, 231, 235) if (kx + cyc) % 4 == 1 else (156, 163, 175))
            la, ra = (14, 15) if cyc % 2 else (15, 14)
            for x in (4, 5):
                add(x, la, body)
            for x in (10, 11):
                add(x, ra, body)
        elif anim == "celebrate":
            on = cyc % 2 == 0
            for i, (px, py) in enumerate([(-2, 3), (17, 2), (-3, 10), (18, 9), (8, -4)]):
                c = (255, 213, 74) if i % 2 else (255, 243, 176)
                add(px, py, c)
                if on:
                    for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        add(px + ox, py + oy, c)
        elif anim == "alert" and cyc % 6 < 4:
            ink, red = (42, 35, 32), (229, 72, 77)
            for bx in range(6, 11):
                for by in range(-5, 1):
                    add(bx, by, (255, 255, 255))
                add(bx, -6, ink)
                add(bx, 1, ink)
            for by in range(-5, 1):
                add(5, by, ink)
                add(11, by, ink)
            for by in (-4, -3, -2, 0):
                add(8, by, red)
        elif anim == "listen":
            n = cyc % 3
            for w in range(n + 1):
                for wy in range(6 - w, 10 + w):
                    add(-1 - w * 2, wy, self.color)
                    add(16 + w * 2, wy, self.color)
        elif anim == "pat":
            top = next(y for y, r in enumerate(rows) if r.strip("."))   # 头顶那一行
            ink, skin = (42, 35, 32), (255, 214, 186)
            hx = 4 + [0, 1, 2, 1][cyc % 4]                  # 小手左右来回摸
            hy = top - 3 + dy                               # 手掌最下面一行压在头顶上
            paw = (".oooo.", "oSSSSo", "oSSSSo", ".oooo.")
            for py, line in enumerate(paw):
                for px, ch in enumerate(line):
                    if ch != ".":
                        add(hx + px, hy + py, skin if ch == "S" else ink)
            pink = (255, 105, 140)
            heart = ((1, 0), (3, 0), (0, 1), (1, 1), (2, 1), (3, 1), (4, 1), (1, 2), (2, 2), (3, 2), (2, 3))
            if cyc >= 2:                                    # 稍后冒出一颗心，往上飘
                rise = min((cyc - 2) // 2, 4, top + PET_OY - 5)  # 别飘出格子顶
                for px, py in heart:
                    add(12 + px, top - 5 - rise + py, pink)
        elif anim == "sleep":
            zy = -2 - (cyc % 4)
            for px, py in ((13, zy), (14, zy), (15, zy), (16, zy), (15, zy + 1), (14, zy + 2),
                           (13, zy + 3), (14, zy + 3), (15, zy + 3), (16, zy + 3)):
                add(px, py, (203, 213, 225))
        return out

    def _draw_pet(self, anim: str, el: float, s: int):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (PET_GW * s, PET_GH * s), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        for x, y, c in self._pet_pixels(anim, el):
            gx, gy = x + PET_OX, y + PET_OY
            if 0 <= gx < PET_GW and 0 <= gy < PET_GH:
                dr.rectangle((gx * s, gy * s, gx * s + s - 1, gy * s + s - 1), fill=c + (255,))
        return img

    def _draw_bubble(self, k: float, text: str):
        from PIL import Image, ImageDraw
        ss = 2
        s = k * ss
        font = self._font(12 * s)
        text = text.replace("\n", " ")
        if len(text) > 18:
            text = text[:18] + "…"
        tw = font.getlength(text)
        pad_x, pad_y, h = 9 * s, 5 * s, 24 * s
        w = tw + 2 * pad_x
        big = Image.new("RGBA", (math.ceil(w) + 2, math.ceil(h) + 2), (0, 0, 0, 0))
        dr = ImageDraw.Draw(big, "RGBA")
        dr.rounded_rectangle((0, 0, w, h), radius=7 * s, fill=(255, 255, 255, 245),
                             outline=(42, 35, 32, 255), width=max(int(1.5 * s), 1))
        dr.text((pad_x, h / 2), text, font=font, fill=(26, 26, 26, 255), anchor="lm")
        return big.resize((math.ceil(big.width / ss), math.ceil(big.height / ss)), Image.LANCZOS)

    def _draw_pill(self, k, accent, label, time_text, levels, dot_style, now):
        from PIL import Image, ImageDraw, ImageFilter
        ss = 2
        s = k * ss
        font = self._font(14 * s)
        items = [("dot", self.DOT), ("label", font.getlength(label) / s)]
        if time_text:
            items.append(("time", font.getlength("0:00") / s))
        if levels:
            items.append(("bars", len(levels) * 3 + (len(levels) - 1) * 2))
        content = sum(w for _, w in items) + self.GAP * (len(items) - 1)
        W, H, M = content + 2 * self.PAD_X, self.H, self.MARGIN
        left = self._expand_left()
        if left:
            items.reverse()             # 圆点靠近桌宠那一端
        cw, ch = math.ceil((W + 2 * M) * s), math.ceil((H + 2 * M) * s)
        img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        key = (cw, ch)
        if key not in self._shadow_cache:
            sh = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle(
                (M * s, (M + 5) * s, (M + W) * s, (M + 5 + H) * s), radius=H / 2 * s,
                fill=(0, 0, 0, 90))
            self._shadow_cache = {key: sh.filter(ImageFilter.GaussianBlur(8 * s))}
        img.alpha_composite(self._shadow_cache[key])
        dr = ImageDraw.Draw(img, "RGBA")
        dr.rounded_rectangle((M * s, M * s, (M + W) * s, (M + H) * s), radius=H / 2 * s,
                             fill=self.BG + (int(255 * self.opacity),),
                             outline=(255, 255, 255, 26), width=max(int(s), 1))
        cy = M + H / 2
        x = M + self.PAD_X
        anchor = (x + self.DOT / 2, cy)
        for kind, w in items:
            if kind == "dot":
                cx = x + self.DOT / 2
                anchor = (cx, cy)
                r = self.DOT / 2
                if dot_style == "pulse":
                    ph = (now % 1.2) / 1.2
                    if ph < 0.7:
                        rr = r + 7 * ph / 0.7
                        dr.ellipse(((cx - rr) * s, (cy - rr) * s, (cx + rr) * s, (cy + rr) * s),
                                   fill=(255, 255, 255, int(90 * (1 - ph / 0.7))))
                if dot_style == "check":
                    pts = [(cx - 3.6, cy + 0.4), (cx - 1.1, cy + 3), (cx + 4, cy - 3)]
                    dr.line([(px * s, py * s) for px, py in pts], fill=accent + (255,),
                            width=max(int(2.2 * s), 1), joint="curve")
                else:
                    alpha = 255
                    if dot_style == "blink":
                        alpha = int(255 * (0.35 + 0.65 * (0.5 + 0.5 * math.cos(2 * math.pi * now / 0.9))))
                    dr.ellipse(((cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s),
                               fill=accent + (alpha,))
            elif kind == "label":
                dr.text((x * s, cy * s), label, font=font, fill=self.TEXT + (255,), anchor="lm")
            elif kind == "time":
                dr.text((x * s, cy * s), time_text, font=font, fill=self.SUBTEXT + (255,),
                        anchor="lm")
            elif kind == "bars":
                bars = levels if not left else list(reversed(levels))
                bx = x
                for v in bars:
                    h = 4 + v * 14
                    dr.rounded_rectangle((bx * s, (cy - h / 2) * s, (bx + 3) * s, (cy + h / 2) * s),
                                         radius=1.5 * s, fill=self.color + (255,))
                    bx += 5
            x += w + self.GAP
        out_w, out_h = math.ceil((W + 2 * M) * k), math.ceil((H + 2 * M) * k)
        img = img.resize((out_w, out_h), Image.LANCZOS)
        return img, round(anchor[0] * k), round(anchor[1] * k)

    def _blit(self, img, x: int, y: int) -> None:
        w, h = img.size
        a = np.asarray(img, dtype=np.uint8)
        alpha = a[..., 3:4].astype(np.uint16)
        pm = (a[..., :3].astype(np.uint16) * alpha // 255).astype(np.uint8)  # 预乘 alpha
        bgra = np.empty_like(a)
        bgra[..., 0], bgra[..., 1], bgra[..., 2], bgra[..., 3] = pm[..., 2], pm[..., 1], pm[..., 0], a[..., 3]
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h  # 负数 = 自上而下
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, bgra.tobytes(), w * h * 4)
            old = gdi32.SelectObject(mem, hbmp)
            blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(x, y)),
                                       ctypes.byref(wt.SIZE(w, h)), mem,
                                       ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend),
                                       ULW_ALPHA)
            gdi32.SelectObject(mem, old)
        finally:
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)


# ───────────────────────────── 录音设备 ─────────────────────────────

_pa_lock = threading.Lock()
_API_RANK = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2}


def find_input_device(name: str, refresh: bool = False) -> dict | None:
    """按名字片段找输入设备；name 为空则用系统默认输入。找不到返回 None。"""
    import sounddevice as sd
    with _pa_lock:
        if refresh:  # PortAudio 只在初始化时枚举设备，热插拔后要重新初始化
            sd._terminate()
            sd._initialize()
        try:
            if not name:
                return dict(sd.query_devices(kind="input"))
            apis = sd.query_hostapis()
            best, best_rank = None, 99
            for d in sd.query_devices():
                if d["max_input_channels"] <= 0 or name.lower() not in d["name"].lower():
                    continue
                rank = _API_RANK.get(apis[d["hostapi"]]["name"], 9)
                if rank < best_rank:
                    best, best_rank = dict(d), rank
            return best
        except Exception:
            return None


# ───────────────────────────── 主程序 ─────────────────────────────

class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.transcriber = Transcriber(cfg)
        self.jobs: queue.Queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._recording = False
        self._cancel = threading.Event()
        self._generation = 0  # 每次“取消”加一，用来作废还在转写的结果
        self._main_tid = kernel32.GetCurrentThreadId()
        self.beep = Beeper(cfg["feedback"])
        self.overlay = Overlay(cfg, self.quit, lambda: self._focus("focus", toggle=False))
        self._hotkeys: dict[int, tuple[str, int]] = {}

    # ── 生命周期 ──

    def run(self) -> None:
        self.overlay.create()  # 窗口必须建在跑消息循环的主线程上
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._housekeeping, daemon=True).start()
        self.jobs.put(("preload",))
        self._register_hotkeys()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and not msg.hWnd:
                action, vk = self._hotkeys.get(int(msg.wParam), ("", 0))
                try:
                    self._dispatch(action, vk)
                except Exception:
                    log.exception("处理快捷键 %s 时出错", action)
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for hk_id in self._hotkeys:
            user32.UnregisterHotKey(None, hk_id)
        self.overlay.destroy()
        log.info("已退出")

    def quit(self) -> None:
        user32.PostThreadMessageW(self._main_tid, WM_QUIT, 0, 0)

    _ACTIONS = {"talk", "talk_send", "cancel", "focus", "focus_wechat"}

    def _register_hotkeys(self) -> None:
        failed = []
        for i, (action, spec) in enumerate(self.cfg["hotkeys"].items(), start=1):
            if not spec:
                continue
            if action not in self._ACTIONS:
                log.warning("config.toml 里有不认识的快捷键项 %s，已忽略", action)
                continue
            mods, vk = parse_hotkey(spec)
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                self._hotkeys[i] = (action, vk)
                log.info("快捷键 %-9s = %s", action, spec)
            else:
                failed.append(f"{action}={spec}")
        if failed:
            text = "快捷键注册失败（被其他程序占用？）：" + "，".join(failed)
            log.error(text)
            self.overlay.message("快捷键注册失败，见日志", "error", 6.0)

    def _dispatch(self, action: str, vk: int) -> None:
        if action in ("talk", "talk_send"):
            with self._state_lock:
                if self._recording:  # 去重：已经在录就忽略
                    return
                self._recording = True
                self._cancel.clear()
            threading.Thread(target=self._session, args=(action == "talk_send", vk),
                             daemon=True).start()
        elif action == "cancel":
            self._generation += 1
            if self._recording:
                self._cancel.set()
            else:
                self.overlay.set("idle")  # 正在转写的那句作废，悬浮窗立刻回到待机
            self.beep("cancel")
            log.info("取消")
        elif action in ("focus", "focus_wechat"):
            self._focus(action)

    def _focus(self, section: str, toggle: bool = True) -> None:
        """唤起目标程序。toggle=True（手柄 +/−）：已经在前台就把它最小化；
        toggle=False（单击桌宠）：只负责切到前台。
        找不到可见窗口（比如被关到托盘）且配了 launch 时，再启动一次它的 exe
        （微信这类单实例程序会把已开着的窗口调出来）。"""
        fc = self.cfg[section]
        fg = foreground_root_if(fc["process_names"], fc["title_contains"])
        if fg and not toggle:
            log.info("已在前台：%s", fc["process_names"][0])
            return
        if fg:
            user32.ShowWindow(fg, SW_MINIMIZE)
            log.info("已最小化：%s", fc["process_names"][0])
            return
        hwnd = find_window(fc["process_names"], fc["title_contains"])
        if hwnd is not None:
            if focus_window(hwnd):
                log.info("已切到前台：%s", fc["process_names"][0])
            else:
                log.warning("窗口切换被系统拒绝：%s", fc["process_names"])
            return
        launch = fc.get("launch") or ""
        if launch:
            try:
                subprocess.Popen([launch], cwd=str(Path(launch).parent))
                log.info("没找到可见窗口，已启动 %s", launch)
                return
            except OSError as e:
                log.warning("启动 %s 失败：%s", launch, e)
        log.warning("没找到要切换的窗口：%s", fc["process_names"])
        self.beep("error")

    # ── 录音 ──

    def _session(self, send: bool, vk: int) -> None:
        """一次“按住说话”：录音线程，从按下到松开。"""
        generation = self._generation
        # WASAPI 要求开流的线程先初始化 COM；本线程是新开的，不初始化会报
        # “Unanticipated host error -9999”（报错里的 WDM-KS 字样是残留信息）。
        com_ok = IS_WIN and ole32.CoInitializeEx(None, COINIT_MULTITHREADED) >= 0
        try:
            result = self._record(vk)
        except Exception as e:
            log.error("录音失败：%s", e)
            self.overlay.set("idle")
            self.overlay.message("录音失败，见日志")
            self.beep("error")
            return
        finally:
            if com_ok:
                ole32.CoUninitialize()
            with self._state_lock:
                self._recording = False
        if result is None:
            self.overlay.set("idle")
            return
        audio, sr, timed_out = result
        self.beep("stop")
        self.overlay.set("busy")
        self.jobs.put(("transcribe", audio, sr, send, timed_out, generation, vk))

    def _open_stream(self, callback):
        import sounddevice as sd
        ac = self.cfg["audio"]
        dev = find_input_device(ac["device"])
        if dev is None:
            dev = find_input_device(ac["device"], refresh=True)  # 可能是刚插上
        if dev is None and ac["device"] and ac["fallback_to_default"]:
            dev = find_input_device("")
            if dev:
                log.warning("没找到“%s”，改用默认输入：%s", ac["device"], dev["name"])
        if dev is None:
            raise RuntimeError(f"找不到麦克风“{ac['device'] or '系统默认输入'}”")
        sr = int(dev["default_samplerate"])
        channels = min(int(dev["max_input_channels"]), 2)
        with _pa_lock:
            stream = sd.InputStream(device=dev["index"], samplerate=sr, channels=channels,
                                    dtype="float32", callback=callback)
            stream.start()
        return stream, sr

    def _record(self, vk: int):
        """返回 (音频, 采样率, 是否超时)；被取消则返回 None。"""
        ac = self.cfg["audio"]
        chunks: list[np.ndarray] = []

        overlay = self.overlay

        def callback(indata, frames, time_info, status):
            chunks.append(indata.copy())
            overlay.push_level(float(np.sqrt(np.mean(np.square(indata)))))

        stream, sr = self._open_stream(callback)
        t0 = time.monotonic()
        timed_out = False
        try:
            self.overlay.set("recording")
            self.beep("start")
            up_since = None
            while True:
                time.sleep(0.01)
                now = time.monotonic()
                if self._cancel.is_set():
                    return None
                if now - t0 > float(ac["max_seconds"]):
                    timed_out = True
                    log.warning("达到最长录音时间 %s 秒，自动收尾", ac["max_seconds"])
                    break
                if key_down(vk):
                    up_since = None
                elif up_since is None:
                    up_since = now
                elif now - up_since >= 0.03:  # 连续 30 毫秒都是松开才算，防抖
                    break
        finally:
            with _pa_lock:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    log.warning("关闭录音流出错", exc_info=True)
        if not chunks:
            raise RuntimeError("没有录到任何数据（设备被拔掉了？）")
        return np.concatenate(chunks), sr, timed_out

    # ── 转写与粘贴（单线程串行） ──

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                if job[0] == "preload":
                    self.overlay.set("loading")
                    self.transcriber.load()
                    self._check_device_at_startup()
                else:
                    self._handle_audio(*job[1:])
                if not self._recording:
                    self.overlay.set("idle")
            except Exception:
                log.exception("后台任务出错")
                if not self._recording:
                    self.overlay.set("idle")
                self.overlay.message("出错了，见日志")
                self.beep("error")

    def _handle_audio(self, audio, sr, send, timed_out, generation, vk) -> None:
        ac = self.cfg["audio"]
        duration = len(audio) / sr
        if duration < float(ac["min_seconds"]):
            log.info("录音太短（%.2f 秒），忽略", duration)
            return
        mono = to_mono(audio)
        head, tail = int(float(ac["trim_head"]) * sr), int(float(ac["trim_tail"]) * sr)
        if len(mono) > head + tail + int(0.1 * sr):  # 裁掉按键咔哒声
            mono = mono[head:len(mono) - tail]
        rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
        if rms < float(ac["silence_rms"]):
            log.warning("录到的几乎是静音（RMS=%.5f）。发射器开了吗？是否被静音？", rms)
            self.overlay.message("几乎没录到声音")
            self.beep("error")
            return
        if not self.transcriber.loaded:
            self.overlay.set("loading")
        t0 = time.monotonic()
        text = self.transcriber.transcribe(resample(mono, sr))
        cost = time.monotonic() - t0
        text = apply_replacements(text, self.cfg["replacements"]).strip()
        shown = text if self.cfg["feedback"]["log_text"] else f"<{len(text)} 字>"
        log.info("录音 %.1f 秒，转写 %.2f 秒：%s", duration, cost, shown)
        if generation != self._generation:
            log.info("已被取消，丢弃结果")
            return
        if not text:
            return
        if not paste_text(text, self.cfg, vk):
            self.overlay.message("没能自动粘贴，文字在剪贴板里")
            self.beep("error")
            return
        if send and (not timed_out or self.cfg["paste"]["send_on_timeout"]):
            time.sleep(float(self.cfg["paste"]["enter_delay"]))
            send_keys(VK_RETURN)
        self.overlay.done()

    def _check_device_at_startup(self) -> None:
        """启动时麦克风不在就提示一次，然后在后台每隔几秒重试，直到出现。"""
        ac = self.cfg["audio"]
        dev = find_input_device(ac["device"])
        if dev is not None:
            log.info("麦克风：%s", dev["name"])
            return
        text = f"找不到麦克风“{ac['device']}”，会自动重试"
        log.warning(text)
        self.overlay.message("找不到麦克风，自动重试中", "error", 8.0)

        def retry():
            while True:
                time.sleep(float(ac["retry_seconds"]))
                if self._recording:
                    continue
                dev = find_input_device(ac["device"], refresh=True)
                if dev is not None:
                    log.info("麦克风已就绪：%s", dev["name"])
                    self.overlay.message("麦克风已就绪", "info", 3.0)
                    return

        threading.Thread(target=retry, daemon=True).start()

    def _housekeeping(self) -> None:
        minutes = float(self.cfg["model"]["idle_unload_minutes"])
        if minutes <= 0:
            return
        while True:
            time.sleep(30)
            idle = time.monotonic() - self.transcriber.last_used
            if (self.transcriber.loaded and idle > minutes * 60
                    and not self._recording and self.jobs.empty()):
                self.transcriber.unload()


# ───────────────────────────── 命令行 ─────────────────────────────

def cmd_list_devices() -> None:
    import sounddevice as sd
    apis = sd.query_hostapis()
    for d in sd.query_devices():
        if d["max_input_channels"] > 0:
            print(f"[{d['index']:>2}] {d['name']}  ({apis[d['hostapi']]['name']}, "
                  f"{d['max_input_channels']} 声道, {int(d['default_samplerate'])} Hz)")
    print("\n把名字里有辨识度的一段填进 config.toml 的 audio.device 即可。")


def cmd_transcribe(path: str, cfg: dict) -> None:
    import wave
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            sys.exit("只支持 16 位 PCM 的 wav")
        sr, ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = to_mono(pcm.reshape(-1, ch).astype(np.float32) / 32768.0)
    tr = Transcriber(cfg)
    t0 = time.monotonic()
    tr.load()
    t1 = time.monotonic()
    text = tr.transcribe(resample(audio, sr))
    t2 = time.monotonic()
    dur = len(audio) / sr
    print(f"加载模型 {t1 - t0:.2f} 秒；音频 {dur:.1f} 秒，转写 {t2 - t1:.2f} 秒"
          f"（实时率 {(t2 - t1) / dur:.3f}）")
    print(apply_replacements(text, cfg["replacements"]))


def cmd_autostart(install: bool) -> None:
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                         winreg.KEY_SET_VALUE)
    with key:
        for name in LEGACY_APP_NAMES + (() if install else (APP_NAME,)):
            try:
                winreg.DeleteValue(key, name)  # 改名前留下的旧自启项指向已不存在的路径
            except FileNotFoundError:
                pass
        if install:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            value = f'"{pythonw}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, value)
            print("已设置开机自启：", value)
        else:
            print("已取消开机自启")


CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
PET_HOOK_EVENTS = ("UserPromptSubmit", "Stop", "StopFailure", "Notification")
_WECHAT_GUESSES = [r"C:\Program Files\Tencent\Weixin\Weixin.exe",
                   r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
                   r"C:\Program Files\Tencent\WeChat\WeChat.exe",
                   r"D:\Weixin\Weixin.exe"]


def install_claude_hooks(path: Path = CLAUDE_SETTINGS_PATH) -> Path | None:
    """把桌宠钩子合并进 Claude Code 的 settings.json：先备份，已有的 pet_hook 项改成本机路径，
    没有的补上，其他设置和别的钩子原样保留。返回备份文件路径（原来没有 settings.json 时为 None）。"""
    data, backup = {}, None
    if path.exists():
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw) if raw.strip() else {}
        backup = BASE / "backup" / f"claude-settings.json.{time.strftime('%Y%m%d-%H%M%S')}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(raw, encoding="utf-8")
    python = Path(sys.executable).with_name("python.exe")
    cmd = f'"{python.as_posix()}" -I "{(BASE / "pet_hook.py").as_posix()}"'
    hooks = data.setdefault("hooks", {})
    for event in PET_HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        found = False
        for group in groups:
            for h in group.get("hooks", []):
                if "pet_hook.py" in str(h.get("command", "")):
                    h.update(type="command", command=cmd)
                    found = True
        if not found:
            entry = {"hooks": [{"type": "command", "command": cmd}]}
            if event == "Notification":
                entry = {"matcher": "", **entry}
            groups.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def _choose_microphone(cfg: dict) -> None:
    import sounddevice as sd
    names = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0 and d["name"] not in names and "映射器" not in d["name"] \
                and "Mapper" not in d["name"] and "主声音捕获" not in d["name"]:
            names.append(d["name"])
    if not names:
        print("  没找到任何麦克风，之后插上再运行一次 --setup。")
        return
    current = cfg["audio"]["device"] or "系统默认"
    print(f"  当前：{current}")
    for i, n in enumerate(names, 1):
        print(f"  [{i}] {n}")
    try:
        choice = input("  输入编号选麦克风（直接回车 = 不改）：").strip()
    except EOFError:  # 没有可输入的终端（比如被其他程序调用），保持不变
        print()
        choice = ""
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        name = names[int(choice) - 1]
        write_config_value("audio", "device", json.dumps(name, ensure_ascii=False))
        print(f"  已选：{name}")


def _find_wechat() -> str:
    hwnd = find_window(["weixin.exe", "wechat.exe"])
    if hwnd:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        path = _process_path(pid.value)
        if path:
            return path
    return next((p for p in _WECHAT_GUESSES if Path(p).exists()), "")


def cmd_setup(cfg: dict) -> None:
    """新电脑一键设置：选麦克风、找微信、挂 Claude 钩子、开机自启、启动。"""
    print("== Pixel Murmur：本机设置 ==")
    if not LOCAL_CONFIG_PATH.exists():
        LOCAL_CONFIG_PATH.write_text(_LOCAL_HEADER, encoding="utf-8")
    print("\n1. 麦克风")
    if sys.stdin and sys.stdin.isatty():
        _choose_microphone(cfg)
    else:
        print("  （不是在命令行窗口里运行，跳过；之后可改 config.local.toml 的 audio.device）")
    print("\n2. 微信")
    wechat = _find_wechat()
    if wechat:
        write_config_value("focus_wechat", "launch", "'" + wechat + "'")
        print(f"  找到：{wechat}")
    else:
        print("  没找到微信。装了的话，把 Weixin.exe 的完整路径填进 config.local.toml 的 "
              "[focus_wechat] launch。")
    print("\n3. Claude Code 钩子（桌宠的完成提醒）")
    backup = install_claude_hooks()
    print(f"  已写入 {CLAUDE_SETTINGS_PATH}")
    if backup:
        print(f"  原文件备份在 {backup}")
    print("  已经开着的 Claude 会话可能要新开一个才生效。")
    print("\n4. 开机自启")
    cmd_autostart(True)
    print("\n5. 启动")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw), str(Path(__file__).resolve())], cwd=str(BASE))
    print("  已在后台启动（已经在运行的话这次会自动退出）。第一次启动会下载约 160 MB 的模型。")
    print("\n完成。手柄连上这台电脑、在 8BitDo App 里确认按键映射后就能用。")


def main() -> None:
    ap = argparse.ArgumentParser(description="Pixel Murmur：为 vibe coding 而生的语音输入 + 像素桌宠")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--transcribe", metavar="WAV")
    ap.add_argument("--install-autostart", action="store_true")
    ap.add_argument("--remove-autostart", action="store_true")
    ap.add_argument("--setup", action="store_true", help="新电脑一键设置")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config()

    if args.list_devices:
        return cmd_list_devices()
    if args.transcribe:
        return cmd_transcribe(args.transcribe, cfg)
    if not IS_WIN:
        sys.exit("常驻模式只支持 Windows")
    if args.install_autostart or args.remove_autostart:
        return cmd_autostart(args.install_autostart)
    if args.setup:
        return cmd_setup(cfg)

    # 单实例
    mutex = kernel32.CreateMutexW(None, False, "Local\\PixelMurmur")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        log.warning("已有一个实例在运行，本次启动退出")
        return
    _ = mutex  # 句柄保持到进程结束

    log.info("启动，后端=%s", cfg["model"]["backend"])
    set_dpi_awareness()
    try:
        App(cfg).run()
    except Exception:
        log.exception("致命错误")
        raise


if __name__ == "__main__":
    main()
