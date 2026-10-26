from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from modules.logging_setup import get_logger

logger = get_logger("single_instance")

APP_MUTEX_NAME: str = "Local\\Verbalia"
DASHBOARD_MUTEX_NAME: str = "Local\\Verbalia_dashboard"
_ERROR_ALREADY_EXISTS: int = 183


class SingleInstance:
    """Mutex con nome di Windows: impedisce di avviare due volte la stessa modalità dell'app.

    Il mutex viene rilasciato dal sistema operativo anche se il processo termina in modo
    anomalo, quindi non lascia lock orfani.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        """True se questa è l'unica istanza; False se un'altra istanza detiene già il mutex."""
        if sys.platform != "win32":
            return True
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(None, False, self.name)
            last_error = ctypes.get_last_error()
        except (AttributeError, OSError) as exc:
            logger.debug("CreateMutexW non disponibile: %s", exc)
            return True
        if not handle:
            return True
        if last_error == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = int(handle)
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None or sys.platform != "win32":
            return
        try:
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
        except (AttributeError, OSError):
            pass


def show_message_box(title: str, message: str) -> None:
    """Mostra un avviso informativo (usato quando l'app è avviata senza console)."""
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x00000040)
    except (AttributeError, OSError):
        sys.stderr.write(f"{title}: {message}\n")


def focus_window_by_title(title: str) -> bool:
    """Porta in primo piano una finestra esistente con il titolo indicato."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except (AttributeError, OSError):
        return False
