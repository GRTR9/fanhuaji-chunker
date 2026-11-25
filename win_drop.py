"""
Windows 原生拖放（WM_DROPFILES），純 ctypes，不需任何原生擴充元件。

tkinterdnd2 的原生 tkdnd 元件載入失敗時（例如 Windows ARM64 版 Python）作為退路。
只支援把檔案／資料夾從檔案總管拖進視窗。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

if sys.platform == "win32":
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.GetParent.restype = wintypes.HWND
    _set_wndproc = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW
    _set_wndproc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _set_wndproc.restype = ctypes.c_void_p
    _user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
    _user32.CallWindowProcW.restype = LRESULT

    _shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    _shell32.DragAcceptFiles.restype = None
    _shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                        wintypes.LPWSTR, wintypes.UINT]
    _shell32.DragQueryFileW.restype = wintypes.UINT
    _shell32.DragFinish.argtypes = [wintypes.HANDLE]
    _shell32.DragFinish.restype = None


def _query_files(hdrop) -> list[str]:
    count = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    paths = []
    for i in range(count):
        n = _shell32.DragQueryFileW(hdrop, i, None, 0)
        buf = ctypes.create_unicode_buffer(n + 1)
        _shell32.DragQueryFileW(hdrop, i, buf, n + 1)
        paths.append(buf.value)
    return paths


def enable_file_drop(root, on_drop: Callable[[list[str]], None]) -> bool:
    """讓 Tk 頂層視窗接受檔案拖放；成功回傳 True。on_drop 在主執行緒呼叫。"""
    if sys.platform != "win32":
        return False
    try:
        root.update_idletasks()
        hwnd = _user32.GetParent(root.winfo_id()) or root.winfo_id()
        state: dict = {}
        pending: list[list[str]] = []

        # 注意：視窗程序是在 Tcl 事件迴圈內被呼叫的，這裡不能碰任何 tkinter 物件
        # （會造成 PyEval_RestoreThread 致命錯誤），只把路徑放進清單，交給 after() 輪詢。
        def wndproc(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                try:
                    paths = _query_files(wparam)
                finally:
                    _shell32.DragFinish(wparam)
                if paths:
                    pending.append(paths)
                return 0
            return _user32.CallWindowProcW(state["old"], h, msg, wparam, lparam)

        def poll():
            while pending:
                on_drop(pending.pop(0))
            root.after(100, poll)

        state["proc"] = WNDPROC(wndproc)  # 保留參考，避免被回收
        old = _set_wndproc(hwnd, GWLP_WNDPROC, ctypes.cast(state["proc"], ctypes.c_void_p))
        if not old:
            return False
        state["old"] = old
        root._win_drop_state = state
        _shell32.DragAcceptFiles(hwnd, True)
        root.after(100, poll)
        return True
    except Exception:  # noqa: BLE001
        return False
