"""Claude Code 钩子 -> 桌宠。

由 ~/.claude/settings.json 的 hooks 调用（UserPromptSubmit / Stop / StopFailure / Notification）。
从标准输入读钩子的 JSON，挑出桌宠需要的几项，用 WM_COPYDATA 发给语音输入的悬浮窗。
悬浮窗没开就什么也不做。永远以 0 退出，不打印任何东西，不影响 Claude。
"""
import ctypes
import json
import re
import sys
from ctypes import wintypes as wt

PET_MSG_MAGIC = 0x50455431  # 和 voice_input.py 里的一致
WM_COPYDATA = 0x004A
SMTO_ABORTIFHUNG = 0x0002


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wt.DWORD), ("lpData", ctypes.c_void_p)]


def summarize(text: str, limit: int = 40) -> str:
    """取回复里第一行有内容的文字，去掉 Markdown 记号。"""
    for line in text.splitlines():
        line = re.sub(r"[#*`>|_~\[\]]", "", line).strip(" -\t")
        if line:
            return line[:limit]
    return ""


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except Exception:
        return
    event = data.get("hook_event_name") or ""
    msg = {"event": event, "session": str(data.get("session_id") or "")}
    if event == "Stop":
        msg["text"] = summarize(str(data.get("last_assistant_message") or ""))
    elif event == "Notification":
        msg["type"] = str(data.get("notification_type") or "")
        msg["message"] = str(data.get("message") or "")[:200]
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.restype = wt.HWND
    user32.FindWindowW.argtypes = (wt.LPCWSTR, wt.LPCWSTR)
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    user32.SendMessageTimeoutW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT,
                                           wt.UINT, ctypes.POINTER(ctypes.c_size_t))
    hwnd = user32.FindWindowW("VoiceInputOverlay", None)
    if not hwnd:
        return
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    buf = ctypes.create_string_buffer(payload, len(payload))
    cds = COPYDATASTRUCT(PET_MSG_MAGIC, len(payload), ctypes.cast(buf, ctypes.c_void_p))
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW(hwnd, WM_COPYDATA, 0, ctypes.addressof(cds),
                               SMTO_ABORTIFHUNG, 1000, ctypes.byref(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
