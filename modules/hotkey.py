from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from modules.contracts import UiCommandType

logger = logging.getLogger(__name__)

# Costanti Win32 per RegisterHotKey
MOD_ALT: int = 0x0001
MOD_CONTROL: int = 0x0002
MOD_SHIFT: int = 0x0004
MOD_WIN: int = 0x0008
MOD_NOREPEAT: int = 0x4000

WM_HOTKEY: int = 0x0312
PM_REMOVE: int = 0x0001

_SPECIAL_KEYS: dict[str, int] = {
    "SPACE": 0x20,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "PAUSE": 0x13,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
}


def parse_hotkey_string(hotkey_str: str) -> tuple[int, int]:
    """Converte una stringa di scorciatoia (es. 'Ctrl+Alt+P') in (modifiers, vk_code).

    Supporta combinazioni di Ctrl, Alt, Shift, Win e tasti A-Z, 0-9, F1-F24 e speciali.
    """
    parts = [p.strip() for p in hotkey_str.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"Stringa scorciatoia vuota o non valida: {hotkey_str!r}")

    modifiers = 0
    key_part = ""

    for part in parts:
        lower = part.lower()
        if lower in ("ctrl", "control"):
            modifiers |= MOD_CONTROL
        elif lower == "alt":
            modifiers |= MOD_ALT
        elif lower == "shift":
            modifiers |= MOD_SHIFT
        elif lower in ("win", "windows", "super", "cmd"):
            modifiers |= MOD_WIN
        else:
            if key_part:
                raise ValueError(f"Scorciatoia con più tasti principali non valida: {hotkey_str!r}")
            key_part = part

    if not key_part:
        raise ValueError(f"Nessun tasto principale specificato nella scorciatoia: {hotkey_str!r}")

    upper_key = key_part.upper()

    # Tasti funzione F1..F24
    if upper_key.startswith("F") and upper_key[1:].isdigit():
        f_num = int(upper_key[1:])
        if 1 <= f_num <= 24:
            vk_code = 0x70 + (f_num - 1)
            return modifiers, vk_code

    # Tasti speciali
    if upper_key in _SPECIAL_KEYS:
        return modifiers, _SPECIAL_KEYS[upper_key]

    # Carattere singolo (A-Z, 0-9)
    if len(upper_key) == 1:
        return modifiers, ord(upper_key)

    raise ValueError(f"Tasto non riconosciuto nella scorciatoia: {key_part!r}")


class GlobalHotkeyListener:
    """Ascoltatore in background per scorciatoie globali Windows (Win32 RegisterHotKey)."""

    def __init__(
        self,
        hotkey_str: str = "Ctrl+Alt+P",
        command_queue: Any = None,
        on_trigger: Callable[[], None] | None = None,
        debounce_sec: float = 0.30,
        register_fn: Callable[..., bool] | None = None,
        unregister_fn: Callable[..., bool] | None = None,
    ) -> None:
        self.hotkey_str = hotkey_str
        self.command_queue = command_queue
        self.on_trigger = on_trigger
        self.debounce_sec = float(debounce_sec)
        self.register_fn = register_fn
        self.unregister_fn = unregister_fn

        self._hotkey_id: int = 1001
        self._last_trigger_time: float = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered: bool = False

    @property
    def is_registered(self) -> bool:
        return self._registered

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def trigger_now(self) -> bool:
        """Esegue l'azione della scorciatoia applicando il debounce.

        Restituisce True se l'evento è stato accettato ed eseguito, False se bloccato dal debounce.
        """
        with self._lock:
            now = time.monotonic()
            if (now - self._last_trigger_time) < self.debounce_sec:
                return False
            self._last_trigger_time = now

        logger.info("Scorciatoia globale attivata: %s", self.hotkey_str)

        # Invia comando alla coda UI se configurata
        if self.command_queue is not None:
            try:
                if hasattr(self.command_queue, "put_nowait"):
                    self.command_queue.put_nowait(UiCommandType.TOGGLE_PAUSE)
                elif hasattr(self.command_queue, "put"):
                    self.command_queue.put(UiCommandType.TOGGLE_PAUSE, block=False)
            except Exception as exc:
                logger.warning("Impossibile accodare TOGGLE_PAUSE: %s", exc)

        # Callback personalizzata se configurata
        if self.on_trigger is not None:
            try:
                self.on_trigger()
            except Exception as exc:
                logger.warning("Errore nella callback della scorciatoia: %s", exc)

        return True

    def start(self) -> None:
        """Avvia il thread in background per la registrazione e l'ascolto della scorciatoia."""
        if self.is_running():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="GlobalHotkeyThread",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Arresta l'ascolto e rilascia la scorciatoia registrata."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _run_loop(self) -> None:
        try:
            modifiers, vk_code = parse_hotkey_string(self.hotkey_str)
        except Exception as exc:
            logger.warning("Scorciatoia non valida (%s): %s", self.hotkey_str, exc)
            return

        success = self._register_hotkey(self._hotkey_id, modifiers, vk_code)
        if not success:
            logger.warning("Registrazione scorciatoia globale non riuscita per %s", self.hotkey_str)
            return

        self._registered = True
        logger.info("Scorciatoia globale registrata con successo: %s", self.hotkey_str)

        try:
            self._message_loop()
        finally:
            self._unregister_hotkey(self._hotkey_id)
            self._registered = False
            logger.debug("Scorciatoia globale de-registrata: %s", self.hotkey_str)

    def _register_hotkey(self, hotkey_id: int, modifiers: int, vk_code: int) -> bool:
        if self.register_fn is not None:
            try:
                return bool(self.register_fn(None, hotkey_id, modifiers, vk_code))
            except Exception as exc:
                logger.warning("register_fn mock fallita: %s", exc)
                return False

        if sys.platform != "win32":
            logger.warning("Scorciatoie globali supportate nativamente solo su Windows.")
            return False

        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            user32.RegisterHotKey.restype = wintypes.BOOL

            # Prova con MOD_NOREPEAT, se fallisce riprova senza
            res = user32.RegisterHotKey(None, hotkey_id, modifiers | MOD_NOREPEAT, vk_code)
            if not res:
                res = user32.RegisterHotKey(None, hotkey_id, modifiers, vk_code)
            return bool(res)
        except Exception as exc:
            logger.warning("Errore Win32 RegisterHotKey: %s", exc)
            return False

    def _unregister_hotkey(self, hotkey_id: int) -> bool:
        if self.unregister_fn is not None:
            try:
                return bool(self.unregister_fn(None, hotkey_id))
            except Exception as exc:
                logger.warning("unregister_fn mock fallita: %s", exc)
                return False

        if sys.platform != "win32":
            return True

        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.UnregisterHotKey.restype = wintypes.BOOL
            return bool(user32.UnregisterHotKey(None, hotkey_id))
        except Exception as exc:
            logger.warning("Errore Win32 UnregisterHotKey: %s", exc)
            return False

    def _message_loop(self) -> None:
        if sys.platform != "win32" or self.register_fn is not None:
            # Per piattaforme non-Windows o test con mock function
            while not self._stop_event.is_set():
                time.sleep(0.05)
            return

        import ctypes
        from ctypes import wintypes

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", wintypes.POINT),
            ]

        user32 = ctypes.windll.user32
        msg = MSG()

        while not self._stop_event.is_set():
            # PeekMessageW con PM_REMOVE è non bloccante per verificare _stop_event regolarmente
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                    self.trigger_now()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)
